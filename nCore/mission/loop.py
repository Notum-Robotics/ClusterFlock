"""Mission loop — Showrunner-centric execution.

The Showrunner does ALL coding, file operations and container work itself.
Flock members are dispatched only for research, information gathering,
asset downloads, and running long parallel tests.
"""

import json
import re
import time
import traceback


# Patterns that indicate leaked chain-of-thought preambles
_THINKING_PREAMBLE_RE = re.compile(
    r'^\s*(?:'
    r'(?:Here\'s|Let me|Okay,?|Alright,?)\s+(?:a |my )?thinking[^:]*:\s*'
    r'|Thinking\s+[Pp]rocess:\s*'
    r'|(?:Step\s+)?\d+\.\s+\*\*Analyze[^*]*\*\*'
    r')',
    re.IGNORECASE | re.MULTILINE
)


def _strip_thinking_preamble(text):
    """Strip leaked chain-of-thought preambles from model responses.

    Some models (Qwen3, etc.) ignore no_think directives and emit their
    reasoning as plain text.  This strips common preamble patterns.
    """
    if not text:
        return text
    # Strip <think>…</think> blocks first
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    # Strip known preamble patterns that indicate leaked CoT
    if _THINKING_PREAMBLE_RE.match(cleaned):
        # The whole response is thinking — return empty to fall back to reasoning_content
        # unless there's substantial non-preamble content after the first few lines
        lines = cleaned.split('\n')
        # Find first line that doesn't look like numbered reasoning
        for i, line in enumerate(lines):
            stripped = line.strip()
            if (stripped and
                not re.match(r'^\d+\.\s+\*\*', stripped) and
                not re.match(r'^\s*[-*]\s+\*\*', stripped) and
                not re.match(r'^\s*(?:Here\'s|Let me|Okay|Alright|Thinking|Step)', stripped, re.I) and
                not stripped.startswith('*   ')):
                # Found substantive content
                return '\n'.join(lines[i:]).strip()
        return ""  # Entire response was thinking
    return cleaned


def _extract_reply_text(result):
    """Extract visible text from an LLM completion result, handling thinking models.

    Thinking models (Qwen3, gemma-4, etc.) may put all output in
    ``reasoning_content`` or wrap it in ``<think>`` tags, leaving ``content``
    empty.  This helper mirrors the extraction logic used by the Showrunner
    and agent-loop so that flock offers / advice don't silently discard
    valid responses.  It also strips leaked chain-of-thought preambles.
    """
    choices = result.get("choices", [])
    if not choices:
        return ""
    msg = choices[0].get("message", {})
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    # Strip <think>…</think> blocks and leaked preambles
    content_clean = _strip_thinking_preamble(content) if content else ""
    return content_clean or reasoning or content


from .state import (
    _lock,
    _ACTION_RESULT_HARD_CAP,
    _CHARS_PER_TOKEN,
    _MISSION_PHASES,
    MissionPlan,
    PlanTask,
)
from .container import (
    _create_container,
    _container_exec,
    _container_write_file,
    _container_read_file,
    _build_workspace_tree,
)
from .showrunner import (
    elect_showrunner,
    find_endpoint,
    ask_showrunner,
    build_showrunner_context,
)
from .flock import update_flock
from .scoring import get_endpoint_ctx
from .parsing import parse_response, diagnose_failure
from .actions import execute_action, ACTION_HANDLERS
from .persistence import persist_missions


# ── Constants ────────────────────────────────────────────────────────────

_MAX_ROUNDS = 400
_WALL_TIMEOUT = 9200
_FLOCK_REFRESH = 120
_PERSIST_INTERVAL = 60

# Plateau detection: identical test output N times in a row → stop looping
_PLATEAU_THRESHOLD = 4       # identical consecutive test outputs
_PLATEAU_WINDOW = 12         # only track last N shell outputs
_PLATEAU_MIN_ROUND = 15      # don't trigger before this round

# Auto-complete: plan 100% done + plateau → force completion
_AUTO_COMPLETE_PLAN_DONE = True
_AUTO_COMPLETE_PLATEAU_ROUNDS = 6  # consecutive plateau rounds before auto-complete

# Spin-loop escalation: bail after N cumulative spin-loop detections
_SPIN_LOOP_BAIL_THRESHOLD = 8

# Showrunner failover: re-elect after N consecutive SR errors
_SR_FAILOVER_THRESHOLD = 3

# Progress-gated timeout: bail if zero tasks completed after N rounds or N seconds
_ZERO_PROGRESS_MAX_ROUNDS = 50
_ZERO_PROGRESS_MAX_SECONDS = 1800  # 30 minutes


# ── Elect or override Showrunner ─────────────────────────────────────────

def _elect_or_override_showrunner(mission):
    """Elect showrunner or use override. Returns bool."""
    if mission.showrunner_override:
        nid = mission.showrunner_override["node_id"]
        mdl = mission.showrunner_override["model"]
        found = find_endpoint(nid, mdl)
        if found:
            mission.showrunner_node_id = found[0]
            mission.showrunner_model = found[1]
            mission.showrunner_score = found[3]
            mission.log_event("SHOWRUNNER",
                              f"Override: {found[1]} on {found[4]} (score={found[3]:.1f})")
            return True
        mission.log_event("WARN", f"Override endpoint not found: {nid}/{mdl}")

    sr = elect_showrunner(penalties=mission._sr_node_perf)
    if not sr:
        return False
    mission.showrunner_node_id = sr[0]
    mission.showrunner_model = sr[1]
    mission.showrunner_score = sr[3]
    mission.log_event("SHOWRUNNER",
                      f"Elected: {sr[1]} on {sr[4]} (score={sr[3]:.1f})")
    return True


# ── Phase helpers ────────────────────────────────────────────────────────

def _advance_phase(mission, new_phase):
    ts = time.strftime("%H:%M:%S")
    old = mission.mission_phase
    mission.mission_phase = new_phase
    mission.phase_history.append({
        "phase": new_phase,
        "entered_at": ts,
        "from": old,
    })
    mission.log_event("PHASE", f"{old} → {new_phase}")


# ── Collect helper task results ──────────────────────────────────────────

def _collect_helper_results(mission):
    """Move completed helper tasks to history. Returns list of result summaries."""
    results = []
    done_ids = []
    for tid, task in list(mission.tasks.items()):
        if task.status in ("done", "failed", "timed_out", "cancelled"):
            summary = f"[{task.agent_name}] {task.status}"
            if task.result:
                summary += f": {task.result[:500]}"
            elif task.error:
                summary += f" ERROR: {task.error[:300]}"
            results.append(summary)
            done_ids.append(tid)
            mission.task_history.append(task.to_dict())
    for tid in done_ids:
        mission.tasks.pop(tid, None)
    return results


