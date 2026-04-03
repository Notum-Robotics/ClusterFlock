"""Flock management — naming, role assignment, agent system prompts."""

import json
import re
import secrets
import time

from registry import all_nodes

from .state import (
    FlockAgent,
    _FLOCK_RENAME_COOLDOWN,
)
from .scoring import (
    _model_quality_tier,
    _get_endpoint_ctx,
)
from .showrunner import _ask_showrunner


# ── Flock naming prompts ─────────────────────────────────────────────────

def _build_flock_naming_prompt(mission, endpoints, reassign=False):
    """Build prompt asking Showrunner to assign mission-specific identities.
    When reassign=True, re-assigns roles to ALL endpoints for the current mission."""
    if reassign:
        lines = [
            "The mission has changed. Reassign roles to ALL flock members for this mission.",
        ]
    else:
        lines = [
            "New AI endpoints have joined the flock. Assign each a unique identity.",
        ]

    lines += [
        "",
        f"MISSION: {mission.mission_text[:500]}",
        "",
        "Assign each endpoint:",
        "- name: a SHORT human first name (one word, e.g. Jenna, David, Mira)",
        "- role: what this agent does for THIS mission (under 50 chars)",
        "- experience: junior, intermediate, senior, or expert",
        "- job_description: a 2-4 sentence paragraph describing this agent's responsibilities,",
        "  strengths, and how they fit into the team. Be specific to the mission. This will be",
        "  sent to the agent with every task so they understand their identity and purpose.",
        "",
        "Examples:",
        '  {"name": "Jenna", "role": "genetic data analyst", "experience": "expert",',
        '   "job_description": "You are the team\'s genetics specialist. Your primary responsibility is parsing FASTA/FASTQ data, running sequence alignments, and identifying mutations. You excel at bioinformatics pipelines and should apply rigorous scientific methodology. When in doubt, validate against reference genomes."}',
        '  {"name": "David", "role": "frontend developer", "experience": "junior",',
        '   "job_description": "You handle UI implementation using HTML, CSS and JavaScript. Focus on clean, accessible markup and responsive layouts. Ask clarifying questions in your response if requirements are ambiguous. Your work will be reviewed by the Showrunner, so include comments explaining your design choices."}',
        "",
    ]

    if not reassign and mission.flock:
        lines.append("Already assigned agents (do NOT rename these):")
        for name, agent in mission.flock.items():
            lines.append(f"  - {name}: {agent.role} ({agent.experience}) = {agent.model}")
        lines.append("")

    lines.append("Endpoints to assign:")
    for ep in endpoints:
        lines.append(f"  - model={ep['model']}, gpu={ep.get('gpu_name', '?')}, "
                     f"toks/s={ep.get('toks_per_sec', '?')}, ctx={ep.get('context_length', '?')}")

    lines.append("")
    lines.append("IMPORTANT: Respond with ONLY a raw JSON array. No wrapping object, no thinking, no explanation.")
    lines.append("Names MUST be unique single human first names.")
    lines.append('[{"name": "...", "role": "...", "experience": "...", "job_description": "..."}, ...]')
    lines.append("One entry per endpoint, same order as listed above.")

    return "\n".join(lines)


# ── Flock update & assignment ────────────────────────────────────────────

