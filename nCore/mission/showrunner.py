"""Showrunner election, context building, prompting, and conversation compaction."""

import json
import re
import secrets
import time

from registry import all_nodes, get_node, correct_endpoint_ctx
import orchestrator as orch_mod

from .state import (
    _PREFLIGHT_HEADROOM,
    _PREFLIGHT_MIN_HISTORY,
    _MAX_CONTEXT_RETRIES,
    _CHARS_PER_TOKEN,
)
from .scoring import (
    model_quality_tier,
    model_size_label,
    composite_score,
    generation_limits,
    context_budget,
    estimate_tokens,
    is_context_overflow,
    get_endpoint_tps,
    get_endpoint_ctx,
    scaled_limits,
    estimate_conversation_tokens,
)
from .container import (
    _container_read_file,
    _container_exec,
    _container_write_file,
    _build_workspace_tree,
)
from .prompts import render_showrunner_system
from .memory import recall_for_mission, format_working_memory_for_context


# ── Showrunner election ──────────────────────────────────────────────────

def elect_showrunner(exclude_node_id=None, penalties=None):
    """Pick the best endpoint as Showrunner.
    Returns (node_id, model, ep_dict, score, hostname) or None."""
    import orchestrator as orch_mod
    stars = orch_mod.starred_nodes()
    nodes = all_nodes()
    penalties = penalties or {}

    def _runtime_penalty(node_id):
        perf = penalties.get(node_id)
        if not perf:
            return 1.0
        timeouts = perf.get("timeouts", 0)
        if timeouts <= 0:
            return 1.0
        successes = perf.get("successes", 0)
        effective = max(0, timeouts - successes * 0.5)
        if effective <= 0:
            return 1.0
        return 1.0 / (2 ** min(effective, 6))

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
            node_perf = penalties.get(node["node_id"])
            if node_perf and node_perf.get("timeouts", 0) >= 4:
                continue
            for ep in node.get("endpoints", []):
                if ep.get("status") not in ("ready", "sleeping") or not ep.get("model"):
                    continue
                tier = model_quality_tier(ep["model"])
                if tier < 2:
                    continue
                tps = ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0
                if tps == 0:
                    tps = 10
                ctx = ep.get("context_length") or 0
                score = composite_score(tps, ep["model"], ctx) * _runtime_penalty(node["node_id"])
                if score > best_score:
                    best_score = score
                    best = (node["node_id"], ep["model"], ep, score, node.get("hostname", ""))
        return best

    if stars:
        result = _best_from(only_starred=True)
        if result:
            return result
    return _best_from()


def find_endpoint(node_id, model):
    """Find a specific endpoint. Returns same tuple as elect_showrunner or None."""
    nodes = all_nodes()
    for node in nodes:
        if node["node_id"] != node_id:
            continue
        if node.get("status") == "dead":
            return None
        for ep in node.get("endpoints", []):
            if ep.get("model") == model and ep.get("status") in ("ready", "sleeping"):
                tps = ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 10
                ctx = ep.get("context_length") or 0
                score = composite_score(tps, model, ctx)
                return (node_id, model, ep, score, node.get("hostname", ""))
    return None


# ── Prompt dispatch to orchestrator ──────────────────────────────────────

def send_prompt_to_endpoint(node_id, model, messages, mission_id, task_id,
                            role="worker", overrides=None):
    """Send a prompt to a specific endpoint via the orchestrator command queue.
    Returns (orch_task_id, wait_timeout)."""
    orch_task_id = "mpt-" + secrets.token_hex(6)
    max_tokens, gen_timeout, wait_timeout = generation_limits(
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
        "locked": True,
        "tight_pack": orch_mod.is_tight_pack(),
        "mission_id": mission_id,
    }
    orch_mod.enqueue(node_id, cmd)
    with orch_mod._lock:
        orch_mod._tasks[orch_task_id] = {
            "status": "pending",
            "expected": 1,
            "results": [],
            "created": time.time(),
        }
    return orch_task_id, wait_timeout


