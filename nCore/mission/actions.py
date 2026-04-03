"""Showrunner action handlers — registry-dispatched execution of all action types."""

import json
import secrets
import shlex
import threading
import time

from .state import (
    AgentTask,
    _lock,
    _PROMPT_STACK_MAX,
    _AUTO_TIMEOUT,
    _AUTO_MAX_ITERATIONS,
    _SHELL_TIMEOUT_DEFAULT,
    _SHELL_TIMEOUT_INSTALL,
)
from .scoring import (
    _model_quality_tier,
    _score_task_complexity,
    _find_better_agent,
    _get_endpoint_ctx,
    _scaled_limits,
)
from .container import (
    _container_exec,
    _container_write_file,
    _container_read_file,
    _build_workspace_tree,
    _smart_truncate,
)
from .showrunner import _compress_agent_history
from .flock import _generate_agent_system_prompt
from .agent_loop import _agent_autonomous_loop


# ── Individual action handlers ───────────────────────────────────────────

def _action_dispatch(mission, action):
    """Dispatch a task to a named agent — always runs as autonomous loop with tools."""
    agent_name = action.get("agent", "")
    goal = action.get("goal", "") or action.get("prompt", "")
    constraints = action.get("constraints", {})
    context = action.get("context", "")

    agent = mission.flock.get(agent_name)
    if not agent:
        # Try to find by partial/bidirectional match
        req = agent_name.lower()
        for name, a in mission.flock.items():
            nl = name.lower()
            if req in nl or nl in req:
                agent = a
                agent_name = name
                break
        if not agent:
            return {"ok": False, "error": f"agent '{agent_name}' not found"}

    if agent.status == "busy":
        return {"ok": False, "error": f"agent '{agent_name}' is already busy"}

    # Build the full prompt with goal + context
    prompt_parts = [f"GOAL: {goal}"]
    if context:
        prompt_parts.append(f"\nCONTEXT: {context}")
    if constraints.get("success_criteria"):
        prompt_parts.append(f"\nSUCCESS CRITERIA: {constraints['success_criteria']}")
    if constraints.get("working_dir"):
        prompt_parts.append(f"\nWORKING DIRECTORY: {constraints['working_dir']}")
    # Inject mission time context so agent can plan accordingly
    elapsed_min = (time.time() - mission.created_at) / 60
    prompt_parts.append(
        f"\nTIME CONTEXT: Mission has been running {elapsed_min:.0f} minutes. "
        f"Work efficiently. When your task is complete, emit a 'done' action with a summary of what you accomplished."
    )
    prompt_text = "\n".join(prompt_parts)

    # Smart agent-task matching — warn if a small model gets a complex task
    task_complexity = _score_task_complexity(goal)
    mismatch_warning = None
    if task_complexity >= 3 and _model_quality_tier(agent.model) < 2:
        better_name, reason = _find_better_agent(mission, agent, task_complexity)
        if better_name:
            mismatch_warning = f"⚠ CAPABILITY MISMATCH: {reason}"
            mission.log_event("WARN",
                              f"Task-agent mismatch: {agent_name} (tier-{_model_quality_tier(agent.model)}) "
                              f"assigned complex task; {better_name} is better suited",
                              agent=agent_name)

    task = AgentTask(
        mission_id=mission.mission_id,
        agent_name=agent_name,
        prompt=prompt_text,
        capabilities=["shell", "write_file", "read_file", "search", "patch_file", "batch_read", "workspace_tree"],
        constraints=constraints,
        timeout=constraints.get("timeout", _AUTO_TIMEOUT),
    )
    mission.tasks[task.task_id] = task
    agent.assigned_task = task.task_id
    agent.status = "busy"

    mission.log_event("DISPATCH",
                      f"task={task.task_id} agent={agent_name} goal={goal[:200]} "
                      f"max_iter={constraints.get('max_iterations', _AUTO_MAX_ITERATIONS)}",
                      task_id=task.task_id, agent=agent_name)

    # Compress conversation history if it's too long for the agent's context
    _compress_agent_history(mission, agent)

    # Start autonomous loop in background thread
    t = threading.Thread(target=_agent_autonomous_loop, args=(mission, task, agent),
                         daemon=True, name=f"auto-{task.task_id}")
    t.start()

    result = {"ok": True, "task_id": task.task_id, "agent": agent_name}
    if mismatch_warning:
        result["warning"] = mismatch_warning
    return result


