"""Showrunner election, context building, prompting, and conversation compaction."""

import json
import re
import secrets
import time

from registry import all_nodes, get_node, correct_endpoint_ctx
import orchestrator as orch_mod

from .state import (
    _COMPACTION_INTERVAL,
    _COMPACTION_TIMEOUT,
    _PREFLIGHT_HEADROOM,
    _PREFLIGHT_MIN_HISTORY,
    _MAX_CONTEXT_RETRIES,
    _CHARS_PER_TOKEN,
)
from .scoring import (
    _model_quality_tier,
    _composite_score,
    _generation_limits,
    _context_budget,
    _estimate_tokens,
    _is_context_overflow,
    _get_endpoint_tps,
    _get_endpoint_ctx,
    _scaled_limits,
    _estimate_conversation_tokens,
)
from .container import (
    _container_read_file,
    _container_exec,
    _container_write_file,
    _build_workspace_tree,
)
from .prompts import _SHOWRUNNER_SYSTEM
from .prompts.builder import build_phase_section, build_knowledge_section


# ── Showrunner election ──────────────────────────────────────────────────

def _elect_showrunner(exclude_node_id=None):
    """Pick the best endpoint as Showrunner.
    Prefers models from starred nodes; falls back to all nodes if none qualify.
    Returns (node_id, model, ep_dict, score, hostname) or None."""
    import orchestrator as orch_mod
    stars = orch_mod.starred_nodes()
    nodes = all_nodes()

    def _best_from(only_starred=False):
        best = None
        best_score = -1
        for node in nodes:
            if node.get("status") == "dead":
                continue
            if exclude_node_id and node["node_id"] == exclude_node_id:
                continue
            if only_starred and node["node_id"] not in stars:
                continue
            for ep in node.get("endpoints", []):
                if ep.get("status") != "ready" or not ep.get("model"):
                    continue
                tier = _model_quality_tier(ep["model"])
                if tier < 2:
                    continue
                tps = ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0
                ctx = ep.get("context_length") or 0
                if tps == 0:
                    tps = 10
                score = _composite_score(tps, ep["model"], ctx)
                if score > best_score:
                    best_score = score
                    best = (node["node_id"], ep["model"], ep, score, node.get("hostname", ""))
        return best

    if stars:
        result = _best_from(only_starred=True)
        if result:
            return result
    return _best_from()


def _find_endpoint(node_id, model):
    """Find a specific endpoint by node_id and model. Returns same tuple as _elect_showrunner or None."""
    nodes = all_nodes()
    for node in nodes:
        if node["node_id"] != node_id:
            continue
        if node.get("status") == "dead":
            return None
        for ep in node.get("endpoints", []):
            if ep.get("model") == model and ep.get("status") == "ready":
                tps = ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 10
                ctx = ep.get("context_length") or 0
                score = _composite_score(tps, model, ctx)
                return (node_id, model, ep, score, node.get("hostname", ""))
    return None


# ── Prompt dispatch to orchestrator ──────────────────────────────────────

def _send_prompt_to_endpoint(node_id, model, messages, mission_id, task_id,
                             role="worker", overrides=None):
    """Send a prompt to a specific endpoint via the orchestrator command queue.

    role / overrides control generation limits — see _generation_limits().
    Returns (orch_task_id, wait_timeout)."""
    orch_task_id = "mpt-" + secrets.token_hex(6)

    max_tokens, gen_timeout, wait_timeout = _generation_limits(
        node_id, model, role, overrides
    )

    cmd = {
        "action": "prompt",
        "task_id": orch_task_id,
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "generation_timeout": gen_timeout,
        "ttl": gen_timeout * 3 + 60,
        "locked": True,  # don't auto-swap models
        "tight_pack": orch_mod.is_tight_pack(),
        "mission_id": mission_id,
    }

    orch_mod.enqueue(node_id, cmd)

    # Register task in orchestrator
    with orch_mod._lock:
        orch_mod._tasks[orch_task_id] = {
            "status": "pending",
            "expected": 1,
            "results": [],
            "created": time.time(),
        }

    return orch_task_id, wait_timeout