# ── Sync plan from state.json ───────────────────────────────────────────

def _req_text(r):
    """Extract text from a requirement dict, trying common key names."""
    if isinstance(r, dict):
        return r.get("text", r.get("desc", r.get("description", "")))
    return str(r)


def _sync_plan_from_state(mission):
    """Read state.json from container and build/update mission.plan for UI."""
    if not mission.container_id:
        return
    raw = _container_read_file(mission.container_id, "/home/mission/state.json")
    if not raw or raw.strip() in ("", "null", "[]", "{}"):
        return
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    tasks_data = state.get("tasks", [])
    reqs = state.get("requirements", [])
    if not tasks_data and not reqs:
        return

    plan_tasks = []
    for item in tasks_data:
        if isinstance(item, dict):
            tid = item.get("id", item.get("title", f"task-{len(plan_tasks)+1}"))
            tid = re.sub(r'[^a-z0-9_-]', '-', tid.lower().strip())[:40]
            status = item.get("status", "pending")
            if status in ("completed", "done", "verified"):
                status = "done"
            elif status in ("in-progress", "in_progress", "working", "active"):
                status = "dispatched"
            elif status in ("failed", "error"):
                status = "failed"
            else:
                status = "pending"
            pt = PlanTask(
                id=tid,
                title=item.get("title", tid),
                type=item.get("type", "implement"),
                deps=[],
                agent_tier=0,
                files=item.get("files", []) if isinstance(item.get("files"), list) else [],
                verify_cmd=item.get("verify_cmd"),
            )
            pt.status = status
            pt.result = item.get("result", "")
            pt.assigned_agent = "Showrunner"
            plan_tasks.append(pt)

    if not tasks_data and reqs:
        for i, req in enumerate(reqs):
            if isinstance(req, dict):
                text = req.get("text", req.get("desc", req.get("description", f"req-{i+1}")))
                verified = req.get("verified", False)
                pt = PlanTask(
                    id=f"req-{i+1}",
                    title=text[:120],
                    type="implement",
                    deps=[],
                    agent_tier=0,
                    files=[],
                    verify_cmd=None,
                )
                pt.status = "done" if verified else "pending"
                pt.assigned_agent = "Showrunner"
                plan_tasks.append(pt)

    if plan_tasks:
        if not mission.plan:
            mission.plan = MissionPlan(
                requirements=[_req_text(r) for r in reqs],
                tasks=plan_tasks,
            )
        else:
            existing_ids = {t.id: t for t in mission.plan.tasks}
            new_tasks = []
            for pt in plan_tasks:
                if pt.id in existing_ids:
                    existing_ids[pt.id].status = pt.status
                    existing_ids[pt.id].result = pt.result
                    new_tasks.append(existing_ids[pt.id])
                else:
                    new_tasks.append(pt)
            mission.plan.tasks = new_tasks
            mission.plan.requirements = [
                _req_text(r) for r in reqs
            ] if reqs else mission.plan.requirements

    if mission.plan:
        done = sum(1 for t in mission.plan.tasks if t.status == "done")
        total = len(mission.plan.tasks)
        if total > 0:
            mission.status_progress = int((done / total) * 90)


# ── Main mission loop ───────────────────────────────────────────────────