def _update_flock(mission):
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
            if ep.get("status") != "ready" or not ep.get("model"):
                continue
            # Skip the Showrunner
            if (node["node_id"] == mission.showrunner_node_id and
                    ep["model"] == mission.showrunner_model):
                continue
            # Skip vision/VL models — they can't produce structured agent responses
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

    # Find unnamed endpoints
    named_ep_ids = {a.endpoint_id for a in mission.flock.values()}
    unnamed = [ep for ep in current_endpoints if ep["endpoint_id"] not in named_ep_ids]

    # Update tokens_per_sec for existing agents (benchmarks may arrive after naming)
    ep_by_id = {ep["endpoint_id"]: ep for ep in current_endpoints}
    for name, agent in mission.flock.items():
        ep_data = ep_by_id.get(agent.endpoint_id)
        if ep_data and ep_data["toks_per_sec"] > 0 and ep_data["toks_per_sec"] != agent.toks_per_sec:
            old_tps = agent.toks_per_sec
            agent.toks_per_sec = ep_data["toks_per_sec"]
            if old_tps == 0:
                mission.log_event("FLOCK", f"Agent {name} benchmark: {agent.toks_per_sec} tok/s")

    # Grace period for disappeared endpoints — move to departed, not delete
    _FLOCK_GRACE_PERIOD = 600  # 10 minutes before permanent removal
    active_ep_ids = {ep["endpoint_id"] for ep in current_endpoints}
    gone = set()
    for name, agent in mission.flock.items():
        if agent.endpoint_id not in active_ep_ids:
            gone.add(name)
    for name in gone:
        agent = mission.flock.pop(name)
        mission._departed_flock[agent.endpoint_id] = (agent, now)

        # Cancel any active tasks assigned to this departed agent
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
                              f"Agent {name} departed — cancelled {len(cancelled_tasks)} active task(s): "
                              f"{', '.join(cancelled_tasks)}. Reassign to available agents.",
                              agent=name)
        else:
            mission.log_event("FLOCK",
                              f"Agent {name} departed (endpoint offline) — "
                              f"will retain identity for {_FLOCK_GRACE_PERIOD}s")

    # Expire old departed entries
    expired = [eid for eid, (_, ts) in mission._departed_flock.items()
               if now - ts > _FLOCK_GRACE_PERIOD]
    for eid in expired:
        agent, _ = mission._departed_flock.pop(eid)
        mission.log_event("FLOCK", f"Agent {agent.name} permanently removed after grace period")

    # Restore any departed agents whose endpoints came back
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
            mission.log_event("FLOCK", f"Agent {agent.name} restored (endpoint back online) — role: {agent.role}")

    # Recalculate unnamed after restoration
    if restored:
        unnamed = [ep for ep in current_endpoints if ep["endpoint_id"] not in named_ep_ids]

    if not unnamed:
        return bool(gone)

    # Ask Showrunner to name them
    prompt = _build_flock_naming_prompt(mission, unnamed)
    names = _parse_flock_naming_response(mission, prompt, unnamed)
    for i, ep in enumerate(unnamed):
        entry = names[i] if i < len(names) else {}
        name = entry.get("name", f"Agent-{len(mission.flock) + 1}")
        role = entry.get("role", "general assistant")
        experience = entry.get("experience", "unknown")
        job_desc = entry.get("job_description", "")
        sys_prompt = _generate_agent_system_prompt(name, role, experience, job_desc, ep["model"])
        mission.flock[name] = FlockAgent(
            endpoint_id=ep["endpoint_id"],
            node_id=ep["node_id"],
            hostname=ep["hostname"],
            model=ep["model"],
            name=name,
            role=role,
            experience=experience,
            toks_per_sec=ep.get("toks_per_sec", 0),
            context_length=ep.get("context_length", 0),
            gpu_name=ep.get("gpu_name", ""),
            system_prompt=sys_prompt,
        )
        mission.log_event("FLOCK", f"Named agent: {name} — {role} ({experience}) = {ep['model']}")

    mission.flock_last_update = now
    return True


def _reassign_flock_roles(mission):
    """Re-assign mission-specific roles to all flock agents (e.g. after mission text changes)."""
    if not mission.flock:
        return
    # Build endpoint list in flock order
    endpoints = []
    agent_order = []  # track (name, agent) to update in-place
    for name, agent in mission.flock.items():
        endpoints.append({
            "endpoint_id": agent.endpoint_id,
            "node_id": agent.node_id,
            "hostname": agent.hostname,
            "model": agent.model,
            "toks_per_sec": agent.toks_per_sec,
            "context_length": agent.context_length,
            "gpu_name": agent.gpu_name,
        })
        agent_order.append((name, agent))

    prompt = _build_flock_naming_prompt(mission, endpoints, reassign=True)
    names = _parse_flock_naming_response(mission, prompt, endpoints)

    # Rebuild flock dict with new names/roles (preserving runtime state)
    new_flock = {}
    existing_names = set()
    for i, (old_name, agent) in enumerate(agent_order):
        entry = names[i] if i < len(names) else {}
        new_name = entry.get("name", old_name)
        # Ensure unique
        while new_name in existing_names:
            new_name = new_name + "-" + secrets.token_hex(2)
        existing_names.add(new_name)

        agent.name = new_name
        agent.role = entry.get("role", agent.role)
        agent.experience = entry.get("experience", agent.experience)
        job_desc = entry.get("job_description", "")
        agent.system_prompt = _generate_agent_system_prompt(
            new_name, agent.role, agent.experience, job_desc, agent.model)
        new_flock[new_name] = agent
        if new_name != old_name:
            mission.log_event("FLOCK", f"Reassigned: {old_name} → {new_name} — {agent.role} ({agent.experience})")
        else:
            mission.log_event("FLOCK", f"Reassigned: {new_name} — {agent.role} ({agent.experience})")

    mission.flock = new_flock
    mission.flock_last_update = time.time()


# ── Naming response parsing ──────────────────────────────────────────────