def wait_for_result(orch_task_id, timeout=120):
    """Poll orchestrator for task result. Returns result dict or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = orch_mod.get_task(orch_task_id)
        if task and task["status"] == "done" and task["results"]:
            return task["results"][0]
        time.sleep(1.0)
    return None


# ── Tiered task history helper ───────────────────────────────────────────

def _tiered_task_history(mission, budget_chars):
    if not mission.task_history:
        return ""
    parts = ["=== TASK HISTORY ==="]
    used = 0
    history = mission.task_history
    _RECENT_COUNT = 5
    failures, recent, older = [], [], []
    recent_start = max(0, len(history) - _RECENT_COUNT)
    for i, t in enumerate(history):
        status = t.get("status", "")
        if status in ("failed", "error", "skipped"):
            failures.append(t)
        elif i >= recent_start:
            recent.append(t)
        else:
            older.append(t)

    for t in failures:
        if used >= budget_chars:
            break
        error = (t.get("error") or t.get("result") or "no detail")[:800]
        tid = t.get("plan_task_id", t.get("agent_name", "?"))
        line = f"- ✗ {tid}: {t.get('status')} — {error}"
        parts.append(line)
        used += len(line)

    for t in recent:
        if used >= budget_chars:
            break
        result = (t.get("result") or "done")[:300]
        tid = t.get("plan_task_id", t.get("agent_name", "?"))
        line = f"- {tid}: {t.get('status', '?')} — {result}"
        parts.append(line)
        used += len(line)

    if older:
        older_lines = []
        for t in older:
            if used >= budget_chars:
                break
            tid = t.get("plan_task_id", t.get("agent_name", "?"))
            entry = f"✓ {tid}"
            older_lines.append(entry)
            used += len(entry) + 3
        if older_lines:
            parts.append("Earlier: " + " | ".join(older_lines))

    if len(parts) <= 1:
        return ""
    parts.append("")
    return "\n".join(parts)


# ── Context building ─────────────────────────────────────────────────────

def build_showrunner_context(mission, include_history=True):
    """Build the full context for the Showrunner prompt via Jinja2 templates."""
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    mission_phase = getattr(mission, "mission_phase", "planning")
    ts = time.strftime('%Y-%m-%d %H:%M:%S %Z')
    elapsed_secs = time.time() - mission.created_at

    # Build agent data
    agents = []
    for name, agent in mission.flock.items():
        status_str = agent.status
        if agent.assigned_task:
            status_str = f"busy (task {agent.assigned_task})"
        tps = agent.toks_per_sec or 0
        speed_label = "fast" if tps > 50 else "moderate" if tps > 20 else "slow" if tps > 0 else "unknown speed"
        tier = model_quality_tier(agent.model)
        size_label = model_size_label(agent.model)
        if tier >= 3:
            quality_label = f"tier-3 large ({size_label})" if size_label else "tier-3 large"
            capability_hint = "complex reasoning, architecture, code generation, debugging"
        elif tier >= 2:
            quality_label = f"tier-2 medium ({size_label})" if size_label else "tier-2 medium"
            capability_hint = "implementation, testing, focused coding tasks"
        else:
            quality_label = f"tier-1 small ({size_label})" if size_label else "tier-1 small"
            capability_hint = "simple file ops, formatting, grep, copying, single-step tasks ONLY"
        perf_note = ""
        _aperf = mission._agent_perf.get(name)
        if _aperf and _aperf.get("total_tasks", 0) > 0:
            _succ = _aperf["task_successes"]
            _total = _aperf["total_tasks"]
            _fail_rate = _aperf["task_failures"] / _total
            if _fail_rate >= 0.5:
                perf_note = f" ⚠ UNDERPERFORMING ({_succ}/{_total} tasks succeeded)"
            elif _total >= 2:
                perf_note = f" track_record={_succ}/{_total}"
        agents.append({
            "name": name, "role": agent.role, "experience": agent.experience,
            "model": agent.model, "tps": tps, "speed_label": speed_label,
            "quality_label": quality_label, "capability_hint": capability_hint,
            "ctx": agent.context_length or "?", "gpu": agent.gpu_name or "?",
            "status": status_str, "failures": agent.failures,
            "perf_note": perf_note,
        })

    # SR info
    sr_tps = 0
    if mission.showrunner_node_id:
        node = get_node(mission.showrunner_node_id)
        if node:
            for ep in node.get("endpoints", []):
                if ep.get("model") == mission.showrunner_model:
                    sr_tps = ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0
    budget_kb = context_budget(sr_ctx) // 1024
    sr_info = {
        "model": mission.showrunner_model,
        "tps": sr_tps,
        "score": mission.showrunner_score,
        "context": sr_ctx,
        "budget_kb": budget_kb,
    }

    # Workspace tree (cached 30s)
    workspace_tree = ""
    if mission.container_id:
        now = time.time()
        if now - mission._workspace_tree_at > 30 or not mission._workspace_tree_cache:
            mission._workspace_tree_cache = _build_workspace_tree(mission.container_id)
            mission._workspace_tree_at = now
        workspace_tree = mission._workspace_tree_cache

    # State.json (cached 30s)
    state_json = ""
    if mission.container_id:
        now_sj = time.time()
        if now_sj - mission._state_json_at > 30 or not mission._state_json_cache:
            mission._state_json_cache = _container_read_file(
                mission.container_id, "/home/mission/state.json") or ""
            mission._state_json_at = now_sj
        state_json = mission._state_json_cache

    # Knowledge base
    kb = getattr(mission, "knowledge_base", None) or {}

    # Working memory
    working_memory = ""
    if mission.container_id:
        working_memory = format_working_memory_for_context(mission.container_id)

    # Long-term memory (cached 5min)
    if not hasattr(mission, '_ltm_cache'):
        mission._ltm_cache = recall_for_mission(mission.mission_text)
        mission._ltm_cache_at = time.time()
    elif time.time() - getattr(mission, '_ltm_cache_at', 0) > 300:
        mission._ltm_cache = recall_for_mission(mission.mission_text)
        mission._ltm_cache_at = time.time()
    long_term_memory = mission._ltm_cache or ""

    # Active tasks
    active_tasks = []
    for tid, task in mission.tasks.items():
        elapsed_t = time.time() - task.created_at
        atask = {"id": tid, "agent": task.agent_name, "status": task.status,
                 "elapsed": elapsed_t}
        if task.checkpoint:
            atask["checkpoint"] = task.checkpoint
        active_tasks.append(atask)

    # Task history (budget-capped)
    total_budget = context_budget(sr_ctx)
    task_history_text = _tiered_task_history(mission, int(total_budget * 0.15))

    # Conversation summary + recent history
    conversation_summary = mission.last_summary or ""
    recent_history = []
    if include_history and mission.conversation:
        window = limits["conversation_window"]
        for msg in mission.conversation[-window:]:
            recent_history.append({
                "role": msg.get("role", "?"),
                "content": msg.get("content", "")[:limits["agent_result_max"]],
            })
    elif not include_history and mission.last_summary:
        pass  # summary is already included

    # Budget allocation
    fixed_estimate = 8000  # rough fixed-section estimate
    remaining = max(4000, total_budget - fixed_estimate)
    budgets = {
        "active": int(remaining * 0.35),
        "history": int(remaining * 0.25),
        "knowledge": int(remaining * 0.20),
        "memory": int(remaining * 0.10),
        "state": int(remaining * 0.10),
    }

    return render_showrunner_system(
        phase=mission_phase,
        mission_text=mission.mission_text,
        mission_version=mission.mission_version,
        timestamp=ts,
        elapsed_secs=elapsed_secs,
        agents=agents,
        sr_info=sr_info,
        workspace_tree=workspace_tree,
        container_env=True,
        tools=mission.tools,
        notes=mission.notes,
        state_json=state_json,
        knowledge_base=kb,
        working_memory=working_memory,
        long_term_memory=long_term_memory,
        active_tasks=active_tasks,
        task_history=task_history_text,
        conversation_summary=conversation_summary,
        recent_history=recent_history,
        user_responses=mission.user_responses,
        budgets=budgets,
    )


# ── Ask the Showrunner ───────────────────────────────────────────────────

def ask_showrunner(mission, user_content, multi_turn=False):
    """Send a message to the SR and get a response. L1/L2 overflow protection."""
    if not mission.showrunner_node_id or not mission.showrunner_model:
        return None

    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model) or 32768
    token_ceiling = int(sr_ctx * _PREFLIGHT_HEADROOM)

    def _build_messages(window_override=None, user_text=None):
        utext = user_text or user_content
        system_context = build_showrunner_context(mission, include_history=not multi_turn)
        msgs = [{"role": "system", "content": system_context}]
        history_msgs = []
        if multi_turn and mission.conversation:
            limits = scaled_limits(sr_ctx)
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
        est = estimate_tokens(msgs) + estimate_tokens(history_msgs)
        user_est = len(utext) // _CHARS_PER_TOKEN + 4
        total_est = est + user_est

        if total_est > token_ceiling:
            overshoot = total_est - token_ceiling
            while overshoot > 0 and len(history_msgs) > _PREFLIGHT_MIN_HISTORY * 2:
                dropped = history_msgs[:2]
                history_msgs = history_msgs[2:]
                freed = sum(len(m.get("content", "")) // _CHARS_PER_TOKEN + 4 for m in dropped)
                overshoot -= freed
            if overshoot > 0:
                cut_chars = overshoot * _CHARS_PER_TOKEN
                if len(utext) > cut_chars + 500:
                    utext = utext[:len(utext) - cut_chars]
                    utext += "\n\n[... truncated to fit context window]"
            final_est = (estimate_tokens(msgs) + estimate_tokens(history_msgs)
                         + len(utext) // _CHARS_PER_TOKEN + 4)
            mission.log_event("CONTEXT",
                              f"Pre-flight trim: {total_est} est → {final_est} "
                              f"(ceiling {token_ceiling}, n_ctx={sr_ctx}, "
                              f"history={len(history_msgs)} msgs)")

        msgs.extend(history_msgs)
        msgs.append({"role": "user", "content": utext})
        return msgs

    messages = _build_messages()

    mission.log_event("DISPATCH", f"Asking Showrunner: {user_content[:200]}...",
                      agent="Showrunner", model=mission.showrunner_model)

    for attempt in range(_MAX_CONTEXT_RETRIES + 1):
        orch_task_id, wait_timeout = send_prompt_to_endpoint(
            mission.showrunner_node_id, mission.showrunner_model,
            messages, mission.mission_id, "showrunner", role="showrunner",
        )
        result = wait_for_result(orch_task_id, timeout=wait_timeout)

        if not result:
            mission.log_event("ERROR", f"Showrunner timeout after {wait_timeout}s",
                              agent="Showrunner")
            return None

        is_overflow_flag, n_prompt, n_ctx_reported = is_context_overflow(result)
        if is_overflow_flag and attempt < _MAX_CONTEXT_RETRIES:
            if n_prompt and n_ctx_reported:
                overshoot_tokens = n_prompt - int(n_ctx_reported * _PREFLIGHT_HEADROOM)
            else:
                overshoot_tokens = sr_ctx // 4

            if n_ctx_reported and n_ctx_reported < sr_ctx:
                correct_endpoint_ctx(mission.showrunner_node_id,
                                     mission.showrunner_model, n_ctx_reported)
                sr_ctx = n_ctx_reported
                token_ceiling = int(sr_ctx * _PREFLIGHT_HEADROOM)

            mission.log_event("CONTEXT",
                              f"Context overflow (attempt {attempt+1}): "
                              f"prompt={n_prompt}, n_ctx={n_ctx_reported or sr_ctx}, "
                              f"overshoot≈{overshoot_tokens} tokens — trimming",
                              agent="Showrunner")

            trim_window = min(4, len(mission.conversation))
            trimmed_user = user_content
            chars_to_cut = overshoot_tokens * _CHARS_PER_TOKEN
            if len(trimmed_user) > chars_to_cut + 500:
                trimmed_user = trimmed_user[:len(trimmed_user) - chars_to_cut]
                trimmed_user += "\n\n[... truncated to fit context window]"
            elif len(trimmed_user) > 2000:
                trimmed_user = trimmed_user[:len(trimmed_user) // 2]
                trimmed_user += "\n\n[... truncated to fit context window]"

            messages = _build_messages(window_override=trim_window, user_text=trimmed_user)
            continue

        if result.get("_agent_error"):
            mission.log_event("ERROR",
                              f"Showrunner agent error: {result.get('error', 'unknown')}",
                              agent="Showrunner")
            return None

        choices = result.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            content_sans_think = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip() if content else ""
            text = content if content_sans_think else (reasoning or content)
            if text:
                usage = result.get("usage", {})
                comp_tokens = usage.get("completion_tokens", 0)
                mission.log_event("RESPONSE",
                                  f"Showrunner responded ({len(text)} chars, {comp_tokens} completion tokens)",
                                  agent="Showrunner", tokens=usage.get("total_tokens", 0))
                mission.round_trips += 1
                mission._sr_overflow_streak = 0
                return text

        mission.log_event("ERROR", f"Showrunner bad response: {json.dumps(result)[:300]}",
                          agent="Showrunner")
        return None

    mission._sr_overflow_streak += 1
    mission.log_event("ERROR",
                      f"Showrunner context overflow persisted after {_MAX_CONTEXT_RETRIES + 1} attempts "
                      f"(streak={mission._sr_overflow_streak})",
                      agent="Showrunner")
    return None


# ── Conversation compaction ──────────────────────────────────────────────

def compress_agent_history(mission, agent):
    """Summarize an agent's conversation history if too long for its context window."""
    agent_ctx = agent.context_length or get_endpoint_ctx(agent.node_id, agent.model)
    total_chars = sum(len(m.get("content", "")) for m in agent.conversation_history) if agent.conversation_history else 0
    budget_chars = int((agent_ctx or 4096) * _CHARS_PER_TOKEN * 0.6)

    if total_chars <= budget_chars:
        return

    if (agent_ctx or 4096) < 8192:
        agent_window = max(6, min(int((agent_ctx or 4096) / 2048), 10))
        agent.conversation_history = agent.conversation_history[-agent_window:]
        mission.log_event("CONTEXT",
                          f"Trimmed {agent.name}'s history to {len(agent.conversation_history)} msgs "
                          f"(small context {agent_ctx})", agent=agent.name)
        return

    mission.log_event("CONTEXT",
                      f"Compressing {agent.name}'s history ({total_chars} chars → summarizing)",
                      agent=agent.name)

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
    orch_task_id, _ = send_prompt_to_endpoint(
        agent.node_id, agent.model, messages, mission.mission_id, "compress",
        role="utility",
    )
    result = wait_for_result(orch_task_id, timeout=240)

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

    agent_window = max(10, min(int((agent_ctx or 4096) / 2048), 40))
    agent.conversation_history = agent.conversation_history[-agent_window:]
    mission.log_event("CONTEXT",
                      f"History compression failed for {agent.name} — truncated to {len(agent.conversation_history)} msgs",
                      agent=agent.name)