def _mission_loop(mission):
    """Mission loop — Showrunner does all coding, dispatches helpers for research."""
    try:
        mission.log_event("INFO", "Mission loop starting (showrunner-centric)")

        _phase_initialize(mission)
        if mission.status != "running":
            return

        # Flock offers: ask all agents what they can contribute (post-init, pre-working)
        if mission.flock and not mission._flock_offers_collected:
            _collect_flock_offers(mission)

        _advance_phase(mission, "working")
        start_time = time.time()
        last_flock_refresh = start_time
        last_persist = start_time
        last_plan_sync = 0
        _recent_sr_actions = []  # Track last N SR actions for spin-loop detection
        _consecutive_parse_fails = 0  # Circuit breaker for parse death spirals
        _consecutive_sr_errors = 0    # Consecutive SR failures for failover
        _sr_reelections = 0           # How many times we've re-elected SR
        _cumulative_spin_loops = 0    # Escalation counter for spin-loop detection
        _recent_test_hashes = []      # Track shell output hashes for plateau detection
        _plateau_rounds = 0           # Consecutive rounds with identical test output
        _plateau_dispatched = False    # Whether we've dispatched flock for test fixes

        for round_num in range(1, _MAX_ROUNDS + 1):
            if mission.status != "running" or mission._stop_event.is_set():
                break

            elapsed = time.time() - start_time
            if elapsed > _WALL_TIMEOUT:
                mission.log_event("WARN", f"Wall-clock timeout ({_WALL_TIMEOUT}s)")
                mission.status_message = "Timeout — completing with current work"
                _advance_phase(mission, "completing")
                _phase_completing(mission)
                return

            now = time.time()
            if now - last_flock_refresh > _FLOCK_REFRESH:
                update_flock(mission)
                last_flock_refresh = now

            if now - last_persist > _PERSIST_INTERVAL:
                persist_missions()
                last_persist = now

            if now - last_plan_sync > 15:
                _sync_plan_from_state(mission)
                last_plan_sync = now

            # Flock advice: tick (fire/collect async requests)
            _flock_advice_tick(mission, round_num)

            helper_results = _collect_helper_results(mission)

            # Build user content for this round-trip
            user_parts = []

            # Inject flock advice (delivered once, non-blocking)
            advice_parts = _collect_flock_advice(mission)
            if advice_parts:
                for ap in advice_parts:
                    user_parts.append(ap)
                user_parts.append("")

            if helper_results:
                user_parts.append("=== HELPER RESULTS ===")
                for r in helper_results:
                    user_parts.append(r)
                user_parts.append("")

            idle_helpers = [
                f"  {name} — {a.role or 'general helper'} ({a.experience or 'junior'})"
                for name, a in mission.flock.items()
                if a.status == "available" and a.name != "Showrunner"
            ]
            busy_helpers = []
            for name, a in mission.flock.items():
                if a.status != "busy":
                    continue
                tid = a.assigned_task or "?"
                task_obj = mission.tasks.get(tid)
                if task_obj:
                    t_elapsed = time.time() - task_obj.created_at
                    cp = task_obj.checkpoint or {}
                    cp_iter = cp.get("iteration", "?")
                    cp_max = cp.get("max_iterations", "?")
                    cp_status = cp.get("status", "running")
                    cp_last = cp.get("last_action", "")
                    line = f"  {name} → {tid} ({t_elapsed:.0f}s, iter {cp_iter}/{cp_max}, {cp_status})"
                    if cp_last:
                        line += f" last: {cp_last[:80]}"
                    if t_elapsed > 180 and cp_status == "stuck":
                        line += " ⚠ STUCK"
                    busy_helpers.append(line)
                else:
                    busy_helpers.append(f"  {name} → {tid}")
            if idle_helpers or busy_helpers:
                user_parts.append("=== FLOCK STATUS ===")
                if idle_helpers:
                    user_parts.append(f"Available helpers ({len(idle_helpers)}):")
                    user_parts.extend(idle_helpers)
                if busy_helpers:
                    user_parts.append(f"Busy ({len(busy_helpers)}):")
                    user_parts.extend(busy_helpers)
                user_parts.append("")

            nudge = _get_behavioral_nudge(mission, round_num)
            if nudge:
                user_parts.append(nudge)

            spin_nudge = _detect_spin_loop(_recent_sr_actions, mission)
            if spin_nudge:
                _cumulative_spin_loops += 1
                user_parts.append(spin_nudge)

                if _cumulative_spin_loops >= _SPIN_LOOP_BAIL_THRESHOLD:
                    mission.log_event("AUTO_BAIL",
                        f"Spin-loop circuit breaker: {_cumulative_spin_loops} "
                        f"cumulative spin-loops detected, forcing completion")
                    mission.status_message = "Spin-loop — completing with current work"
                    _advance_phase(mission, "completing")
                    _phase_completing(mission)
                    return

            # Plateau detection: auto-complete or dispatch flock
            auto_complete_nudge = _check_auto_complete(
                mission, _plateau_rounds, round_num)
            if auto_complete_nudge:
                user_parts.append(auto_complete_nudge)
            elif _plateau_rounds >= 3 and not _plateau_dispatched:
                if _dispatch_flock_for_plateau(
                        mission, _recent_test_hashes, round_num):
                    _plateau_dispatched = True
                    user_parts.append(
                        "⚠ TEST PLATEAU: Your tests produce identical output each run. "
                        "Flock agents have been dispatched to investigate and fix the failures. "
                        "Move on to other work or wait for their results.")

            # Progress-gated timeout: no tasks done after threshold → bail
            plan = getattr(mission, "plan", None)
            tasks_done = 0
            if plan and plan.tasks:
                tasks_done = sum(1 for t in plan.tasks if t.status == "done")
            if tasks_done == 0 and (
                round_num >= _ZERO_PROGRESS_MAX_ROUNDS
                or elapsed >= _ZERO_PROGRESS_MAX_SECONDS
            ):
                trigger = (f"round {round_num}" if round_num >= _ZERO_PROGRESS_MAX_ROUNDS
                           else f"{elapsed:.0f}s elapsed")
                # Check if SR has done any real work (round_trips > 1 means at
                # least one successful SR exchange even without plan tasks)
                if mission.round_trips > 1:
                    mission.log_event("AUTO_BAIL",
                        f"Zero-progress timeout: 0 plan tasks completed after {trigger}, "
                        f"forcing completion with existing work")
                    mission.status_message = f"No progress after {trigger} — completing"
                    _advance_phase(mission, "completing")
                    _phase_completing(mission)
                else:
                    mission.log_event("FAILED",
                        f"Zero-progress timeout: no work completed after {trigger}")
                    mission.status = "failed"
                    mission.status_message = f"Mission failed: no work after {trigger}"
                return

            user_parts.append(f"Round {round_num}/{_MAX_ROUNDS}. "
                              f"Elapsed: {elapsed:.0f}s. "
                              f"Continue working on the mission.")

            if round_num == 1:
                user_parts.insert(0,
                    "Begin work. Inspect the mission, understand requirements, "
                    "then start implementing. You have full file and shell access. "
                    "Focus on building the core deliverables yourself first.")

                # Inject flock offers on round 1 so SR knows what help is available
                if mission._flock_offers:
                    offer_lines = ["=== FLOCK OFFERS ===",
                                   "Your support team reviewed the mission and offered to help:"]
                    for aname, info in mission._flock_offers.items():
                        role = info.get("role", "developer")
                        exp = info.get("experience", "")
                        offer = info.get("offer", "")
                        offer_lines.append(f"  {aname} ({exp} {role}): {offer}")
                    offer_lines.append(
                        "Consider delegating tasks to them when appropriate.\n")
                    user_parts.insert(1, "\n".join(offer_lines))

            user_content = "\n".join(user_parts)

            mission.status_message = f"Working (round {round_num})..."
            response_text = ask_showrunner(mission, user_content, multi_turn=True)

            if not response_text:
                _consecutive_sr_errors += 1
                mission.log_event("WARN",
                    f"Round {round_num}: empty SR response "
                    f"(consecutive={_consecutive_sr_errors})")

                # ── Showrunner failover circuit breaker ──────────────
                if _consecutive_sr_errors >= _SR_FAILOVER_THRESHOLD:
                    old_node = mission.showrunner_node_id
                    old_model = mission.showrunner_model

                    # Penalize the failing node so elect_showrunner avoids it
                    perf = mission._sr_node_perf.setdefault(old_node, {})
                    perf["timeouts"] = perf.get("timeouts", 0) + _consecutive_sr_errors

                    mission.log_event("SR_FAILOVER",
                        f"Showrunner {old_model} on {old_node} failed "
                        f"{_consecutive_sr_errors}x consecutively — re-electing")

                    ok = _elect_or_override_showrunner(mission)
                    if ok and mission.showrunner_node_id != old_node:
                        _sr_reelections += 1
                        _consecutive_sr_errors = 0
                        # Reset conversation — new SR has no context
                        mission.conversation = []
                        mission.log_event("SR_FAILOVER",
                            f"New Showrunner: {mission.showrunner_model} "
                            f"on {mission.showrunner_node_id} "
                            f"(re-election #{_sr_reelections})")
                        update_flock(mission)
                        time.sleep(2)
                        continue
                    elif ok and mission.showrunner_node_id == old_node:
                        # Same node re-elected (only option) — give it more time
                        mission.log_event("SR_FAILOVER",
                            f"Re-elected same node {old_node} (only option) "
                            f"— waiting 15s for model to reload")
                        time.sleep(15)
                        _consecutive_sr_errors = 0
                        continue
                    else:
                        # No healthy SR at all — fail the mission
                        mission.log_event("ERROR",
                            f"No healthy Showrunner available after "
                            f"{_consecutive_sr_errors} failures")
                        mission.status = "failed"
                        mission.status_message = (
                            f"Showrunner failed {_consecutive_sr_errors}x "
                            f"and no alternative available")
                        return

                time.sleep(3)
                continue

            mission.conversation.append({"role": "user", "content": user_content})
            mission.conversation.append({"role": "assistant", "content": response_text})

            parsed = parse_response(response_text)
            if not parsed or not parsed.get("actions"):
                _consecutive_parse_fails += 1
                diag = diagnose_failure(response_text)
                mission.log_event("WARN",
                    f"Round {round_num}: parse failure #{_consecutive_parse_fails} — {diag}")

                if _consecutive_parse_fails >= 8:
                    # Fatal: 8 consecutive failures — bail out
                    mission.log_event("AUTO_BAIL",
                        f"Parse circuit breaker: {_consecutive_parse_fails} consecutive failures, "
                        "forcing completion")
                    mission.status_message = "Parse failures — completing with current work"
                    _advance_phase(mission, "completing")
                    _phase_completing(mission)
                    return

                if _consecutive_parse_fails >= 5:
                    # Severe: trim conversation to system + last 2 exchanges
                    mission.log_event("WARN",
                        f"Parse circuit breaker: trimming context after "
                        f"{_consecutive_parse_fails} failures")
                    sys_msgs = [m for m in mission.conversation
                                if m.get("role") == "system"]
                    recent = mission.conversation[-4:]  # last 2 user/assistant pairs
                    mission.conversation = sys_msgs + recent

                mission.conversation.append({"role": "user", "content":
                    f"Could not parse your response as JSON. {diag}\n"
                    "You MUST respond with ONLY a raw JSON object, no markdown, no code fences:\n"
                    "{\"thinking\": \"...\", \"actions\": [...]}"
                })
                time.sleep(1)
                continue

            _consecutive_parse_fails = 0  # Reset on successful parse
            _consecutive_sr_errors = 0    # Reset on successful SR response

            action_results = []
            completed = False
            for action in parsed.get("actions", []):
                atype = action.get("type", "")

                if atype == "complete":
                    result = execute_action(mission, action)
                    if result.get("ok"):
                        completed = True
                        break
                    else:
                        # Verification gate or deliverable check failed —
                        # feed the error back to SR so it can fix and retry.
                        msg = result.get("message") or result.get("error") or "Completion blocked"
                        action_results.append(f"complete: {msg}")
                else:
                    result = execute_action(mission, action)
                    result_str = _format_action_result(atype, result, action)
                    if result_str:
                        action_results.append(result_str)
                    # Track shell output for plateau detection
                    # Only track test-like commands, not curl/pkill/etc.
                    if atype == "shell" and isinstance(result, dict):
                        cmd = (action.get("command") or "").lower()
                        _is_test_cmd = (
                            "test" in cmd or "pytest" in cmd
                            or "unittest" in cmd or "mocha" in cmd
                            or "jest" in cmd or "check" in cmd
                            or cmd.strip().endswith(".sh")
                        )
                        if _is_test_cmd:
                            _, is_plateau, count = _track_test_output(
                                result, _recent_test_hashes)
                            if is_plateau:
                                _plateau_rounds += 1
                                if _plateau_rounds == _PLATEAU_THRESHOLD:
                                    mission.log_event("PLATEAU_DETECTED",
                                        f"Test output identical for {count} "
                                        f"consecutive runs (plateau_rounds={_plateau_rounds})")
                            else:
                                _plateau_rounds = 0

            if completed:
                break

            # Track SR actions for spin-loop detection
            for action in parsed.get("actions", []):
                atype = action.get("type", "")
                if atype == "shell":
                    _recent_sr_actions.append(("shell", action.get("command", "").strip()))
                elif atype == "wait_for_flock":
                    _recent_sr_actions.append(("wait", "wait_for_flock"))
                elif atype == "read_file":
                    # Include line range to distinguish sequential reads of same file
                    path = action.get("path", "")
                    start = action.get("start_line") or action.get("start") or ""
                    end = action.get("end_line") or action.get("end") or ""
                    if start or end:
                        _recent_sr_actions.append(("read", f"{path}:{start}-{end}"))
                    else:
                        _recent_sr_actions.append(("read", path))
            _recent_sr_actions = _recent_sr_actions[-12:]  # keep last 12

            if action_results:
                feedback = "Action results:\n" + "\n\n".join(action_results)
                if len(feedback) > _ACTION_RESULT_HARD_CAP * 2:
                    feedback = feedback[:_ACTION_RESULT_HARD_CAP * 2] + "\n[... truncated]"
                mission.conversation.append({"role": "user", "content": feedback})

            # Force auto-complete if plateau persists beyond tolerance
            if _plateau_rounds >= _AUTO_COMPLETE_PLATEAU_ROUNDS + 3:
                plan = getattr(mission, "plan", None)
                if plan and plan.tasks:
                    done = sum(1 for t in plan.tasks if t.status == "done")
                    total = len(plan.tasks)
                    if done >= total and round_num >= _PLATEAU_MIN_ROUND:
                        mission.log_event("AUTO_COMPLETE",
                            f"Forced completion: {done}/{total} tasks done, "
                            f"test plateau for {_plateau_rounds} rounds, "
                            f"SR did not self-complete")
                        break

            time.sleep(1)

        else:
            mission.log_event("WARN", f"Exhausted {_MAX_ROUNDS} rounds")
            mission.status_message = "Round limit reached — completing"

        # If we exhausted rounds with zero real work, mark as failed
        _any_work_done = (mission.round_trips > 1 or mission.tasks_completed > 0
                          or (mission.plan and any(
                              t.status == "done" for t in mission.plan.tasks)))
        if not _any_work_done:
            mission.status = "failed"
            mission.status_message = (
                f"Mission failed: {_MAX_ROUNDS} rounds exhausted with no work completed "
                f"(SR errors={_consecutive_sr_errors}, re-elections={_sr_reelections})")
            mission.log_event("FAILED", mission.status_message)
            mission.log_event("INFO", f"Mission loop ended (status={mission.status})")
            return

        _advance_phase(mission, "completing")
        _phase_completing(mission)

        mission.log_event("INFO", f"Mission loop ended (status={mission.status})")

    except Exception as e:
        mission.log_event("ERROR",
                          f"Mission loop crashed: {e}\n{traceback.format_exc()}")
        mission.status = "error"
        mission.status_message = f"Internal error: {e}"


