"""Main mission loop — Showrunner prompting cycle, flock coordination, action execution."""

import json
import time
import traceback

import session as session_mod

from .state import (
    MissionState,
    _lock,
    _missions,
    _flock_status_line,
    _COMPACTION_INTERVAL,
    _MISSION_PHASES,
    _ACTION_RESULT_HARD_CAP,
)
from .scoring import (
    _get_endpoint_ctx,
    _estimate_conversation_tokens,
    _scaled_limits,
)
from .container import (
    _create_container,
    _container_exec,
    _container_write_file,
    _container_read_file,
)
from .parsing import (
    _parse_showrunner_response,
    _diagnose_parse_failure,
)
from .showrunner import (
    _elect_showrunner,
    _find_endpoint,
    _ask_showrunner,
    _compact_conversation,
)
from .flock import (
    _update_flock,
    _reassign_flock_roles,
)
from .actions import _execute_action
from .persistence import _write_mission_log_to_container


def _mission_loop(mission):
    """Main async loop for a running mission."""
    try:
        mission.log_event("INFO", "Mission loop starting")

        resuming = mission.container_id is not None

        if not resuming:
            # 1. Create/reuse container
            mission.log_event("INFO", "Creating Docker container...")
            mission.status_message = "Initializing container..."
            cid = _create_container(mission.mission_id)
            if not cid:
                mission.log_event("ERROR", "Failed to create Docker container")
                mission.status = "error"
                mission.status_message = "Failed to create Docker container"
                return
            mission.container_id = cid
            mission.log_event("INFO", f"Container ready: {cid[:12]}")

            # Load bootstrapped tools manifest into mission state
            _manifest_raw = _container_read_file(cid, "/home/mission/tools/manifest.json")
            if _manifest_raw:
                try:
                    mission.tools = json.loads(_manifest_raw)
                    mission.log_event("INFO", f"Loaded {len(mission.tools)} bootstrapped tools")
                except (ValueError, TypeError):
                    pass

        # Elect or apply Showrunner override
        mission.status_message = "Electing Showrunner..."
        old_sr = mission.showrunner_model
        override = mission.showrunner_override
        if override:
            sr = _find_endpoint(override["node_id"], override["model"])
            if not sr:
                mission.log_event("WARN",
                    f"Showrunner override not available: {override['model']} — falling back to auto")
                mission.showrunner_override = None
                sr = _elect_showrunner()
            else:
                mission.log_event("INFO", f"Showrunner override applied: {sr[1]} on {sr[4]}")
        else:
            sr = _elect_showrunner()
        if not sr:
            mission.log_event("ERROR", "No suitable Showrunner found — no healthy endpoints")
            mission.status = "error"
            mission.status_message = "No healthy endpoints available for Showrunner"
            return
        mission.showrunner_node_id = sr[0]
        mission.showrunner_model = sr[1]
        mission.showrunner_score = sr[3]
        if old_sr and old_sr != sr[1]:
            mission.log_event("INFO",
                f"Showrunner changed: {old_sr} → {sr[1]} on {sr[4]} (score={sr[3]:.1f})")
        elif not old_sr:
            mission.log_event("INFO",
                f"Showrunner elected: {sr[1]} on {sr[4]} (score={sr[3]:.1f})")

        # Always (re-)build flock — new agents may have joined
        mission.status_message = "Building flock..."
        _update_flock(mission)

        mission.status = "running"
        mission.status_message = "Mission active"

        if not resuming:
            _container_write_file(mission.container_id, "/home/mission/mission.txt",
                                  mission.mission_text)

        # Initial Showrunner prompt
        if resuming:
            mission.log_event("INFO", "Mission resumed — continuing from last state")
            context_hint = ""
            if mission.last_summary:
                context_hint = f"\n\nHere is a summary of progress so far:\n{mission.last_summary}"
            initial_prompt = (
                f"Mission has been resumed. Here is the current mission:\n\n"
                f"{mission.mission_text}\n\n"
                f"You have {len(mission.flock)} agents available. "
                f"The workspace at /home/mission/ contains files and tools from previous work."
                f"{context_hint}\n\n"
                f"Start by reading /home/mission/state.json to understand progress. "
                f"Then check the container state: list files in /home/mission/. "
                f"Continue where you left off — remember to update state.json as you go."
            )
        else:
            initial_prompt = (
                f"A new mission has started. Here is the mission:\n\n"
                f"{mission.mission_text}\n\n"
                f"You have {len(mission.flock)} agents available — your flock is your greatest asset. "
                f"Review the agents above — note their speeds, roles, and capabilities.\n\n"
                f"FIRST: Extract ALL specific requirements from the mission text — deliverables, "
                f"word counts, topics to cover, format requirements. Then initialize state tracking:\n"
                f"  write_file /home/mission/state.json with your requirements list and phased plan\n"
                f"THEN: INSPECT before acting — gather any info you need (curl URLs, check tools).\n"
                f"THEN: Start executing. Plan your work to maximize parallelism —\n"
                f"dispatch independent tasks to idle agents while you handle coordination.\n"
                f"Keep every agent busy. A flock sitting idle is wasted potential.\n"
                f"Respond with your plan and first set of actions."
            )

        # ── Main iteration loop ──────────────────────────────────────────
        while not mission._stop_event.is_set() and mission.status == "running":

            # Check for blocking user prompts
            has_blocking = any(
                p["blocking"] and not p["answered"] for p in mission.pending_prompts
            )
            if has_blocking:
                mission.status_message = "Waiting for user input..."
                time.sleep(2)
                continue

            # Auto-complete: result exists but showrunner is stuck in overflow loop
            if mission._has_result and mission._sr_overflow_streak >= 3:
                mission.log_event("INFO",
                    f"Auto-completing: result exists and showrunner context overflow "
                    f"streak={mission._sr_overflow_streak}")
                mission.status = "completed"
                mission.status_message = "Mission completed (auto — result available)"
                mission.status_progress = 100
                break

            # ── Collect ALL updates into a single batched prompt ──
            prompt_parts = []

            # User responses
            answered = [r for r in mission.user_responses if r.get("_new")]
            for r in answered:
                r.pop("_new", None)
                prompt_parts.append(
                    f"User responded to your question:\n"
                    f"Q: {r.get('question', '?')}\n"
                    f"A: {r.get('response', '')}"
                )

            # Completed tasks (batch all unreported results — dynamic limit)
            sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
            loop_limits = _scaled_limits(sr_ctx)
            result_limit = loop_limits["agent_result_max"]
            completed_summaries = []
            for task_data in list(mission.task_history):
                if task_data.get("_reported"):
                    continue
                task_data["_reported"] = True
                completed_summaries.append(
                    f"[{task_data.get('agent_name', '?')}] "
                    f"({task_data.get('status', '?')}): "
                    f"{(task_data.get('result') or task_data.get('error', 'no output'))[:result_limit]}"
                )

            if completed_summaries:
                prompt_parts.append(
                    "Agent results have arrived:\n\n" +
                    "\n\n".join(completed_summaries) +
                    "\n\n⚠ MANDATORY: Verify these results before proceeding. "
                    "Read any files the agent created, run any scripts to check output. "
                    "Do NOT trust agent results without verification. "
                    "Update /home/mission/state.json with progress."
                )

            # Task checkpoints — surface stuck agents or slow progress
            for tid, task in list(mission.tasks.items()):
                if not task.checkpoint:
                    continue
                cp = task.checkpoint
                if cp.get("_surfaced"):
                    continue
                should_surface = False
                reason = ""
                if cp.get("status") == "stuck":
                    should_surface = True
                    reason = "agent appears stuck (2+ consecutive failures)"
                elif (cp.get("elapsed", 0) > task.timeout * 0.5 and
                      cp.get("iteration", 0) < cp.get("max_iterations", 10) * 0.5):
                    should_surface = True
                    reason = "slow progress (>50% time, <50% iterations)"
                if should_surface:
                    cp["_surfaced"] = True
                    prompt_parts.append(
                        f"⚠ Task {tid} ({task.agent_name}): {reason}\n"
                        f"  Iteration {cp.get('iteration')}/{cp.get('max_iterations')}, "
                        f"elapsed {cp.get('elapsed', 0):.0f}s, "
                        f"last action: {cp.get('last_action', '?')}\n"
                        f"  You may cancel_task if the approach is wrong."
                    )

            # Mission text changes
            session = session_mod.get(mission.mission_id)
            if session and session.get("mission_text") != mission.mission_text:
                old_text = mission.mission_text
                mission.mission_text = session["mission_text"]
                mission.mission_version = session.get(
                    "mission_version", mission.mission_version + 1
                )
                mission.log_event("MISSION_CHANGED",
                    f"v{mission.mission_version}: {mission.mission_text[:200]}")
                _reassign_flock_roles(mission)
                # Reset completion state — old result/verification no longer applies
                mission._has_result = False
                mission._completion_verified = False
                mission._sr_consecutive_fails = 0
                mission._sr_overflow_streak = 0
                prompt_parts.append(
                    f"⚠ MISSION TEXT HAS CHANGED (v{mission.mission_version}).\n"
                    f"Old: {old_text[:500]}\n"
                    f"New: {mission.mission_text[:500]}\n\n"
                    f"Your flock has been reassigned with new roles for this mission.\n"
                    f"Decide whether to: (a) pivot immediately, (b) let current tasks complete "
                    f"then pivot, or (c) ignore if minor."
                )

            # Build combined prompt
            if prompt_parts:
                initial_prompt = (
                    "\n\n---\n\n".join(prompt_parts) +
                    "\n\nProcess all updates and decide next steps."
                )
            elif initial_prompt == "Continue the mission. What's next?" and not mission.tasks:
                time.sleep(3)

            # Poison-pill recovery: after 2+ consecutive showrunner failures with
            # no new updates, the previous initial_prompt likely contains oversized
            # content that caused the failure.  Replace with a clean probe.
            if not prompt_parts and mission._sr_consecutive_fails >= 2:
                initial_prompt = (
                    "Continue the mission. What is the current status and what are the next steps?\n"
                    f"{_flock_status_line(mission)}"
                )

            # Update flock periodically
            _update_flock(mission)

            # Ask Showrunner
            mission.status_message = "Showrunner thinking..."
            mission.conversation.append({"role": "user", "content": initial_prompt})

            response_text = _ask_showrunner(mission, initial_prompt, multi_turn=True)
            if not response_text:
                if (mission.conversation and
                        mission.conversation[-1].get("role") == "user"):
                    mission.conversation.pop()
                mission._sr_consecutive_fails += 1
                mission.log_event("ERROR",
                    f"Showrunner failed (streak={mission._sr_consecutive_fails}) "
                    f"— attempting re-election")

                # Auto-complete if we already have a result and keeps failing
                if mission._has_result and (
                    mission._sr_consecutive_fails >= 3
                    or mission._sr_overflow_streak >= 3
                ):
                    mission.log_event("INFO",
                        f"Auto-completing: result exists and showrunner stuck "
                        f"(fails={mission._sr_consecutive_fails}, "
                        f"overflows={mission._sr_overflow_streak})")
                    mission.status = "completed"
                    mission.status_message = "Mission completed (auto — result available)"
                    mission.status_progress = 100
                    break

                sr = _elect_showrunner(exclude_node_id=mission.showrunner_node_id)
                if sr:
                    mission.showrunner_node_id = sr[0]
                    mission.showrunner_model = sr[1]
                    mission.showrunner_score = sr[3]
                    mission.log_event("INFO", f"New Showrunner: {sr[1]} on {sr[4]}")
                    continue
                else:
                    mission.status_message = "No available Showrunner — waiting..."
                    time.sleep(10)
                    continue

            # Success — reset failure streak
            mission._sr_consecutive_fails = 0

            # Auto-progress: estimate based on round trips (asymptotic to 90%)
            if mission.status_progress < 0:
                mission.status_progress = 0
            completed_tasks = len(mission.task_history)
            rt = mission.round_trips
            mission.status_progress = min(
                90, int(90 * (1 - 1.0 / (1 + rt * 0.15 + completed_tasks * 0.1)))
            )

            # Store assistant response in conversation for multi-turn
            mission.conversation.append({"role": "assistant", "content": response_text})

            # Parse and execute actions
            parsed = _parse_showrunner_response(response_text)
            if parsed:
                thinking = parsed.get("thinking", "")
                if thinking:
                    mission.log_event("THINKING", thinking[:3000])

                actions = parsed.get("actions", [])
                actions = [a for a in actions if a.get("type", "").strip()]

                if not actions:
                    mission._consecutive_empty += 1
                    mission.log_event("WARN",
                        f"Showrunner returned no actions "
                        f"(streak={mission._consecutive_empty}) — "
                        f"raw[:{min(500, len(response_text))}]: "
                        f"{response_text[:500]}")

                    # Detect completion intent in thinking
                    completion_phrases = (
                        "mission complete", "mission accomplished",
                        "requirements have been satisfied",
                        "all tasks completed", "mission is done",
                        "successfully completed",
                        "mark the mission as complete", "mark as complete",
                        "marking complete", "all requirements met",
                        "all requirements fulfilled",
                        "ready to complete", "can now complete",
                        "should complete",
                    )
                    thinking_lower = thinking.lower() if thinking else ""
                    # Skip detection if thinking looks like JSON (nested response parsing)
                    _is_json_thinking = thinking and thinking.strip()[:1] in ('{', '[')
                    if not _is_json_thinking and any(phrase in thinking_lower for phrase in completion_phrases):
                        mission.log_event("INFO",
                            "Auto-completing: Showrunner expressed completion in thinking")
                        actions = [{"type": "complete", "summary": thinking[:500]}]
                        mission._consecutive_empty = 0
                    elif mission._consecutive_empty >= 3:
                        state_content = (
                            _container_read_file(mission.container_id,
                                                 "/home/mission/state.json")
                            or "not found"
                        )
                        ls_out, _, _ = _container_exec(
                            mission.container_id, "ls -la /home/mission/",
                            timeout=5
                        )
                        initial_prompt = (
                            "⚠ RECOVERY: You have returned no executable actions for "
                            f"{mission._consecutive_empty} consecutive rounds.\n\n"
                            f"Current workspace files:\n{ls_out}\n\n"
                            f"state.json contents:\n{state_content[:2000]}\n\n"
                            "You MUST respond with a JSON object containing an "
                            "'actions' array. Example: "
                            '{"thinking": "...", "actions": [{"type": "shell", '
                            '"command": "ls"}]}\n'
                            "IMPORTANT: Escape all special characters in JSON "
                            "string values. Use \\n for newlines, \\\" for quotes "
                            "inside strings.\n\n"
                            "What is the next concrete step to complete the mission?"
                        )
                    else:
                        diag = _diagnose_parse_failure(response_text)
                        initial_prompt = (
                            f"Your previous response could not be parsed into actions.\n"
                            f"DIAGNOSIS: {diag}\n"
                            f"RECEIVED (first 300 chars): {response_text[:300]}\n\n"
                            "Please respond with a valid JSON object containing "
                            "'thinking' and 'actions' keys. "
                            "Remember: respond with RAW JSON only, no markdown fences. "
                            "Escape newlines as \\n and quotes as \\\" in string values."
                            "\n\nIf the mission is complete, use: "
                            '{"thinking": "...", "actions": [{"type": "complete", '
                            '"summary": "..."}]}\n\n'
                            f"{_flock_status_line(mission)}\n"
                            "Continue the mission. What's next?"
                        )
                    if mission._consecutive_empty >= 5:
                        time.sleep(min(mission._consecutive_empty * 3, 30))
                    continue
                else:
                    mission._consecutive_empty = 0

                # Dynamic limits based on Showrunner's context window
                sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id,
                                           mission.showrunner_model)
                limits = _scaled_limits(sr_ctx)
                results_summary = []
                total_result_chars = 0
                max_total = limits["total_results_max"]
                substantive_actions = False
                for action in actions:
                    try:
                        atype = action.get("type", "")
                        if atype not in ("status", "user_message", "reflect",
                                         "set_context_window"):
                            substantive_actions = True
                        result = _execute_action(mission, action)
                        if atype in ("read_file", "shell", "batch_read",
                                     "workspace_tree", "search"):
                            limit = min(limits["action_result_max"], _ACTION_RESULT_HARD_CAP)
                        else:
                            limit = min(limits["action_result_max"] // 4, 2000)
                        remaining = max_total - total_result_chars
                        this_limit = min(limit, max(remaining, 200))
                        entry = f"{atype}: {json.dumps(result)[:this_limit]}"
                        results_summary.append(entry)
                        total_result_chars += len(entry)
                    except Exception as e:
                        mission.log_event("ERROR",
                            f"Action failed: {action.get('type', '?')}: {e}")
                        results_summary.append(
                            f"{action.get('type', '?')}: ERROR {e}"
                        )

                # Feed results back as next prompt
                flock_line = _flock_status_line(mission)

                # Track idle agents across rounds for escalating nudge
                idle_agents = [
                    n for n, a in mission.flock.items()
                    if a.status == "available"
                ]
                if idle_agents and not mission.tasks:
                    mission._idle_flock_rounds = getattr(
                        mission, '_idle_flock_rounds', 0
                    ) + 1
                else:
                    mission._idle_flock_rounds = 0

                # Build idle-agent nudge (escalates after 3 rounds)
                idle_nudge = ""
                if mission._idle_flock_rounds >= 5:
                    idle_nudge = (
                        f"\n\n⚠ CRITICAL: {len(idle_agents)} agents "
                        f"({', '.join(idle_agents)}) have been idle "
                        f"for {mission._idle_flock_rounds} consecutive rounds "
                        "while you work solo. This is inefficient. Delegate NOW: "
                        "dispatch a bug-fix, verification, or any sub-task to an "
                        "idle agent. If there is truly nothing to delegate, "
                        "batch your remaining actions (read + fix + test) into "
                        "ONE response."
                    )
                elif mission._idle_flock_rounds >= 3:
                    idle_nudge = (
                        f"\n\n💡 {len(idle_agents)} agents idle for "
                        f"{mission._idle_flock_rounds} rounds. "
                        "Consider delegating: bug fixes, test writing, "
                        "file verification, or documentation improvements "
                        "can all be dispatched."
                    )

                # Nudge about single-action round trips
                single_action_nudge = ""
                substantive_count = sum(
                    1 for a in actions
                    if a.get('type') not in (
                        'status', 'user_message', 'reflect',
                        'set_context_window', 'save_note',
                    )
                )
                if substantive_count == 1 and not mission.tasks:
                    single_action_nudge = (
                        "\n\n⏱ Tip: You sent 1 action this round. Batch multiple "
                        "actions (e.g. read + patch + shell test) in one response "
                        "to save round-trips."
                    )

                # Phase-awareness: always show current phase, nudge when actions suggest advancement
                phase_nudge = ""
                mission_phase = getattr(mission, "mission_phase", "planning")
                if mission_phase == "planning":
                    if mission.round_trips >= 2:
                        sj = _container_read_file(mission.container_id, "/home/mission/state.json") or ""
                        has_plan = '"requirements"' in sj and ('"phases"' in sj or '"plan"' in sj)
                        if not has_plan:
                            phase_nudge = (
                                "\n\n📋 PHASE: planning — state.json needs 'requirements' and 'phases' before advancing. "
                                'Then: {"type": "advance_phase", "phase": "scaffolding"}'
                            )
                        elif "advance_phase" not in str(actions):
                            phase_nudge = (
                                '\n\n📋 PHASE: planning — plan ready. Advance: {"type": "advance_phase", "phase": "scaffolding"}'
                            )
                else:
                    _act_types = {a.get("type") for a in actions}
                    _wrote_code = bool(_act_types & {"write_file", "batch_write", "patch_file", "replace_lines"})
                    _ran_tests = any(
                        a.get("type") in ("shell", "run_tool") and
                        any(kw in (a.get("command", "") + a.get("name", "")).lower()
                            for kw in ("pytest", "test", "jest", "mocha", "verify"))
                        for a in actions
                    )
                    if mission_phase == "scaffolding" and _wrote_code:
                        phase_nudge = '\n\n📋 PHASE: scaffolding → you\'re writing code, advance_phase to "implementing"'
                    elif mission_phase == "implementing" and _ran_tests:
                        phase_nudge = '\n\n📋 PHASE: implementing → running tests, advance_phase to "testing"'
                    elif mission_phase == "testing" and _ran_tests:
                        phase_nudge = '\n\n📋 PHASE: testing → if tests pass, advance_phase to "verifying"'
                    elif mission_phase == "verifying":
                        phase_nudge = '\n\n📋 PHASE: verifying → when verified, advance_phase to "completing"'
                    else:
                        phase_nudge = f"\n\n📋 PHASE: {mission_phase}"

                if results_summary:
                    initial_prompt = (
                        "Action results:\n" +
                        "\n".join(results_summary) +
                        f"\n\n{flock_line}" +
                        idle_nudge +
                        single_action_nudge +
                        phase_nudge +
                        "\nContinue the mission. What's next?"
                    )
                else:
                    initial_prompt = (
                        f"{flock_line}" + idle_nudge + single_action_nudge +
                        phase_nudge +
                        "\nContinue the mission. What's next?"
                    )

                # Invalidate workspace tree cache after file-modifying actions
                if any(a.get("type") in ("write_file", "patch_file", "shell")
                       for a in actions):
                    mission._workspace_tree_at = 0

                # If Showrunner only sent status/message actions and tasks are
                # running, wait for results instead of immediately re-prompting
                if not substantive_actions and mission.tasks:
                    mission.status_message = (
                        f"Waiting for {len(mission.tasks)} autonomous task(s)..."
                    )
                    wait_start = time.time()
                    while mission.tasks and (time.time() - wait_start) < 30:
                        if mission._stop_event.is_set():
                            break
                        has_new = any(
                            not td.get("_reported")
                            for td in mission.task_history
                        )
                        if has_new:
                            break
                        time.sleep(2)
                    continue

            # Periodically write mission log to container
            if mission.round_trips > 0 and mission.round_trips % 10 == 0:
                _write_mission_log_to_container(mission)

            # ── Layer 3: Token-budget-aware compaction ──
            _sr_ctx = (
                _get_endpoint_ctx(mission.showrunner_node_id,
                                  mission.showrunner_model) or 32768
            )
            _conv_tokens = _estimate_conversation_tokens(mission)
            _compact_threshold = int(_sr_ctx * 0.50)
            _needs_compact = (
                _conv_tokens > _compact_threshold
                or (mission.round_trips > 0
                    and mission.round_trips % _COMPACTION_INTERVAL == 0
                    and len(mission.conversation) >
                    _scaled_limits(_sr_ctx)["conversation_window"])
            )
            if _needs_compact and len(mission.conversation) > 6:
                mission.log_event("CONTEXT",
                    f"Compaction triggered: conv≈{_conv_tokens} tokens "
                    f"(threshold={_compact_threshold}, n_ctx={_sr_ctx}, "
                    f"msgs={len(mission.conversation)})")
                _compact_conversation(mission)

            # Wait for pending tasks
            if mission.tasks:
                available_agents = sum(
                    1 for a in mission.flock.values()
                    if a.status == "available"
                )
                if available_agents == 0:
                    mission.status_message = (
                        f"Waiting for {len(mission.tasks)} agent task(s)..."
                    )
                    wait_start = time.time()
                    while mission.tasks and (time.time() - wait_start) < 30:
                        if mission._stop_event.is_set():
                            break
                        has_new = any(
                            not td.get("_reported")
                            for td in mission.task_history
                        )
                        if has_new:
                            break
                        time.sleep(1)
                else:
                    mission.status_message = (
                        f"{len(mission.tasks)} task(s) running, "
                        f"{available_agents} agent(s) free"
                    )
                    time.sleep(2)
            else:
                time.sleep(3)

        mission.log_event("INFO", f"Mission loop ended (status={mission.status})")

    except Exception as e:
        mission.log_event("ERROR",
            f"Mission loop crashed: {e}\n{traceback.format_exc()}")
        mission.status = "error"
        mission.status_message = f"Internal error: {e}"