def _action_cancel_task(mission, action):
    """Cancel a running task (autonomous or regular)."""
    task_id = action.get("task_id", "")
    reason = action.get("reason", "Cancelled by Showrunner")

    task = mission.tasks.get(task_id)
    if not task:
        return {"ok": False, "error": f"task '{task_id}' not found or already completed"}

    task._cancel_event.set()
    mission.log_event("CANCEL_TASK", f"task={task_id} reason={reason}",
                      task_id=task_id)

    return {"ok": True, "task_id": task_id, "message": f"Cancel signal sent: {reason}"}


def _action_wait_for_flock(mission, action):
    """Wait for ALL active flock tasks to complete or timeout."""
    timeout = min(int(action.get("timeout", 600)), _AUTO_TIMEOUT)
    start = time.time()

    if not mission.tasks:
        return {"ok": True, "completed": 0, "still_running": 0, "results": [],
                "message": "No active tasks to wait for."}

    mission.log_event("INFO",
                      f"Showrunner waiting for {len(mission.tasks)} flock tasks (timeout={timeout}s)")
    mission.status_message = f"Waiting for {len(mission.tasks)} flock task(s)..."

    while mission.tasks and (time.time() - start) < timeout:
        if mission._stop_event.is_set():
            break
        remaining = len(mission.tasks)
        mission.status_message = f"Waiting for {remaining} flock task(s)..."
        time.sleep(2)

    # Collect all newly completed results
    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    result_limit = limits["agent_result_max"]

    results = []
    for td in mission.task_history:
        if not td.get("_reported"):
            td["_reported"] = True
            results.append({
                "agent": td.get("agent_name", "?"),
                "status": td.get("status", "?"),
                "result": (td.get("result") or td.get("error", "no output"))[:result_limit]
            })

    still_running = list(mission.tasks.keys())
    elapsed = time.time() - start
    mission.log_event("INFO",
                      f"Wait complete: {len(results)} finished, {len(still_running)} still running "
                      f"({elapsed:.0f}s elapsed)")

    return {
        "ok": True,
        "completed": len(results),
        "still_running": len(still_running),
        "results": results,
        "elapsed": round(elapsed, 1),
    }