# ── Phase: Initialize ────────────────────────────────────────────────────

def _phase_initialize(mission):
    """Set up container, elect showrunner, build flock."""
    resuming = mission.container_id is not None

    if not resuming:
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

        manifest_raw = _container_read_file(cid, "/home/mission/tools/manifest.json")
        if manifest_raw:
            try:
                mission.tools = json.loads(manifest_raw)
                mission.log_event("INFO", f"Loaded {len(mission.tools)} bootstrapped tools")
            except (ValueError, TypeError):
                pass

    mission.status_message = "Electing Showrunner..."
    ok = _elect_or_override_showrunner(mission)
    if not ok:
        mission.log_event("ERROR", "No suitable Showrunner found")
        mission.status = "error"
        mission.status_message = "No healthy endpoints for Showrunner"
        return

    mission.status_message = "Building flock..."
    update_flock(mission)

    mission.status = "running"
    mission.status_message = "Mission active"

    if not resuming:
        _container_write_file(mission.container_id, "/home/mission/mission.txt",
                              mission.mission_text)
    elif mission.mission_version > 1:
        _container_write_file(mission.container_id, "/home/mission/mission.txt",
                              mission.mission_text)

    from .memory import init_working_memory
    init_working_memory(mission.container_id)

    if resuming:
        mission.log_event("INFO", f"Resumed in phase: {mission.mission_phase}")


