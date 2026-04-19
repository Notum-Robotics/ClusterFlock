"""Flock management — naming, role assignment, agent system prompts."""

import json
import re
import secrets
import time

from registry import all_nodes

from .state import FlockAgent, _FLOCK_RENAME_COOLDOWN
from .scoring import model_quality_tier, model_size_label, get_endpoint_ctx
from .showrunner import ask_showrunner
from .prompts import render_agent_system, render_agent_lite, render_naming_prompt


# ── Flock update & assignment ────────────────────────────────────────────

def update_flock(mission):
    """Scan cluster endpoints and update flock assignments. Returns True if changes made."""
    now = time.time()
    if now - mission.flock_last_update < _FLOCK_RENAME_COOLDOWN and mission.flock:
        return False

    nodes = all_nodes()
    current_endpoints = []
    for node in nodes:
        if node.get("status") == "dead":
            continue
        for ep in node.get("endpoints", []):
            if ep.get("status") not in ("ready", "sleeping") or not ep.get("model"):
                continue
            if (node["node_id"] == mission.showrunner_node_id and
                    ep["model"] == mission.showrunner_model):
                continue
            model_lower = ep["model"].lower()
            if any(tag in model_lower for tag in ("-vl-", "-vl.", "_vl_", "_vl.", "vl-", "vision")):
                continue
            ep_id = f"{node['node_id']}:{ep['model']}"
            current_endpoints.append({
                "endpoint_id": ep_id,
                "node_id": node["node_id"],
                "hostname": node.get("hostname", ""),
                "model": ep["model"],
                "toks_per_sec": ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0,
                "context_length": ep.get("context_length") or 0,
                "gpu_name": ep.get("gpu") or ep.get("gpu_name") or "",
            })

    named_ep_ids = {a.endpoint_id for a in mission.flock.values()}
    unnamed = [ep for ep in current_endpoints if ep["endpoint_id"] not in named_ep_ids]

    # Update tps for existing agents
    ep_by_id = {ep["endpoint_id"]: ep for ep in current_endpoints}
    for name, agent in mission.flock.items():
        ep_data = ep_by_id.get(agent.endpoint_id)
        if ep_data and ep_data["toks_per_sec"] > 0 and ep_data["toks_per_sec"] != agent.toks_per_sec:
            old_tps = agent.toks_per_sec
            agent.toks_per_sec = ep_data["toks_per_sec"]
            if old_tps == 0:
                mission.log_event("FLOCK", f"Agent {name} benchmark: {agent.toks_per_sec} tok/s")

    # Grace period for disappeared endpoints
    _FLOCK_GRACE_PERIOD = 600
    active_ep_ids = {ep["endpoint_id"] for ep in current_endpoints}
    gone = set()
    for name, agent in mission.flock.items():
        if agent.endpoint_id not in active_ep_ids:
            gone.add(name)
    for name in gone:
        agent = mission.flock.pop(name)
        mission._departed_flock[agent.endpoint_id] = (agent, now)
        cancelled_tasks = []
        for tid, task in list(mission.tasks.items()):
            if task.agent_name == name and task.status in ("pending", "running"):
                task._cancel_event.set()
                task.status = "failed"
                task.error = f"Agent {name} departed (endpoint offline)"
                task.completed_at = now
                mission.task_history.append(task.to_dict())
                del mission.tasks[tid]
                cancelled_tasks.append(tid)
        if cancelled_tasks:
            mission.log_event("FLOCK",
                              f"Agent {name} departed — cancelled {len(cancelled_tasks)} task(s): "
                              f"{', '.join(cancelled_tasks)}", agent=name)
        else:
            mission.log_event("FLOCK",
                              f"Agent {name} departed (endpoint offline) — retain for {_FLOCK_GRACE_PERIOD}s")

    # Expire old departed entries
    expired = [eid for eid, (_, ts) in mission._departed_flock.items()
               if now - ts > _FLOCK_GRACE_PERIOD]
    for eid in expired:
        agent, _ = mission._departed_flock.pop(eid)
        mission.log_event("FLOCK", f"Agent {agent.name} permanently removed")

    # Restore returned agents
    restored = []
    for ep in current_endpoints:
        if ep["endpoint_id"] in mission._departed_flock and ep["endpoint_id"] not in named_ep_ids:
            agent, _ = mission._departed_flock.pop(ep["endpoint_id"])
            agent.status = "available"
            agent.toks_per_sec = ep.get("toks_per_sec", agent.toks_per_sec)
            agent.context_length = ep.get("context_length", agent.context_length)
            mission.flock[agent.name] = agent
            named_ep_ids.add(ep["endpoint_id"])
            restored.append(agent.name)
            mission.log_event("FLOCK", f"Agent {agent.name} restored (back online)")

    if restored:
        unnamed = [ep for ep in current_endpoints if ep["endpoint_id"] not in named_ep_ids]

    # Filter tier-1 when enough tier-2+ agents exist
    if unnamed:
        tier2_plus = sum(1 for a in mission.flock.values() if model_quality_tier(a.model) >= 2)
        if tier2_plus >= 2:
            filtered = []
            for ep in unnamed:
                if model_quality_tier(ep["model"]) < 2:
                    mission.log_event("FLOCK",
                                      f"Excluding tier-1 {ep['model']} — {tier2_plus} tier-2+ agents in flock")
                else:
                    filtered.append(ep)
            unnamed = filtered

    if not unnamed:
        return bool(gone)

    # Ask SR to name them
    prompt = render_naming_prompt(
        mission_text=mission.mission_text[:500],
        endpoints=[{
            "model": ep["model"],
            "tier_label": _tier_label(ep["model"]),
            "gpu": ep.get("gpu_name", "?"),
            "tps": ep.get("toks_per_sec", "?"),
            "ctx": ep.get("context_length", "?"),
        } for ep in unnamed],
        existing_names=list(mission.flock.keys()),
    )
    names = _parse_flock_naming_response(mission, prompt, unnamed)
    for i, ep in enumerate(unnamed):
        entry = names[i] if i < len(names) else {}
        name = entry.get("name", f"Agent-{len(mission.flock) + 1}")
        role = entry.get("role", "general assistant")
        experience = entry.get("experience", "unknown")
        job_desc = entry.get("job_description", "")
        sys_prompt = generate_agent_identity_prompt(name, role, experience, job_desc, ep["model"])
        mission.flock[name] = FlockAgent(
            endpoint_id=ep["endpoint_id"], node_id=ep["node_id"],
            hostname=ep["hostname"], model=ep["model"], name=name,
            role=role, experience=experience,
            toks_per_sec=ep.get("toks_per_sec", 0),
            context_length=ep.get("context_length", 0),
            gpu_name=ep.get("gpu_name", ""),
            system_prompt=sys_prompt,
        )
        mission.log_event("FLOCK", f"Named agent: {name} — {role} ({experience}) = {ep['model']}")

    mission.flock_last_update = now
    return True