def _parse_flock_naming_response(mission, prompt, endpoints):
    """Send naming prompt to Showrunner and parse the JSON array response.
    Returns a list of dicts with 'name', 'role', 'experience' keys.
    Length matches endpoints (fills with fallbacks if parsing fails)."""
    response_text = _ask_showrunner(mission, prompt)
    if not response_text:
        return _flock_fallback_names(len(endpoints), set(mission.flock.keys()))

    mission.log_event("DEBUG", f"Naming raw response ({len(response_text)} chars): {response_text[:1500]}")

    # Strip think tags and code fences
    naming_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL).strip()
    if not naming_text:
        naming_text = response_text
    fence_m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)```', naming_text)
    if fence_m:
        naming_text = fence_m.group(1).strip()

    # Try parsing JSON
    parsed = None
    try:
        parsed = json.loads(naming_text)
    except json.JSONDecodeError:
        # Try extracting array from response
        match = re.search(r'\[.*\]', naming_text, re.DOTALL)
        if not match:
            match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if parsed and isinstance(parsed, dict):
        # Look for a list value — try known keys first, then any list
        for key in ("names", "agents", "flock", "endpoints", "assignments"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        if isinstance(parsed, dict):
            # Find any key whose value is a list of dicts with "name"
            for v in parsed.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0]:
                    parsed = v
                    break

    if not isinstance(parsed, list):
        parsed = []

    # Validate entries have expected "name" key; discard invalid ones
    valid = [e for e in parsed if isinstance(e, dict) and e.get("name")]
    if not valid and parsed:
        # List had entries but none with "name" — try regex extraction from raw text
        valid = _extract_name_objects(response_text)
    if valid:
        parsed = valid

    mission.log_event("DEBUG", f"Naming parsed {len(parsed)} entries: {json.dumps(parsed, default=str)[:400]}")

    # Validate and sanitize each entry
    existing_names = set(mission.flock.keys())
    result = []
    for i in range(len(endpoints)):
        if i < len(parsed) and isinstance(parsed[i], dict):
            name = str(parsed[i].get("name", "")).strip()
            role = str(parsed[i].get("role", parsed[i].get("specialty", ""))).strip()
            experience = str(parsed[i].get("experience", "")).strip().lower()
            # Sanitize
            if not name:
                name = f"Agent-{len(existing_names) + 1}"
            if not role:
                role = "general assistant"
            role = role[:50]  # enforce 50 char limit
            if experience not in ("junior", "intermediate", "senior", "expert"):
                experience = "intermediate"
            job_description = str(parsed[i].get("job_description", "")).strip()
            if not job_description:
                job_description = f"You are a {experience}-level {role}. Complete tasks thoroughly and report back."
            # Ensure unique name
            while name in existing_names:
                name = name + "-" + secrets.token_hex(2)
            existing_names.add(name)
            result.append({"name": name, "role": role, "experience": experience, "job_description": job_description})
        else:
            fallback = _flock_fallback_names(1, existing_names)[0]
            existing_names.add(fallback["name"])
            result.append(fallback)

    return result


def _extract_name_objects(text):
    """Try to extract {"name": ..., "role": ..., "experience": ...} objects from free text.
    Used as fallback when the main JSON parse fails."""
    results = []
    for m in re.finditer(r'\{[^{}]*"name"\s*:\s*"[^"]+?"[^{}]*\}', text):
        try:
            obj = json.loads(m.group(0))
            if obj.get("name"):
                results.append(obj)
        except json.JSONDecodeError:
            pass
    return results


def _flock_fallback_names(count, existing_names):
    """Generate fallback names when Showrunner naming fails."""
    _FALLBACK_NAMES = ["Alex", "Sam", "Robin", "Casey", "Morgan", "Riley", "Jordan", "Taylor"]
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

def _generate_agent_system_prompt(name, role, experience, job_description, model):
    """Generate a persistent, elaborate system prompt stored on the FlockAgent."""
    tier = _model_quality_tier(model)
    capability = "large and powerful" if tier >= 3 else "capable and efficient" if tier >= 2 else "fast and lightweight"
    base = (
        f"You are {name}, a {experience}-level {role}.\n\n"
        f"IDENTITY & PURPOSE:\n"
        f"{job_description}\n\n"
        f"CHAIN OF COMMAND:\n"
        f"You report to the Showrunner — a higher-intelligence orchestrator model that manages "
        f"the overall mission. The Showrunner assigns you tasks, reviews your output, and "
        f"coordinates your work with other agents in the flock. Follow the Showrunner's "
        f"instructions precisely. If a task is ambiguous, do your best interpretation and "
        f"clearly state your assumptions in your response.\n\n"
        f"YOUR CAPABILITIES:\n"
        f"You are running on {model} ({capability}). Work within your strengths. "
        f"Be thorough, precise, and take pride in your work. Your output will be verified "
        f"by the Showrunner, so accuracy matters more than speed.\n\n"
        f"WORK ETHIC:\n"
        f"- Deliver complete, working solutions — not sketches or placeholders\n"
        f"- If you encounter an error or blocker, explain it clearly so the Showrunner can help\n"
        f"- Include your reasoning when the task involves judgment calls\n"
        f"- Never fabricate data, URLs, or file contents — if unsure, say so"
    )
    return base


def _build_agent_system_prompt(agent):
    """Build the full system prompt for a flock agent — tier-adapted for model size."""
    base = agent.system_prompt or (
        f"You are {agent.name}, a {agent.experience}-level {agent.role}. "
        f"You are part of a coordinated AI flock reporting to a Showrunner. "
        f"Be thorough, precise, and take pride in your work."
    )

    tier = _model_quality_tier(agent.model)

    if tier < 2:
        # Simplified prompt for small models — fewer action types, shorter examples
        return (
            base + "\n\n"
            "Respond with ONLY this JSON — no other text:\n"
            '{"thinking":"what you will do","actions":[...]}\n\n'
            "Action types:\n"
            '- {"type":"shell","command":"ls -la /home/mission/"}\n'
            '- {"type":"write_file","path":"/home/mission/file.py","content":"..."}\n'
            '- {"type":"read_file","path":"/home/mission/file.py"}\n'
            '- {"type":"done","summary":"what was accomplished"}\n\n'
            "RULES: Raw JSON only. No markdown. No text outside the JSON.\n"
            "Double quotes only. Escape newlines as \\n in strings.\n"
            "All files go under /home/mission/.\n"
            "When done, use the done action.\n"
        )

    return (
        base + "\n\n"
        "Respond with EXACTLY this JSON structure — nothing else, no markdown, no text before or after:\n"
        "{\n"
        '  "thinking": "one sentence about what you will do next",\n'
        '  "actions": [\n'
        '    {"type": "shell", "command": "ls -la /home/mission/"},\n'
        '    {"type": "write_file", "path": "/home/mission/script.js", "content": "..."},\n'
        '    {"type": "read_file", "path": "/home/mission/output.txt"},\n'
        '    {"type": "read_file", "path": "/home/mission/big.py", "start_line": 50, "end_line": 120},\n'
        '    {"type": "batch_read", "paths": ["/home/mission/a.py", "/home/mission/b.py"]},\n'
        '    {"type": "workspace_tree", "path": "/home/mission/"},\n'
        '    {"type": "patch_file", "path": "/home/mission/app.py", '
        '"old": "return 404", "new": "return 200"},\n'
        '    {"type": "search", "pattern": "error", "path": "/home/mission/"},\n'
        '    {"type": "done", "summary": "what was accomplished"}\n'
        "  ]\n"
        "}\n\n"
        "CRITICAL — respond with valid JSON only. Common mistakes to avoid:\n"
        "- Do NOT wrap in ```json ... ```. Just raw { } \n"
        "- Do NOT add text before or after the JSON\n"
        "- Do NOT use single quotes — JSON requires double quotes\n"
        "- Escape special chars in strings: newlines as \\n, quotes as \\\"\n\n"
        "WORKFLOW — follow this loop:\n"
        "1. INSPECT first: read_file, workspace_tree, shell 'ls', search for patterns\n"
        "2. ACT: write code or run commands based on what you found\n"
        "3. VERIFY: check output, read result files, look at exit codes\n"
        "4. Repeat until done, then use {\"type\": \"done\", \"summary\": \"...\"}\n\n"
        "RULES:\n"
        "- All files go under /home/mission/\n"
        "- NEVER guess at file contents or structure — always read/inspect first\n"
        "- Use patch_file for small edits instead of rewriting entire files\n"
        "- Use read_file with start_line/end_line for large files instead of reading everything\n"
        "- Use batch_read to read multiple files at once, workspace_tree for project overview\n"
        "- If a command fails, read the error and try a DIFFERENT approach\n"
        "- Shell timeout: {\"type\":\"shell\",\"command\":\"...\",\"timeout\":300} (up to 600s)\n\n"
        "CONTAINER: Ubuntu 24.04 with curl, wget, python3, pip3, nodejs, npm, jq, git.\n"
        "- Playwright (Node.js) may be pre-installed — use Node.js, NOT Python.\n"
        "- ALWAYS use headless:true — no display available.\n"
    )