def _phase_completing(mission):
    """Final sync, mark completed."""
    mission.status_message = "Completing mission..."
    mission.status_progress = 92

    _sync_plan_from_state(mission)

    if mission.container_id and not mission._has_result:
        out, _, rc = _container_exec(
            mission.container_id,
            "test -f /home/mission/result.html && echo yes",
            timeout=5)
        if rc == 0 and "yes" in (out or ""):
            mission._has_result = True

    try:
        from .memory import auto_extract_memories
        extracted = auto_extract_memories(mission)
        if extracted:
            mission.log_event("MEMORY", f"Auto-extracted {extracted} long-term memories")
    except Exception as e:
        mission.log_event("WARN", f"Memory extraction failed: {e}")

    if mission.container_id:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        _container_exec(mission.container_id,
                        f"echo '\\n=== MISSION COMPLETE ===\\n{ts}\\n' "
                        ">> /home/mission/mission_log.md",
                        timeout=5)

    summary = "Mission completed."
    if mission.plan:
        done = sum(1 for t in mission.plan.tasks if t.status == "done")
        total = len(mission.plan.tasks)
        summary = f"Mission completed: {done}/{total} tasks done."
    mission.status = "completed"
    mission.status_message = summary
    mission.status_progress = 100
    mission.log_event("COMPLETE", summary)


# ── Flock offers (post-plan, pre-working) ────────────────────────────────

def _collect_flock_offers(mission):
    """Ask each flock agent what they can contribute, collect responses.

    Runs synchronously between initialization and the working phase.
    Each agent gets the mission text and is asked what they can help with.
    Prompts are trimmed to fit each agent's context window.
    Timeout per agent is calculated from their TPS and expected response length + 75%.
    """
    from .showrunner import send_prompt_to_endpoint
    from .scoring import estimate_tokens
    import orchestrator as orch_mod

    agents = [(name, a) for name, a in mission.flock.items()
              if a.status == "available"]
    if not agents:
        mission._flock_offers_collected = True
        return

    mission.status_message = "Gathering flock input..."
    mission.log_event("FLOCK_OFFERS", f"Asking {len(agents)} flock agents what they can contribute")

    # Trim mission text to fit small context windows
    mission_text = mission.mission_text or ""

    # ── 1. Fire all requests in parallel ─────────────────────────────
    _OFFER_DEADLINE = 200  # hard deadline for all offer responses

    for agent_name, agent in agents:
        ctx = agent.context_length or 2048
        tps = agent.toks_per_sec or 10

        # Budget: leave room for system (~200 tok) + response (~200 tok) + margin
        prompt_budget_chars = max(400, (ctx - 500) * 4)  # 4 chars/token rough

        brief = mission_text[:prompt_budget_chars]
        if len(mission_text) > prompt_budget_chars:
            brief = mission_text[:prompt_budget_chars - 20] + "\n[... truncated]"

        messages = [
            {"role": "system", "content":
             f"You are {agent_name}, a {agent.experience or 'junior'} "
             f"{agent.role or 'developer'}. Answer concisely in 2-3 sentences. "
             f"Do NOT use <think> tags. Reply directly."},
            {"role": "user", "content":
             f"/no_think\nMISSION:\n{brief}\n\n"
             f"The lead developer (Showrunner) will write all the code. "
             f"You are a support member. Given your role as {agent.role or 'developer'}, "
             f"what could you specifically contribute to help this mission succeed? "
             f"Be concrete — mention specific files, tests, reviews, or research you could do. "
             f"Keep it to 2-3 sentences."},
        ]

        try:
            orch_task_id, _ = send_prompt_to_endpoint(
                agent.node_id, agent.model, messages,
                mission.mission_id, f"offer-{agent_name}",
                role="utility",
                overrides={"max_tokens": 512},
            )
            mission._flock_offers_pending[agent_name] = {
                "orch_task_id": orch_task_id,
                "timeout": _OFFER_DEADLINE,
                "started_at": time.time(),
                "role": agent.role or "developer",
                "experience": agent.experience or "junior",
                "node_id": agent.node_id,
            }
            mission.log_event("FLOCK_OFFERS",
                f"Sent offer request to {agent_name} on {agent.node_id} "
                f"(deadline={_OFFER_DEADLINE}s, tps={tps:.0f})",
                agent=agent_name)
        except Exception as e:
            mission.log_event("WARN", f"Failed to request offer from {agent_name}: {e}")

    # Trigger immediate push delivery to avoid waiting for next poll cycle
    try:
        import push as push_mod
        import registry as reg_mod
        import threading as _thr
        triggered = set()
        for info in mission._flock_offers_pending.values():
            nid = info.get("node_id", "")
            if nid in triggered:
                continue
            node = reg_mod.get_node(nid)
            if node and node.get("conn_mode") == "push":
                addr = node.get("address")
                tok = node.get("orchestrator_token")
                if addr and tok:
                    _thr.Thread(target=push_mod._poll, args=(nid, addr, tok),
                                daemon=True).start()
                    triggered.add(nid)
        if triggered:
            mission.log_event("FLOCK_OFFERS",
                f"Triggered immediate push delivery to {len(triggered)} node(s)")
    except Exception:
        pass  # push trigger is best-effort

    # ── 2. Wait for all responses (bounded by per-agent timeout) ─────
    if not mission._flock_offers_pending:
        mission._flock_offers_collected = True
        return

    # Hard deadline: 120 seconds for all offers
    global_deadline = time.time() + _OFFER_DEADLINE

    while mission._flock_offers_pending and time.time() < global_deadline:
        done_names = []
        for agent_name, info in list(mission._flock_offers_pending.items()):
            elapsed = time.time() - info["started_at"]

            task_data = orch_mod.get_task(info["orch_task_id"])
            if task_data and task_data["status"] == "done" and task_data["results"]:
                result = task_data["results"][0]
                if result.get("error") or result.get("_agent_error"):
                    mission.log_event("FLOCK_OFFERS",
                        f"{agent_name}: agent error — {str(result.get('error',''))[:120]}",
                        agent=agent_name)
                    done_names.append(agent_name)
                    continue
                text = _extract_reply_text(result)
                if text:
                    mission._flock_offers[agent_name] = {
                        "offer": text[:600],
                        "role": info["role"],
                        "experience": info["experience"],
                    }
                    mission.log_event("FLOCK_OFFERS",
                        f"{agent_name} ({info['role']}): {text[:120]}",
                        agent=agent_name)
                else:
                    mission.log_event("FLOCK_OFFERS",
                        f"{agent_name}: empty response (keys={list(result.keys())[:8]})",
                        agent=agent_name)
                done_names.append(agent_name)

            elif elapsed > info["timeout"]:
                mission.log_event("FLOCK_OFFERS",
                    f"{agent_name} timed out after {elapsed:.0f}s", agent=agent_name)
                done_names.append(agent_name)

        for name in done_names:
            mission._flock_offers_pending.pop(name, None)

        if mission._flock_offers_pending:
            time.sleep(1)

    # Timeout stragglers
    for agent_name in list(mission._flock_offers_pending.keys()):
        mission.log_event("FLOCK_OFFERS",
            f"{agent_name} timed out (global deadline)", agent=agent_name)
    mission._flock_offers_pending.clear()
    mission._flock_offers_collected = True

    if mission._flock_offers:
        mission.log_event("FLOCK_OFFERS",
            f"Collected {len(mission._flock_offers)} offers from flock")
    else:
        mission.log_event("FLOCK_OFFERS", "No offers received from flock")