def reassign_flock_roles(mission):
    """Re-assign mission-specific roles to all flock agents."""
    if not mission.flock:
        return
    endpoints = []
    agent_order = []
    for name, agent in mission.flock.items():
        endpoints.append({
            "model": agent.model,
            "tier_label": _tier_label(agent.model),
            "gpu": agent.gpu_name or "?",
            "tps": agent.toks_per_sec,
            "ctx": agent.context_length or "?",
        })
        agent_order.append((name, agent))

    prompt = render_naming_prompt(
        mission_text=mission.mission_text[:500],
        endpoints=endpoints,
        existing_names=[],
    )
    raw_endpoints = [{"endpoint_id": a.endpoint_id, "model": a.model} for _, a in agent_order]
    names = _parse_flock_naming_response(mission, prompt, raw_endpoints)

    new_flock = {}
    existing_names = set()
    for i, (old_name, agent) in enumerate(agent_order):
        entry = names[i] if i < len(names) else {}
        new_name = entry.get("name", old_name)
        while new_name in existing_names:
            new_name = new_name + "-" + secrets.token_hex(2)
        existing_names.add(new_name)
        agent.name = new_name
        agent.role = entry.get("role", agent.role)
        agent.experience = entry.get("experience", agent.experience)
        job_desc = entry.get("job_description", "")
        agent.system_prompt = generate_agent_identity_prompt(
            new_name, agent.role, agent.experience, job_desc, agent.model)
        new_flock[new_name] = agent
        if new_name != old_name:
            mission.log_event("FLOCK", f"Reassigned: {old_name} → {new_name} — {agent.role}")
        else:
            mission.log_event("FLOCK", f"Reassigned: {new_name} — {agent.role}")

    mission.flock = new_flock
    mission.flock_last_update = time.time()


# ── Naming response parsing ──────────────────────────────────────────────