def _wait_for_result(orch_task_id, timeout=120):
    """Poll orchestrator for task result. Returns result dict or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = orch_mod.get_task(orch_task_id)
        if task and task["status"] == "done" and task["results"]:
            result = task["results"][0]
            # Surface agent-side errors immediately (don't mask with success)
            if result.get("_agent_error"):
                return result
            return result
        time.sleep(1.0)
    return None


# ── Context building ─────────────────────────────────────────────────────

def _build_showrunner_context(mission, include_history=True):
    """Build the full context for the Showrunner prompt.
    Dynamically sized based on the Showrunner's loaded context_length.
    When include_history=False, skip RECENT HISTORY (used with multi-turn messages)."""
    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)

    parts = [_SHOWRUNNER_SYSTEM]

    # Phase-specific workflow guidance
    mission_phase = getattr(mission, "mission_phase", "planning")
    parts.append(f"\n{build_phase_section(mission_phase)}")

    # Timestamp and mission text (always included)
    ts = time.strftime('%Y-%m-%d %H:%M:%S %Z')
    elapsed_min = (time.time() - mission.created_at) / 60
    parts.append(f"\n=== CURRENT TIME: {ts} | Mission elapsed: {elapsed_min:.0f}min | Phase: {mission_phase} ===")
    parts.append(f"\n=== MISSION (v{mission.mission_version}) ===\n{mission.mission_text}\n")

    # Available agents with rich detail
    if mission.flock:
        parts.append("\n=== AVAILABLE AGENTS ===\n")
        for name, agent in mission.flock.items():
            status_str = agent.status
            if agent.assigned_task:
                status_str = f"busy (task {agent.assigned_task})"
            tps = agent.toks_per_sec or 0
            speed_label = "fast" if tps > 50 else "moderate" if tps > 20 else "slow" if tps > 0 else "unknown speed"
            tier = _model_quality_tier(agent.model)
            quality_label = "large/smart" if tier >= 3 else "medium" if tier >= 2 else "small/fast"
            parts.append(
                f"- {name}: {agent.role} ({agent.experience}, {quality_label})\n"
                f"    model={agent.model}, {tps} tok/s ({speed_label}), "
                f"ctx={agent.context_length or '?'}, gpu={agent.gpu_name or '?'}, "
                f"status={status_str}, failures={agent.failures}"
            )
        parts.append("")

    # Showrunner info with context budget awareness
    if mission.showrunner_model:
        sr_tps = 0
        if mission.showrunner_node_id:
            node = get_node(mission.showrunner_node_id)
            if node:
                for ep in node.get("endpoints", []):
                    if ep.get("model") == mission.showrunner_model:
                        sr_tps = ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0
        budget_kb = _context_budget(sr_ctx) // 1024
        parts.append(f"=== YOU (SHOWRUNNER) ===\n"
                     f"model={mission.showrunner_model}, {sr_tps} tok/s, "
                     f"score={mission.showrunner_score:.1f}, "
                     f"context={sr_ctx} tokens, content_budget≈{budget_kb}KB\n")

    # Full recursive workspace tree (cached — rebuilds every 30s)
    if mission.container_id:
        now = time.time()
        if now - mission._workspace_tree_at > 30 or not mission._workspace_tree_cache:
            mission._workspace_tree_cache = _build_workspace_tree(mission.container_id)
            mission._workspace_tree_at = now
        tree = mission._workspace_tree_cache
        if tree:
            parts.append("=== WORKSPACE TREE (/home/mission/) ===")
            parts.append(tree)
            parts.append("")

    # Container environment reference
    if mission.container_id:
        parts.append(
            "=== CONTAINER ENVIRONMENT ===\n"
            "Ubuntu 24.04 \u2022 Python 3.12 \u2022 Node.js 18+ \u2022 git \u2022 npm \u2022 curl \u2022 wget \u2022 jq \u2022 ctags\n"
            "Install: python3 -m pip install <pkg> | apt-get install -y <pkg>\n"
            "Pre-installed tools: outline, lint, test, search_def, verify, diff_since\n"
        )

    # Tool manifest
    if mission.tools:
        parts.append("\n=== AVAILABLE TOOLS ===\n")
        for t in mission.tools:
            parts.append(f"- {t['name']}: {t['description']}")
        parts.append("")

    # Persistent scratchpad — always visible
    if mission.notes:
        parts.append("=== YOUR NOTES (scratchpad) ===")
        for note in mission.notes:
            parts.append(f"- {note['key']}: {note['value']}")
        parts.append("")

    # Auto-inject state.json — the showrunner's task tracker
    if mission.container_id:
        state_content = _container_read_file(mission.container_id, "/home/mission/state.json")
        if state_content and state_content.strip() not in ("", "null", "[]", "{}"):
            state_preview = state_content[:limits["action_result_max"]]
            parts.append("=== STATE.JSON (task tracker) ===")
            parts.append(state_preview)
            if len(state_content) > len(state_preview):
                parts.append(f"[truncated — {len(state_content)} total chars]")
            parts.append("")

    # Shared knowledge base — visible to all agents
    kb = getattr(mission, "knowledge_base", None)
    if kb:
        parts.append(build_knowledge_section(kb))

    # Active tasks with FULL real-time checkpoint detail
    if mission.tasks:
        parts.append("=== ACTIVE TASKS ===")
        for tid, task in mission.tasks.items():
            elapsed = time.time() - task.created_at
            parts.append(f"- {tid}: {task.agent_name} ({task.status}, {elapsed:.0f}s)")
            if task.checkpoint:
                cp = task.checkpoint
                cp_status = cp.get("status", "working")
                parts.append(
                    f"    iter={cp.get('iteration', '?')}/{cp.get('max_iterations', '?')}, "
                    f"shells={cp.get('shell_commands', 0)}, status={cp_status}\n"
                    f"    last_action: {cp.get('last_action', 'n/a')}\n"
                    f"    files_created: {', '.join(cp.get('files_written', [])) or 'none'}"
                )
        parts.append("")

    # Completed tasks summary (recent — use scaled limit)
    recent_completed = [t for t in mission.task_history[-10:] if not t.get("_reported_to_sr")]
    if recent_completed:
        result_limit = limits["agent_result_max"]
        parts.append("=== RECENT COMPLETED TASKS ===")
        for t in recent_completed:
            result_preview = (t.get("result") or t.get("error") or "no output")[:result_limit]
            parts.append(f"- {t.get('agent_name', '?')}: {t.get('status', '?')} — {result_preview}")
        parts.append("")

    # Tiered history: last summary + recent raw conversation
    if include_history:
        if mission.last_summary:
            parts.append(f"\n=== PROGRESS SUMMARY (round-trip {mission.round_trips}) ===\n")
            parts.append(mission.last_summary)
        # Always include recent raw exchanges even if summary exists
        if mission.conversation:
            window = limits["conversation_window"]
            recent = mission.conversation[-window:]
            if recent:
                parts.append("\n=== RECENT HISTORY ===\n")
                conv_char_limit = limits["agent_result_max"]
                for msg in recent:
                    parts.append(f"[{msg.get('role', '?')}]: {msg.get('content', '')[:conv_char_limit]}")
    else:
        # Without history in system prompt, still include summary for anchoring
        if mission.last_summary:
            parts.append(f"\n=== PROGRESS SUMMARY (round-trip {mission.round_trips}) ===\n")
            parts.append(mission.last_summary)

    # Pending user responses
    if mission.user_responses:
        parts.append("\n=== USER RESPONSES ===\n")
        for resp in mission.user_responses:
            parts.append(f"[USER @ {resp.get('time_str', '?')}]: {resp.get('response', '')}")
        parts.append("")

    # Mission changed flag
    if mission.mission_version > 1:
        parts.append(f"\n⚠ MISSION TEXT UPDATED (now v{mission.mission_version}). "
                     "Review the mission text above and decide: pivot, complete current tasks then pivot, or ignore.\n")

    return "\n".join(parts)


# ── Ask the Showrunner ───────────────────────────────────────────────────

def _ask_showrunner(mission, user_content, multi_turn=False):
    """Send a message to the Showrunner and get a response.

    Three layers of context-overflow protection:
      L1 — Pre-flight: estimate tokens, trim conversation window & user content to fit.
      L2 — Catch & retry: if llama-server returns exceed_context_size_error, trim more
            and retry once with the *same* showrunner (no re-election).
      (L3 is handled externally — continuous token tracking triggers compaction early.)
    """
    if not mission.showrunner_node_id or not mission.showrunner_model:
        return None

    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model) or 32768
    token_ceiling = int(sr_ctx * _PREFLIGHT_HEADROOM)  # 90% of n_ctx

    # ── Build message array with pre-flight trimming (Layer 1) ──

    def _build_messages(window_override=None, user_text=None):
        """Assemble [system, ...history, user] messages that fit within token_ceiling."""
        utext = user_text or user_content
        system_context = _build_showrunner_context(mission, include_history=not multi_turn)
        msgs = [{"role": "system", "content": system_context}]

        history_msgs = []
        if multi_turn and mission.conversation:
            limits = _scaled_limits(sr_ctx)
            default_window = limits["conversation_window"]
            if window_override is not None:
                window = window_override
            elif mission.conversation_window_override and mission.conversation_window_override > default_window:
                window = mission.conversation_window_override
            else:
                window = default_window
            history_msgs = [
                {"role": m["role"], "content": m.get("content", "")}
                for m in mission.conversation[-window:]
            ]

        # Pre-flight: estimate and trim if needed
        est = _estimate_tokens(msgs) + _estimate_tokens(history_msgs)
        user_est = len(utext) // _CHARS_PER_TOKEN + 4
        total_est = est + user_est

        if total_est > token_ceiling:
            overshoot = total_est - token_ceiling

            # Pass 1: drop oldest conversation pairs until it fits
            while overshoot > 0 and len(history_msgs) > _PREFLIGHT_MIN_HISTORY * 2:
                # Remove the two oldest messages (one user + one assistant pair)
                dropped = history_msgs[:2]
                history_msgs = history_msgs[2:]
                freed = sum(len(m.get("content", "")) // _CHARS_PER_TOKEN + 4 for m in dropped)
                overshoot -= freed

            # Pass 2: truncate user content (action results are the bulk)
            if overshoot > 0:
                cut_chars = overshoot * _CHARS_PER_TOKEN
                if len(utext) > cut_chars + 500:
                    utext = utext[:len(utext) - cut_chars]
                    utext += "\n\n[... truncated to fit context window]"

            # Log that we trimmed
            final_est = (_estimate_tokens(msgs) + _estimate_tokens(history_msgs)
                         + len(utext) // _CHARS_PER_TOKEN + 4)
            mission.log_event("CONTEXT",
                              f"Pre-flight trim: {total_est} est tokens → {final_est} "
                              f"(ceiling {token_ceiling}, n_ctx={sr_ctx}, "
                              f"history={len(history_msgs)} msgs)")

        msgs.extend(history_msgs)
        msgs.append({"role": "user", "content": utext})
        return msgs

    messages = _build_messages()

    mission.log_event("DISPATCH", f"Asking Showrunner: {user_content[:200]}...",
                      agent="Showrunner", model=mission.showrunner_model)

    # ── Send and handle response (with Layer 2 retry) ──

    for attempt in range(_MAX_CONTEXT_RETRIES + 1):
        orch_task_id, wait_timeout = _send_prompt_to_endpoint(
            mission.showrunner_node_id,
            mission.showrunner_model,
            messages,
            mission.mission_id,
            "showrunner",
            role="showrunner",
        )

        result = _wait_for_result(orch_task_id, timeout=wait_timeout)

        if not result:
            mission.log_event("ERROR", f"Showrunner timeout after {wait_timeout}s",
                              agent="Showrunner")
            return None

        # ── Layer 2: detect context overflow and retry with trimmed context ──
        is_overflow, n_prompt, n_ctx_reported = _is_context_overflow(result)
        if is_overflow and attempt < _MAX_CONTEXT_RETRIES:
            # Calculate how much to trim
            if n_prompt and n_ctx_reported:
                overshoot_tokens = n_prompt - int(n_ctx_reported * _PREFLIGHT_HEADROOM)
            else:
                overshoot_tokens = sr_ctx // 4  # conservative 25% cut

            # Correct registry if actual n_ctx is lower than what was reported
            if n_ctx_reported and n_ctx_reported < sr_ctx:
                correct_endpoint_ctx(mission.showrunner_node_id,
                                     mission.showrunner_model, n_ctx_reported)
                sr_ctx = n_ctx_reported
                token_ceiling = int(sr_ctx * _PREFLIGHT_HEADROOM)
                mission.log_event("CONTEXT",
                    f"Registry corrected: {mission.showrunner_model} on "
                    f"{mission.showrunner_node_id} ctx {sr_ctx} → {n_ctx_reported}",
                    agent="Showrunner")

            mission.log_event("CONTEXT",
                              f"Context overflow (attempt {attempt+1}): "
                              f"prompt={n_prompt}, n_ctx={n_ctx_reported or sr_ctx}, "
                              f"overshoot≈{overshoot_tokens} tokens — trimming & retrying",
                              agent="Showrunner")

            # Rebuild with much smaller window, truncated user content
            trim_window = min(4, len(mission.conversation))
            trimmed_user = user_content
            chars_to_cut = overshoot_tokens * _CHARS_PER_TOKEN
            if len(trimmed_user) > chars_to_cut + 500:
                trimmed_user = trimmed_user[:len(trimmed_user) - chars_to_cut]
                trimmed_user += "\n\n[... truncated to fit context window]"
            elif len(trimmed_user) > 2000:
                # Cut in half as last resort
                trimmed_user = trimmed_user[:len(trimmed_user) // 2]
                trimmed_user += "\n\n[... truncated to fit context window]"

            messages = _build_messages(window_override=trim_window, user_text=trimmed_user)
            continue  # retry

        # Non-overflow agent error — pass through
        if result.get("_agent_error"):
            mission.log_event("ERROR", f"Showrunner agent error: {result.get('error', 'unknown')}",
                              agent="Showrunner")
            return None

        # ── Extract text — handle both thinking and non-thinking models ──
        choices = result.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            content_sans_think = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip() if content else ""
            if content_sans_think:
                text = content
            elif reasoning:
                text = reasoning
            else:
                text = content
            if text:
                usage = result.get("usage", {})
                tokens = usage.get("total_tokens", 0)
                comp_tokens = usage.get("completion_tokens", 0)
                mission.log_event("RESPONSE",
                                  f"Showrunner responded ({len(text)} chars, {comp_tokens} completion tokens)",
                                  agent="Showrunner", tokens=tokens)
                mission.round_trips += 1
                mission._sr_overflow_streak = 0  # reset on success
                return text

        mission.log_event("ERROR", f"Showrunner bad response: {json.dumps(result)[:300]}",
                          agent="Showrunner")
        return None

    # All retries exhausted (context overflow persisted)
    mission._sr_overflow_streak += 1
    mission.log_event("ERROR",
                      f"Showrunner context overflow persisted after {_MAX_CONTEXT_RETRIES + 1} attempts "
                      f"(streak={mission._sr_overflow_streak})",
                      agent="Showrunner")
    return None


# ── Conversation compaction ──────────────────────────────────────────────

def _compress_agent_history(mission, agent):
    """Summarize an agent's conversation history if it's too long for its context window."""
    agent_ctx = agent.context_length or _get_endpoint_ctx(agent.node_id, agent.model)
    total_chars = sum(len(m.get("content", "")) for m in agent.conversation_history) if agent.conversation_history else 0
    budget_chars = int((agent_ctx or 4096) * _CHARS_PER_TOKEN * 0.6)  # 60% of context for history

    if total_chars <= budget_chars:
        return  # fits fine

    mission.log_event("CONTEXT",
                      f"Compressing {agent.name}'s history ({total_chars} chars → summarizing)",
                      agent=agent.name)

    # Build a summarization prompt from recent history
    history_text = ""
    for msg in agent.conversation_history[-30:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")[:2000]
        history_text += f"[{role}]: {content}\n\n"

    summary_prompt = (
        f"You are {agent.name}. Summarize your work so far in a concise paragraph (200-400 words). "
        f"Focus on: what tasks you completed, what files you created/modified, key decisions, "
        f"and any important context for future work.\n\n"
        f"YOUR CONVERSATION HISTORY:\n{history_text}\n\n"
        f"Respond with ONLY the summary paragraph, no JSON, no formatting."
    )

    messages = [{"role": "user", "content": summary_prompt}]
    orch_task_id, _ = _send_prompt_to_endpoint(
        agent.node_id, agent.model, messages, mission.mission_id, "compress",
        role="utility",
    )
    result = _wait_for_result(orch_task_id, timeout=120)

    if result:
        choices = result.get("choices", [])
        if choices:
            summary = choices[0].get("message", {}).get("content", "")
            if summary:
                agent.conversation_history = [
                    {"role": "user", "content": "Summarize your work so far."},
                    {"role": "assistant", "content": f"PRIOR WORK SUMMARY:\n{summary}"},
                ]
                mission.log_event("CONTEXT",
                                  f"Compressed {agent.name}'s history to summary ({len(summary)} chars)",
                                  agent=agent.name)
                return

    # Compression failed — just truncate
    agent_window = max(10, min(int((agent_ctx or 4096) / 2048), 40))
    agent.conversation_history = agent.conversation_history[-agent_window:]
    mission.log_event("CONTEXT",
                      f"History compression failed for {agent.name} — truncated to {len(agent.conversation_history)} messages",
                      agent=agent.name)


def _compact_conversation(mission):
    """Compact the showrunner's conversation history into a progressive summary.
    Called periodically from the main loop when round_trips crosses a compaction threshold.
    Generates a structured summary of all conversation turns, stores in last_summary,
    then trims the raw conversation array."""
    if not mission.conversation or len(mission.conversation) < 6:
        return  # nothing worth compacting

    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    window = limits["conversation_window"]

    # Don't compact if conversation fits comfortably
    if len(mission.conversation) <= window:
        return

    # Build text from the conversation turns that will be pruned
    prune_count = len(mission.conversation) - window
    old_turns = mission.conversation[:prune_count]

    history_text = ""
    for msg in old_turns[-40:]:  # summarize up to last 40 pruned turns
        role = msg.get("role", "?")
        content = msg.get("content", "")[:3000]
        history_text += f"[{role}]: {content}\n\n"

    existing_summary = mission.last_summary or ""
    summary_prompt = (
        "You are the Showrunner of this mission. Produce a concise STRUCTURED summary "
        "of your progress so far. This summary will be your long-term memory — you will "
        "see it in every future prompt.\n\n"
        "Include:\n"
        "- Key decisions made and why\n"
        "- Tasks completed and their results\n"
        "- Current state of the project (what files exist, what works)\n"
        "- Failed approaches (so you don't repeat them)\n"
        "- What still needs to be done\n\n"
    )
    if existing_summary:
        summary_prompt += f"PREVIOUS SUMMARY (incorporate and update this):\n{existing_summary}\n\n"
    summary_prompt += f"RECENT CONVERSATION TO SUMMARIZE:\n{history_text}\n\n"
    summary_prompt += "Respond with ONLY the summary text. No JSON, no formatting, just clear prose with bullet points."

    messages = [{"role": "user", "content": summary_prompt}]
    mission.log_event("CONTEXT",
                      f"Compacting conversation: {len(mission.conversation)} turns, "
                      f"pruning {prune_count}, keeping {window}")

    orch_task_id, _ = _send_prompt_to_endpoint(
        mission.showrunner_node_id, mission.showrunner_model,
        messages, mission.mission_id, "compact",
        role="utility",
    )
    result = _wait_for_result(orch_task_id, timeout=_COMPACTION_TIMEOUT)

    if result:
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
            if text and len(text) > 50:
                mission.last_summary = text
                mission.last_summary_at = time.time()
                # Prune old turns, keep the recent window
                mission.conversation = mission.conversation[-window:]
                mission.log_event("CONTEXT",
                                  f"Conversation compacted: summary={len(text)} chars, "
                                  f"kept {len(mission.conversation)} recent turns")
                # Write updated log to container
                from .persistence import _write_mission_log_to_container
                _write_mission_log_to_container(mission)
                return

    # Compaction failed — just trim without summary
    mission.conversation = mission.conversation[-window:]
    mission.log_event("CONTEXT",
                      f"Compaction failed — truncated to {len(mission.conversation)} turns (no summary)")