# ── Flock advice system ──────────────────────────────────────────────────

def _flock_advice_tick(mission, round_num):
    """Check if we should request or collect flock advice. Non-blocking.

    Triggers on the 2nd SR round-trip spent on a milestone.  A milestone is
    the *current working frontier*: the first pending task in the plan (SR
    writes code itself, so tasks stay "pending" until marked "done").

    Any agent that is available and was never personally dispatched real work
    can offer advice.  One agent per milestone, highest-ranking first.
    """
    from .showrunner import send_prompt_to_endpoint
    from .scoring import composite_score
    import orchestrator as orch_mod

    # ── 1. Collect finished advice requests (non-blocking) ───────────
    done_pending = []
    for agent_name, info in list(mission._flock_advice_pending.items()):
        # Non-blocking check: read orch task directly (wait_for_result with
        # timeout=0 never enters its poll loop, so we bypass it).
        task_data = orch_mod.get_task(info["orch_task_id"])
        if task_data and task_data["status"] == "done" and task_data["results"]:
            result = task_data["results"][0]
            text = _extract_reply_text(result)
            if text:
                mission._flock_advice[agent_name] = {
                    "milestone": info["milestone"],
                    "advice": text[:1500],
                    "role": info["role"],
                    "experience": info["experience"],
                }
                mission.log_event("FLOCK_ADVICE",
                    f"{agent_name} ({info['role']}) gave advice on '{info['milestone']}'",
                    agent=agent_name)
            else:
                mission.log_event("FLOCK_ADVICE",
                    f"Advice from {agent_name} returned empty", agent=agent_name)
            done_pending.append(agent_name)
        elif time.time() - info["started_at"] > info["timeout"]:
            mission.log_event("FLOCK_ADVICE",
                f"Advice request to {agent_name} timed out", agent=agent_name)
            done_pending.append(agent_name)
    for name in done_pending:
        mission._flock_advice_pending.pop(name, None)

    # ── 2. Guards for new requests ───────────────────────────────────
    plan = getattr(mission, "plan", None)
    if not plan or not plan.tasks:
        return

    # Build set of agents that were ever dispatched real work
    dispatched_agents = set()
    for t in mission.task_history:
        if t.get("agent_name"):
            dispatched_agents.add(t["agent_name"])
    for tid, t in mission.tasks.items():
        if t.agent_name:
            dispatched_agents.add(t.agent_name)

    advised = set(mission._flock_advice.keys()) | set(mission._flock_advice_pending.keys())
    # Only consider agents that are available, never dispatched, and haven't advised
    available_agents = [
        (name, a) for name, a in mission.flock.items()
        if a.status == "available" and name not in advised and name not in dispatched_agents
    ]
    if not available_agents:
        return

    # ── 3. Find current working frontier ─────────────────────────────
    # The "current milestone" is the first non-done task in the plan.
    # SR works sequentially; tasks go pending → done (rarely "dispatched").
    current_task = None
    for task in plan.tasks:
        if task.status != "done":
            current_task = task
            break

    if not current_task:
        return  # all done

    # Track rounds on the current frontier task
    tracker = mission._advice_milestone_tracker
    if tracker.get("_current") != current_task.id:
        # Frontier shifted — reset counter
        tracker["_current"] = current_task.id
        tracker["_rounds"] = 1
    else:
        tracker["_rounds"] = tracker.get("_rounds", 0) + 1

    # Fire once the SR has spent 2+ rounds on this milestone.
    # Using < 2 (not == 2) so the check survives persistence restores where
    # the counter was already at 2 and gets incremented past it.  The
    # milestone-dedup check below prevents double-firing.
    if tracker.get("_rounds", 0) < 2:
        return

    # Don't request advice on a milestone that already got it
    milestones_with_advice = {v["milestone"] for v in mission._flock_advice.values()}
    milestones_with_advice |= {v["milestone"] for v in mission._flock_advice_pending.values()}
    if current_task.id in milestones_with_advice:
        return

    # ── 4. Pick highest-ranking unused agent ─────────────────────────
    ranked = sorted(available_agents,
                    key=lambda x: composite_score(
                        x[1].toks_per_sec or 10, x[1].model, x[1].context_length or 4096
                    ), reverse=True)
    agent_name, agent = ranked[0]

    # ── 5. Build advice prompt and fire async ────────────────────────
    advice_prompt = (
        f"/no_think\n"
        f"You are {agent_name}, a {agent.role or 'developer'} "
        f"({agent.experience or 'experienced'}) on this mission.\n\n"
        f"MISSION:\n{mission.mission_text}\n\n"
        f"The team lead is currently working on this milestone:\n"
        f"  \"{current_task.title}\"\n\n"
        f"Based on your expertise, give brief, practical advice for this milestone. "
        f"Focus on: pitfalls to avoid, best practices, things easily overlooked, "
        f"and any creative suggestions. Be concise (2-4 paragraphs max)."
    )
    messages = [
        {"role": "system", "content":
         f"You are {agent_name}, {agent.role or 'a developer'}. "
         f"Give practical advice based on your expertise. Be concise and actionable. "
         f"Do NOT use <think> tags. Reply directly."},
        {"role": "user", "content": advice_prompt},
    ]

    try:
        orch_task_id, wait_timeout = send_prompt_to_endpoint(
            agent.node_id, agent.model, messages,
            mission.mission_id, f"advice-{agent_name}",
            role="worker",
        )
        mission._flock_advice_pending[agent_name] = {
            "orch_task_id": orch_task_id,
            "timeout": min(wait_timeout, 180),
            "started_at": time.time(),
            "milestone": current_task.id,
            "role": agent.role or "developer",
            "experience": agent.experience or "experienced",
        }
        mission.log_event("FLOCK_ADVICE",
            f"Requesting advice from {agent_name} ({agent.role}) "
            f"on milestone '{current_task.title}'",
            agent=agent_name)
    except Exception as e:
        mission.log_event("WARN", f"Failed to request advice from {agent_name}: {e}")