def _parse_flock_naming_response(mission, prompt, endpoints):
    response_text = ask_showrunner(mission, prompt)
    if not response_text:
        return _flock_fallback_names(len(endpoints), set(mission.flock.keys()))

    mission.log_event("DEBUG", f"Naming raw ({len(response_text)} chars): {response_text[:1500]}")

    naming_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL).strip()
    if not naming_text:
        naming_text = response_text
    fence_m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)```', naming_text)
    if fence_m:
        naming_text = fence_m.group(1).strip()

    parsed = None
    try:
        parsed = json.loads(naming_text)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', naming_text, re.DOTALL)
        if not match:
            match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if parsed and isinstance(parsed, dict):
        for key in ("names", "agents", "flock", "endpoints", "assignments"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0]:
                    parsed = v
                    break

    if not isinstance(parsed, list):
        parsed = []

    valid = [e for e in parsed if isinstance(e, dict) and e.get("name")]
    if not valid and parsed:
        valid = _extract_name_objects(response_text)
    if valid:
        parsed = valid

    existing_names = set(mission.flock.keys())
    result = []
    for i in range(len(endpoints)):
        if i < len(parsed) and isinstance(parsed[i], dict):
            name = str(parsed[i].get("name", "")).strip()
            role = str(parsed[i].get("role", parsed[i].get("specialty", ""))).strip()
            experience = str(parsed[i].get("experience", "")).strip().lower()
            if not name:
                name = f"Agent-{len(existing_names) + 1}"
            if not role:
                role = "general assistant"
            role = role[:50]
            if experience not in ("junior", "intermediate", "senior", "expert"):
                experience = "intermediate"
            job_description = str(parsed[i].get("job_description", "")).strip()
            if not job_description:
                job_description = f"You are a {experience}-level {role}. Complete tasks thoroughly."
            while name in existing_names:
                name = name + "-" + secrets.token_hex(2)
            existing_names.add(name)
            result.append({"name": name, "role": role, "experience": experience,
                           "job_description": job_description})
        else:
            fallback = _flock_fallback_names(1, existing_names)[0]
            existing_names.add(fallback["name"])
            result.append(fallback)
    return result


def _extract_name_objects(text):
    results = []
    for m in re.finditer(r'\{[^{}]*"name"\s*:\s*"[^"]+?"[^{}]*\}', text):
        try:
            obj = json.loads(m.group(0))
            if obj.get("name"):
                results.append(obj)
        except json.JSONDecodeError:
            pass
    return results


_FALLBACK_NAMES = ["Alex", "Sam", "Robin", "Casey", "Morgan", "Riley", "Jordan", "Taylor"]


def _flock_fallback_names(count, existing_names):
    result = []
    used = set(existing_names)
    idx = 0
    for _ in range(count):
        while idx < len(_FALLBACK_NAMES) and _FALLBACK_NAMES[idx] in used:
            idx += 1
        name = _FALLBACK_NAMES[idx] if idx < len(_FALLBACK_NAMES) else f"Agent-{len(used) + 1}"
        used.add(name)
        result.append({"name": name, "role": "general assistant", "experience": "intermediate"})
        idx += 1
    return result


# ── Agent system prompts ─────────────────────────────────────────────────

def generate_agent_identity_prompt(name, role, experience, job_description, model):
    """Generate the identity section for a flock agent (stored on FlockAgent.system_prompt)."""
    tier = model_quality_tier(model)
    capability = "large and powerful" if tier >= 3 else "capable and efficient" if tier >= 2 else "fast and lightweight"
    return (
        f"You are {name}, a {experience}-level {role}.\n\n"
        f"IDENTITY & PURPOSE:\n{job_description}\n\n"
        f"CHAIN OF COMMAND:\n"
        f"You report to the Showrunner — a higher-intelligence orchestrator model that manages "
        f"the overall mission. Follow the Showrunner's instructions precisely. If a task is "
        f"ambiguous, do your best interpretation and clearly state your assumptions.\n\n"
        f"YOUR CAPABILITIES:\n"
        f"You are running on {model} ({capability}). Work within your strengths. "
        f"Be thorough, precise, and take pride in your work.\n\n"
        f"WORK ETHIC:\n"
        f"- Deliver complete, working solutions — not sketches or placeholders\n"
        f"- If you encounter an error or blocker, explain it clearly\n"
        f"- Include your reasoning when the task involves judgment calls\n"
        f"- Never fabricate data, URLs, or file contents — if unsure, say so"
    )


def build_agent_system_prompt(agent, mission=None):
    """Build the full system prompt for a flock agent — tier-adapted."""
    tier = model_quality_tier(agent.model)

    if tier < 2:
        return render_agent_lite(
            agent={"name": agent.name, "role": agent.role, "experience": agent.experience,
                   "system_prompt": agent.system_prompt or ""},
            mission_text=mission.mission_text[:500] if mission else "",
            scratchpad=agent.scratchpad if isinstance(agent.scratchpad, dict) else {},
        )

    kb = {}
    plan_context = ""
    tools = []
    if mission:
        kb = getattr(mission, "knowledge_base", None) or {}
        tools = mission.tools or []
        plan = getattr(mission, "plan", None)
        if plan:
            plan_parts = [f"MISSION: {mission.mission_text[:500]}",
                          f"PLAN PROGRESS: {plan.progress_summary()}"]
            done_tasks = [t for t in plan.tasks if t.status == "done" and t.result]
            if done_tasks:
                plan_parts.append("COMPLETED TASKS:")
                for t in done_tasks[-10:]:
                    plan_parts.append(f"  - {t.title}: {(t.result or '')[:150]}")
            plan_context = "\n".join(plan_parts)

    return render_agent_system(
        agent={"name": agent.name, "role": agent.role, "experience": agent.experience,
               "model": agent.model, "system_prompt": agent.system_prompt or ""},
        mission_text=mission.mission_text[:500] if mission else "",
        tier=tier,
        scratchpad=agent.scratchpad if isinstance(agent.scratchpad, dict) else {},
        knowledge_base=kb,
        plan_context=plan_context,
        tools=tools,
        phase=getattr(mission, "mission_phase", "") if mission else "",
    )


# ── Helpers ──────────────────────────────────────────────────────────────

def _tier_label(model):
    tier = model_quality_tier(model)
    size = model_size_label(model)
    if tier >= 3:
        return f"tier-3 large ({size})"
    if tier >= 2:
        return f"tier-2 medium ({size})"
    return f"tier-1 small ({size})"