def _action_shell(mission, action):
    """Execute a shell command in the container."""
    command = action.get("command", "")
    if not command:
        return {"ok": False, "error": "no command"}

    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)

    timeout = min(int(action.get("timeout", _SHELL_TIMEOUT_DEFAULT)), _SHELL_TIMEOUT_INSTALL)
    mission.log_event("SHELL", f"$ {command[:200]} (timeout={timeout}s)")
    out, err, rc = _container_exec(mission.container_id, command, timeout=timeout)

    stdout_out = _smart_truncate(out, limits["smart_truncate_max"], is_own_content=True)
    stderr_limit = max(limits["smart_truncate_max"] // 3, 1500)
    result = {"ok": rc == 0, "exit_code": rc, "stdout": stdout_out, "stderr": err[:stderr_limit]}

    mission.log_event("SHELL_RESULT", f"rc={rc} out={len(out)}B err={len(err)}B",
                      exit_code=rc)
    return result


def _action_write_file(mission, action):
    """Write a file inside the container."""
    path = action.get("path", "")
    content = action.get("content", "")
    if not path:
        return {"ok": False, "error": "no path"}

    ok = _container_write_file(mission.container_id, path, content)
    mission.log_event("WRITE_FILE", f"path={path} size={len(content)}B ok={ok}")
    return {"ok": ok}


def _action_read_file(mission, action):
    """Read a file from the container. Supports optional start_line/end_line for targeted reads."""
    path = action.get("path", "")
    if not path:
        return {"ok": False, "error": "no path"}

    start_line = action.get("start_line")
    end_line = action.get("end_line")

    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    max_read = limits["read_file_max"]

    # Line-range read via sed (efficient — doesn't load whole file into Python)
    if start_line is not None and end_line is not None:
        start_line = max(1, int(start_line))
        end_line = max(start_line, int(end_line))
        wc_out, _, _ = _container_exec(mission.container_id,
                                       f"wc -l < {shlex.quote(path)}", timeout=10)
        total_lines = int(wc_out.strip()) if wc_out and wc_out.strip().isdigit() else "?"
        cmd = f"sed -n '{start_line},{end_line}p' {shlex.quote(path)}"
        content, err, rc = _container_exec(mission.container_id, cmd, timeout=30)
        if rc != 0 or content is None:
            return {"ok": False, "error": f"file not found or unreadable: {err}"}
        mission.log_event("READ_FILE", f"path={path} lines={start_line}-{end_line} size={len(content)}B")
        truncated = len(content) > max_read
        display = _smart_truncate(content, max_read, is_own_content=True) if truncated else content
        result = {"ok": True, "content": display,
                  "lines": f"{start_line}-{end_line}", "total_lines": total_lines}
        if truncated:
            result["truncated"] = True
        return result

    # Full file read
    content = _container_read_file(mission.container_id, path)
    if content is None:
        return {"ok": False, "error": "file not found or unreadable"}

    mission.log_event("READ_FILE", f"path={path} size={len(content)}B")
    total_lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
    truncated = len(content) > max_read
    display = _smart_truncate(content, max_read, is_own_content=True) if truncated else content
    result = {"ok": True, "content": display, "total_lines": total_lines}
    if truncated:
        result["truncated"] = True
        result["total_size"] = len(content)
        result["hint"] = "File was truncated. Use start_line/end_line for targeted reads."
    return result


def _action_search(mission, action):
    """Search files by content (grep -r) in the container."""
    pattern = action.get("pattern", "")
    path = action.get("path", "/home/mission/")
    if not pattern:
        return {"ok": False, "error": "no search pattern"}

    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    max_lines = max(60, limits["search_max"] // 100)
    search_limit = limits["search_max"]

    is_regex = action.get("regex", False)
    grep_flag = "-rn" if is_regex else "-rnF"
    cmd = f"grep {grep_flag} --include='*' {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -{max_lines}"
    mission.log_event("SEARCH", f"pattern={pattern} path={path}")
    out, err, rc = _container_exec(mission.container_id, cmd, timeout=30)
    if rc == 1 and not out:
        return {"ok": True, "matches": 0, "content": "No matches found."}
    return {"ok": True, "matches": out.count('\n'), "content": out[:search_limit]}


def _action_batch_read(mission, action):
    """Read multiple files in one action — efficient for gathering context."""
    paths = action.get("paths", [])
    if not paths or not isinstance(paths, list):
        return {"ok": False, "error": "paths must be a non-empty array"}

    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    per_file_limit = limits["read_file_max"] // max(len(paths), 1)
    per_file_limit = max(per_file_limit, 2000)

    results = {}
    total_chars = 0
    budget = limits["read_file_max"]
    for path in paths[:20]:
        if total_chars >= budget:
            results[path] = "[BUDGET EXHAUSTED]"
            continue
        content = _container_read_file(mission.container_id, path)
        if content is None:
            results[path] = "[NOT FOUND]"
        else:
            remaining = budget - total_chars
            limit = min(per_file_limit, remaining)
            if len(content) > limit:
                results[path] = _smart_truncate(content, limit, is_own_content=True)
            else:
                results[path] = content
            total_chars += len(results[path])

    mission.log_event("BATCH_READ", f"{len(paths)} files, {total_chars} chars total")
    return {"ok": True, "files": results}


def _action_workspace_tree(mission, action):
    """Return recursive workspace tree."""
    path = action.get("path", "/home/mission/")
    tree = _build_workspace_tree(mission.container_id, path)
    if not tree:
        return {"ok": False, "error": "could not build tree"}
    mission.log_event("WORKSPACE_TREE", f"path={path} ({len(tree)} chars)")
    return {"ok": True, "tree": tree}


def _action_patch_file(mission, action):
    """Surgical edit: replace exact text in a file without rewriting the whole thing."""
    path = action.get("path", "")
    old_text = action.get("old", "")
    new_text = action.get("new", "")
    if not path or not old_text:
        return {"ok": False, "error": "path and old text required"}

    content = _container_read_file(mission.container_id, path)
    if content is None:
        return {"ok": False, "error": f"file not found: {path}"}

    count = content.count(old_text)
    if count == 0:
        return {"ok": False, "error": "old text not found in file",
                "hint": "read_file first to see exact content"}
    if count > 1:
        return {"ok": False, "error": f"old text matches {count} locations — be more specific"}

    new_content = content.replace(old_text, new_text, 1)
    ok = _container_write_file(mission.container_id, path, new_content)
    mission.log_event("PATCH_FILE", f"path={path} ok={ok} (-{len(old_text)}B +{len(new_text)}B)")
    return {"ok": ok, "path": path}


def _action_reflect(mission, action):
    """Reflect/think without executing — logged for context."""
    thought = action.get("thought", "")
    mission.log_event("REFLECT", thought[:2000])
    return {"ok": True, "noted": True}


def _action_set_context_window(mission, action):
    """Let showrunner request a wider conversation history window."""
    requested = action.get("window")
    if not requested or not isinstance(requested, (int, float)) or requested < 1:
        return {"ok": False, "error": "window must be a positive integer"}
    requested = int(requested)
    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    default_window = limits["conversation_window"]
    mission.conversation_window_override = requested
    mission.log_event("CONFIG", f"Context window set to {requested} (default {default_window})")
    return {"ok": True, "window": requested, "default": default_window}


def _action_batch_write(mission, action):
    """Write multiple files in one action — efficient for scaffolding."""
    files = action.get("files", [])
    if not files or not isinstance(files, list):
        return {"ok": False, "error": "files must be a non-empty array of {path, content}"}

    results = {}
    ok_count = 0
    for entry in files[:30]:
        path = entry.get("path", "")
        content = entry.get("content", "")
        if not path:
            continue
        parent = "/".join(path.split("/")[:-1])
        if parent:
            _container_exec(mission.container_id, f"mkdir -p {shlex.quote(parent)}", timeout=10)
        ok = _container_write_file(mission.container_id, path, content)
        results[path] = {"ok": ok, "size": len(content)}
        if ok:
            ok_count += 1

    mission.log_event("BATCH_WRITE", f"{ok_count}/{len(files)} files written")
    return {"ok": ok_count > 0, "written": ok_count, "total": len(files), "files": results}


def _action_multi_patch(mission, action):
    """Apply multiple patches across files in one action."""
    patches = action.get("patches", [])
    if not patches or not isinstance(patches, list):
        return {"ok": False, "error": "patches must be a non-empty array of {path, old, new}"}

    results = []
    ok_count = 0
    for patch in patches[:20]:
        path = patch.get("path", "")
        old_text = patch.get("old", "")
        new_text = patch.get("new", "")
        if not path or not old_text:
            results.append({"path": path, "ok": False, "error": "path and old text required"})
            continue

        content = _container_read_file(mission.container_id, path)
        if content is None:
            results.append({"path": path, "ok": False, "error": "file not found"})
            continue

        cnt = content.count(old_text)
        if cnt == 0:
            results.append({"path": path, "ok": False, "error": "old text not found"})
            continue
        if cnt > 1:
            results.append({"path": path, "ok": False, "error": f"matches {cnt} locations"})
            continue

        new_content = content.replace(old_text, new_text, 1)
        ok = _container_write_file(mission.container_id, path, new_content)
        results.append({"path": path, "ok": ok,
                        "delta": f"-{len(old_text)}B +{len(new_text)}B"})
        if ok:
            ok_count += 1

    mission.log_event("MULTI_PATCH", f"{ok_count}/{len(patches)} patches applied")
    return {"ok": ok_count > 0, "applied": ok_count, "total": len(patches), "results": results}


def _action_save_note(mission, action):
    """Save a key-value note to the persistent scratchpad (always visible in system prompt)."""
    key = action.get("key", "").strip()
    value = action.get("value", "").strip()
    if not key or not value:
        return {"ok": False, "error": "key and value required"}
    key = key[:100]
    value = value[:2000]
    for note in mission.notes:
        if note["key"] == key:
            note["value"] = value
            mission.log_event("NOTE", f"Updated note: {key}")
            return {"ok": True, "action": "updated", "key": key}
    mission.notes.append({"key": key, "value": value})
    if len(mission.notes) > 50:
        mission.notes = mission.notes[-50:]
    mission.log_event("NOTE", f"Saved note: {key}")
    return {"ok": True, "action": "created", "key": key}


def _action_create_tool(mission, action):
    """Create a new tool script in the container."""
    name = action.get("name", "")
    description = action.get("description", "")
    script = action.get("script", "")

    if not name or not script:
        return {"ok": False, "error": "name and script required"}

    for t in mission.tools:
        if t["name"] == name:
            return {"ok": False, "error": f"tool '{name}' already exists"}

    tool_path = f"/home/mission/tools/{name}"
    ok = _container_write_file(mission.container_id, tool_path, script)
    if not ok:
        return {"ok": False, "error": "failed to write tool script"}

    _container_exec(mission.container_id, f"chmod +x {shlex.quote(tool_path)}")
    _container_exec(mission.container_id,
                    f"{shlex.quote(tool_path)} --help 2>/dev/null || true")

    tool_entry = {
        "name": name,
        "description": description,
        "created_by": "Showrunner",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    mission.tools.append(tool_entry)

    manifest_json = json.dumps(mission.tools, indent=2)
    _container_write_file(mission.container_id, "/home/mission/tools/manifest.json", manifest_json)

    mission.log_event("TOOL_CREATED", f"name={name}: {description}")
    return {"ok": True, "name": name}


def _action_status(mission, action):
    """Update status message for the UI."""
    mission.status_message = action.get("message", "")
    mission.status_progress = action.get("progress", -1)
    mission.log_event("STATUS", mission.status_message, progress=mission.status_progress)
    return {"ok": True}


def _action_user_prompt(mission, action):
    """Queue a prompt for the user."""
    question = action.get("question", "")
    blocking = action.get("blocking", False)

    if len(mission.pending_prompts) >= _PROMPT_STACK_MAX:
        return {"ok": False, "error": f"max {_PROMPT_STACK_MAX} pending prompts reached"}

    prompt_entry = {
        "id": "up-" + secrets.token_hex(4),
        "question": question,
        "blocking": blocking,
        "asked_at": time.time(),
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "answered": False,
        "response": None,
    }
    mission.pending_prompts.append(prompt_entry)
    mission.log_event("USER_PROMPT", f"Question: {question[:200]}", blocking=blocking)

    return {"ok": True, "prompt_id": prompt_entry["id"], "blocking": blocking}


def _action_user_message(mission, action):
    """Send a message to the user (non-blocking status)."""
    message = action.get("message", "")
    mission.log_event("USER_MESSAGE", message)
    return {"ok": True}


def _action_create_result(mission, action):
    """Write a self-contained result.html to the container."""
    html = action.get("html", "")
    if not html:
        return {"ok": False, "error": "html content required"}
    ok = _container_write_file(mission.container_id, "/home/mission/result.html", html)
    if not ok:
        return {"ok": False, "error": "failed to write result.html"}
    mission.log_event("WRITE_FILE", f"result.html ({len(html)} bytes)")
    mission._has_result = True
    return {"ok": True, "path": "/home/mission/result.html"}


def _action_complete(mission, action):
    """Mark mission as completed."""
    summary = action.get("summary", "Mission completed.")
    mission.status = "completed"
    mission.status_message = summary
    mission.status_progress = 100
    mission.log_event("COMPLETE", summary)

    # Check for result.html existence
    if mission.container_id and not mission._has_result:
        out, _, rc = _container_exec(mission.container_id, "test -f /home/mission/result.html && echo yes")
        if rc == 0 and "yes" in (out or ""):
            mission._has_result = True

    # Auto-generate a result page if showrunner didn't create one
    if mission.container_id and not mission._has_result:
        import html as html_mod
        safe_summary = html_mod.escape(summary)
        safe_mission = html_mod.escape(mission.mission_text or "")
        fallback_html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>body{font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;"
            "padding:0 20px;color:#e0e0e0;background:#1a1a2e}"
            "h1{color:#7fdbca;font-size:1.4rem}h2{color:#c3a6ff;font-size:1.1rem}"
            "p{line-height:1.6;white-space:pre-wrap}.mission{color:#888;font-style:italic}"
            "</style></head><body>"
            f"<h1>Mission Complete</h1>"
            f"<p class='mission'>{safe_mission}</p>"
            f"<h2>Result</h2><p>{safe_summary}</p>"
            "</body></html>"
        )
        ok = _container_write_file(mission.container_id, "/home/mission/result.html", fallback_html)
        if ok:
            mission._has_result = True

    # Write final log to container
    if mission.container_id:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        _container_exec(mission.container_id,
                        f"echo '\\n=== MISSION COMPLETE ===\\n{ts}\\n' >> /home/mission/mission_log.md")

    from .persistence import _persist_missions
    _persist_missions()
    return {"ok": True, "summary": summary}


# ── Action registry — replaces 21-branch if/elif chain ──────────────────

_ACTION_HANDLERS = {
    "dispatch":            _action_dispatch,
    "dispatch_autonomous": _action_dispatch,
    "cancel_task":         _action_cancel_task,
    "wait_for_flock":      _action_wait_for_flock,
    "shell":               _action_shell,
    "write_file":          _action_write_file,
    "read_file":           _action_read_file,
    "search":              _action_search,
    "create_tool":         _action_create_tool,
    "status":              _action_status,
    "user_prompt":         _action_user_prompt,
    "user_message":        _action_user_message,
    "create_result":       _action_create_result,
    "batch_read":          _action_batch_read,
    "workspace_tree":      _action_workspace_tree,
    "patch_file":          _action_patch_file,
    "reflect":             _action_reflect,
    "set_context_window":  _action_set_context_window,
    "batch_write":         _action_batch_write,
    "multi_patch":         _action_multi_patch,
    "save_note":           _action_save_note,
}


def _execute_action(mission, action):
    """Execute a single Showrunner action. Returns result dict."""
    atype = action.get("type", "")

    # Special handling for "complete" — pre-completion verification gate
    if atype == "complete":
        if not getattr(mission, '_completion_verified', False):
            mission._completion_verified = True
            mission.log_event("VERIFY", "Pre-completion verification gate triggered")
            state_json = _container_read_file(mission.container_id, "/home/mission/state.json") or "not found"
            ls_out, _, _ = _container_exec(mission.container_id, "ls -la /home/mission/", timeout=5)
            elapsed_min = (time.time() - mission.created_at) / 60
            return {
                "ok": False,
                "verification_required": True,
                "message": (
                    f"⚠ VERIFICATION REQUIRED before completion (elapsed: {elapsed_min:.0f}min)\n\n"
                    f"state.json:\n{state_json[:4000]}\n\n"
                    f"Workspace:\n{ls_out}\n\n"
                    "Before completing, you MUST verify:\n"
                    "1. Re-read the original mission text — what was asked?\n"
                    "2. Check EACH requirement in state.json — is it truly met?\n"
                    "3. Read your deliverable files — are they complete and thorough?\n"
                    "4. For documents: 'wc -w' to verify word counts meet expectations\n"
                    "5. For code: run it to verify it works\n"
                    "6. Mark each requirement verified:true in state.json\n"
                    "7. If anything is lacking, fix it NOW before completing\n\n"
                    "If everything checks out, emit 'complete' again with an accurate summary "
                    "including actual elapsed time."
                ),
            }
        return _action_complete(mission, action)

    handler = _ACTION_HANDLERS.get(atype)
    if handler:
        return handler(mission, action)

    mission.log_event("WARN", f"Unknown action type: {atype}")
    return {"ok": False, "error": f"unknown action type: {atype}"}