def _collect_flock_advice(mission):
    """Return formatted advice strings for injection into SR user content, then clear them."""
    if not mission._flock_advice:
        return []
    advice_parts = []
    delivered = []
    for agent_name, info in mission._flock_advice.items():
        if info.get("_delivered"):
            continue
        role = info.get("role", "developer")
        exp = info.get("experience", "experienced")
        milestone = info.get("milestone", "?")
        advice_text = info.get("advice", "")
        if advice_text:
            advice_parts.append(
                f"=== Advice from {agent_name}, {exp} {role} "
                f"regarding '{milestone}' ===\n{advice_text}"
            )
            delivered.append(agent_name)
    for name in delivered:
        mission._flock_advice[name]["_delivered"] = True
    return advice_parts


# ── Spin-loop detection ──────────────────────────────────────────────────

def _detect_spin_loop(recent_actions, mission):
    """Detect if the Showrunner is stuck in a repetitive loop.

    Returns a nudge string if a spin-loop is detected, else None.
    Triggers when the same action is repeated 3+ times in the last 8 actions.
    """
    if len(recent_actions) < 4:
        return None

    # Count repeated action signatures in recent history
    from collections import Counter
    sig_counts = Counter(recent_actions[-8:])
    worst_sig, worst_count = sig_counts.most_common(1)[0]

    if worst_count >= 3:
        atype, detail = worst_sig
        # Check if SR is waiting on a busy flock task
        busy_tasks = [t for t in mission.tasks.values() if t.status == "running"]
        if busy_tasks and atype in ("shell", "read", "wait"):
            task_names = ", ".join(f"{t.agent_name}:{t.task_id}" for t in busy_tasks)
            mission.log_event("SPIN_LOOP",
                f"SR repeated '{atype}: {detail[:60]}' {worst_count}x — "
                f"waiting on busy tasks: {task_names}")
            return (
                f"⚠ SPIN-LOOP DETECTED: You've repeated the same action "
                f"({atype}: {detail[:60]}) {worst_count} times while waiting for flock results. "
                f"STOP waiting. Take over the work yourself — you are the lead developer. "
                f"Write the code or file directly. If a dispatched task is stuck, cancel it "
                f'with {{"type": "cancel_task", "task_id": "..."}} and do the work yourself.'
            )
        else:
            mission.log_event("SPIN_LOOP",
                f"SR repeated '{atype}: {detail[:60]}' {worst_count}x — no busy tasks")
            return (
                f"⚠ SPIN-LOOP DETECTED: You've repeated the same action "
                f"({atype}: {detail[:60]}) {worst_count} times. "
                f"This pattern is wasting round-trips. Try a different approach: "
                f"write the code directly, use a different command, or move on to the next task."
            )

    return None


# ── Test output plateau detection ────────────────────────────────────────

def _track_test_output(result, recent_hashes):
    """Track shell output for plateau detection. Returns (hash, is_plateau, count)."""
    import hashlib
    stdout = result.get("stdout", "") if isinstance(result, dict) else ""
    if not stdout or len(stdout) < 20:
        return None, False, 0
    # Hash the output (ignore timestamps and PIDs)
    import re as _re
    cleaned = _re.sub(r'\b\d{2}:\d{2}:\d{2}\b', 'TS', stdout)
    cleaned = _re.sub(r'\bPID\s*\d+\b', 'PID', cleaned)
    cleaned = _re.sub(r'\b\d{4,}\b', 'N', cleaned)  # long numbers (PIDs, ports)
    h = hashlib.md5(cleaned.encode()).hexdigest()[:12]
    recent_hashes.append(h)
    if len(recent_hashes) > _PLATEAU_WINDOW:
        recent_hashes[:] = recent_hashes[-_PLATEAU_WINDOW:]
    # Count consecutive identical hashes from the end
    count = 0
    for prev_h in reversed(recent_hashes):
        if prev_h == h:
            count += 1
        else:
            break
    return h, count >= _PLATEAU_THRESHOLD, count


def _check_auto_complete(mission, plateau_rounds, round_num):
    """Check if we should auto-complete: plan done + sustained plateau.
    Returns a nudge string or None."""
    if round_num < _PLATEAU_MIN_ROUND:
        return None
    if plateau_rounds < _AUTO_COMPLETE_PLATEAU_ROUNDS:
        return None
    plan = getattr(mission, "plan", None)
    if not plan or not plan.tasks:
        return None
    done = sum(1 for t in plan.tasks if t.status == "done")
    total = len(plan.tasks)
    if done < total:
        return None
    # All plan tasks done + sustained plateau = auto-complete
    mission.log_event("AUTO_COMPLETE",
        f"All {total} plan tasks done + test output unchanged for "
        f"{plateau_rounds} consecutive rounds — forcing completion")
    return (
        f"⚠ AUTO-COMPLETE: All {total} plan tasks are done and your test output "
        f"has been identical for {plateau_rounds} consecutive rounds. "
        f"The mission deliverables are complete. Stop iterating and call complete now:\n"
        f'{{"type": "complete", "summary": "All {total} deliverables implemented and tested."}}'
    )


def _dispatch_flock_for_plateau(mission, recent_hashes, round_num):
    """When SR is stuck on a test plateau, dispatch idle flock agents to fix issues.
    Returns True if dispatch happened."""
    idle_agents = [
        (name, a) for name, a in mission.flock.items()
        if a.status == "available" and name != "Showrunner"
    ]
    if not idle_agents:
        return False

    # Read the test output from the container to give agents context
    test_output = ""
    if mission.container_id:
        test_output, _, _ = _container_exec(
            mission.container_id,
            "cat /tmp/test_full.txt 2>/dev/null || cat /tmp/test_output.txt 2>/dev/null || echo 'no test output found'",
            timeout=5)
        test_output = (test_output or "")[:2000]

    # Dispatch up to 2 agents for test fixing
    dispatched = 0
    for name, agent in idle_agents[:2]:
        goal = (
            f"Fix failing tests in this project. The test output has plateaued — "
            f"the same tests keep failing despite the Showrunner's attempts to fix them.\n\n"
            f"LATEST TEST OUTPUT:\n{test_output}\n\n"
            f"Review the test failures, find the root causes in the source code, "
            f"and fix the bugs. Focus on making the failing tests pass. "
            f"Read test_suite.sh (or equivalent) and the source files it tests, "
            f"then apply targeted fixes."
        )
        action = {
            "type": "dispatch",
            "agent": name,
            "goal": goal,
            "constraints": {"max_iterations": 15, "timeout": 300},
        }
        result = execute_action(mission, action)
        if result.get("ok"):
            mission.log_event("PLATEAU_DISPATCH",
                f"Dispatched {name} to fix test failures (plateau detected at round {round_num})",
                agent=name)
            dispatched += 1

    return dispatched > 0


# ── Behavioral nudges ────────────────────────────────────────────────────

def _get_behavioral_nudge(mission, round_num):
    """Return a single behavioral nudge or None. At most one per round."""

    idle_agents = [
        (name, a) for name, a in mission.flock.items()
        if a.status == "available" and name != "Showrunner"
    ]
    if idle_agents and round_num >= 8:
        any_dispatched = any(a.last_used > 0 for _, a in idle_agents)
        if not any_dispatched and round_num in (8, 20, 40):
            hints = []
            for name, a in idle_agents[:3]:
                role = a.role or "general"
                hints.append(f"{name} ({role})")
            return (
                f"💡 Your support team is idle: {', '.join(hints)}. "
                "If you have working code, dispatch them to review it for bugs, "
                "write tests, or create documentation. They're here to support YOUR work."
            )

    kb = getattr(mission, "knowledge_base", {}) or {}
    if round_num in (6, 14, 25, 40) and not kb:
        has_memories = any(
            e.get("level") in ("MEMORY", "SAVE_MEMORY")
            for e in mission.event_log[-80:]
        )
        if not has_memories:
            return (
                "⚡ MEMORY WARNING: 0 memories saved after "
                f"{round_num} rounds. save_memory preserves decisions through compaction."
            )

    if round_num in (10, 20, 35):
        has_checkpoint = any(
            e.get("level") == "CHECKPOINT" or "checkpoint" in e.get("message", "").lower()
            for e in mission.event_log
        )
        if not has_checkpoint:
            return (
                "⚡ No git checkpoints yet. Use checkpoint to save progress."
            )

    if round_num in (8, 18, 30):
        plan = getattr(mission, "plan", None)
        if plan and hasattr(plan, "tasks"):
            done_count = sum(1 for t in plan.tasks if t.status == "done")
            if done_count == 0 and len(plan.tasks) > 0:
                return (
                    "⚡ Plan shows 0 completed tasks despite "
                    f"{round_num} rounds. Update task statuses in state.json."
                )

    return None


# ── Format action results ────────────────────────────────────────────────

def _format_action_result(atype, result, action):
    """Format an action result dict into a readable string for the SR conversation."""
    if not result:
        return f"{atype}: no result"

    if isinstance(result, dict):
        if result.get("verification_required"):
            return f"{atype}: {result.get('message', 'verification required')}"

        ok = result.get("ok")
        error = result.get("error")
        content = result.get("content", "")
        output = result.get("output", "")

        if atype == "dispatch":
            if ok:
                return (f"dispatch: task {result.get('task_id', '?')} dispatched to "
                        f"{result.get('agent', '?')}"
                        + (f" ⚠ {result['warning']}" if result.get("warning") else ""))
            return f"dispatch: FAILED — {error}"

        if atype == "shell":
            rc = result.get("exit_code", "?")
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            r = f"shell: rc={rc}"
            if stdout:
                r += f"\n{stdout[:_ACTION_RESULT_HARD_CAP]}"
            if stderr:
                r += f"\nstderr: {stderr[:2000]}"
            return r

        if atype in ("read_file", "batch_read"):
            if ok and content:
                return f"{atype}: {content[:_ACTION_RESULT_HARD_CAP]}"
            return f"{atype}: {error or 'no content'}"

        if atype == "write_file":
            path = action.get("path", "?")
            if ok:
                syntax = result.get("syntax_errors")
                r = f"write_file: {path} OK ({result.get('size', '?')}B)"
                if syntax:
                    r += f" ⚠ SYNTAX ERROR: {syntax}"
                return r
            return f"write_file: {path} FAILED — {error}"

        if atype == "workspace_tree":
            tree = result.get("tree", "")
            return f"workspace_tree:\n{tree[:_ACTION_RESULT_HARD_CAP]}" if tree else "workspace_tree: empty"

        if atype in ("patch_file", "multi_patch"):
            if ok:
                return f"{atype}: OK"
            return f"{atype}: {error or 'failed'}"

        if atype == "search":
            content = result.get("content", "")
            count = result.get("matches", 0)
            if not content:
                return "search: no matches"
            return f"search: ({count} matches)\n{str(content)[:_ACTION_RESULT_HARD_CAP]}"

        if atype == "wait_for_flock":
            return f"wait_for_flock: {result.get('message', 'done')}"

        if atype == "create_result":
            if ok:
                return "create_result: result.html written"
            return f"create_result: {error or 'failed'}"

        if atype == "save_note":
            return f"save_note: saved '{action.get('key', '?')}'"

        if atype == "save_memory":
            return f"save_memory: saved ({action.get('category', '?')})"

        if atype == "recall_memory":
            memories = result.get("memories", "")
            return f"recall_memory: {memories[:3000]}" if memories else "recall_memory: nothing found"

        if atype == "memory_list":
            files = result.get("files", [])
            count = result.get("count", 0)
            if files:
                return f"memory_list: {count} files: {', '.join(files[:20])}"
            return "memory_list: no memory files"

        if atype == "memory_read":
            content = result.get("content", "")
            path = result.get("path", action.get("path", "?"))
            if content:
                return f"memory_read ({path}):\n{content[:_ACTION_RESULT_HARD_CAP]}"
            return f"memory_read: {result.get('error', 'no content')}"

        if atype in ("memory_create", "memory_update", "memory_append"):
            path = result.get("path", action.get("path", "?"))
            size = result.get("size", "?")
            if ok:
                return f"{atype}: {path} OK ({size}B)"
            return f"{atype}: {result.get('error', 'failed')}"

        if atype == "memory_delete":
            path = result.get("path", action.get("path", "?"))
            return f"memory_delete: {path} {'OK' if ok else result.get('error', 'failed')}"

        if ok:
            msg = result.get("message", output or content or "done")
            return f"{atype}: {str(msg)[:_ACTION_RESULT_HARD_CAP]}"
        if error:
            return f"{atype}: ERROR — {error}"
        return f"{atype}: {json.dumps(result)[:800]}"

    return f"{atype}: {str(result)[:800]}"
