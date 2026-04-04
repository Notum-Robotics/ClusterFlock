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
    _MISSION_PHASES,
    _SHELL_STDOUT_HARD_CAP,
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
    _syntax_check,
    _replace_lines,
    _apply_diff,
    _find_files,
    _file_info,
    _scaffold_project,
    _SCAFFOLD_TEMPLATES,
    _git_checkpoint,
    _git_restore,
    _git_list_checkpoints,
    _git_diff_since,
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
    # Hard safety cap — base64/binary tokenizes at ~1:1, not the assumed 3:1
    if len(stdout_out) > _SHELL_STDOUT_HARD_CAP:
        stdout_out = stdout_out[:_SHELL_STDOUT_HARD_CAP] + (
            f"\n[TRUNCATED — {len(out)} bytes total. "
            f"Pipe through head/tail/grep or redirect to file.]")
    stderr_limit = max(limits["smart_truncate_max"] // 3, 1500)
    result = {"ok": rc == 0, "exit_code": rc, "stdout": stdout_out, "stderr": err[:stderr_limit]}

    mission.log_event("SHELL_RESULT", f"rc={rc} out={len(out)}B err={len(err)}B",
                      exit_code=rc)
    return result


def _action_write_file(mission, action):
    """Write a file inside the container. Supports append mode."""
    path = action.get("path", "")
    content = action.get("content", "")
    append = action.get("append", False)
    if not path:
        return {"ok": False, "error": "no path"}

    # Ensure parent directory exists
    parent = "/".join(path.split("/")[:-1])
    if parent:
        _container_exec(mission.container_id, f"mkdir -p {shlex.quote(parent)}", timeout=10)

    if append:
        # Read existing content and append
        existing = _container_read_file(mission.container_id, path) or ""
        content = existing + content
        ok = _container_write_file(mission.container_id, path, content)
        mission.log_event("WRITE_FILE", f"path={path} append=true +{len(content) - len(existing)}B total={len(content)}B ok={ok}")
    else:
        ok = _container_write_file(mission.container_id, path, content)
        mission.log_event("WRITE_FILE", f"path={path} size={len(content)}B ok={ok}")

    result = {"ok": ok, "path": path, "size": len(content)}

    # Auto syntax check for supported file types
    if ok:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
            check_ok, errors = _syntax_check(mission.container_id, path)
            if not check_ok:
                result["syntax_errors"] = errors
                result["syntax_ok"] = False
            else:
                result["syntax_ok"] = True

    return result


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
    result = {"ok": ok, "path": path}
    # Auto syntax check
    if ok:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
            check_ok, errors = _syntax_check(mission.container_id, path)
            if not check_ok:
                result["syntax_errors"] = errors
                result["syntax_ok"] = False
            else:
                result["syntax_ok"] = True
    return result


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
            # Auto syntax check for supported file types
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
                check_ok, errors = _syntax_check(mission.container_id, path)
                if not check_ok:
                    results[path]["syntax_errors"] = errors

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
            mission.knowledge_base[key] = value  # agents see notes via knowledge_base
            mission.log_event("NOTE", f"Updated note: {key}")
            return {"ok": True, "action": "updated", "key": key}
    mission.notes.append({"key": key, "value": value})
    if len(mission.notes) > 50:
        mission.notes = mission.notes[-50:]
    mission.knowledge_base[key] = value  # agents see notes via knowledge_base
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

    # Include input_schema if provided — for structured tool usage
    input_schema = action.get("input_schema", [])

    tool_entry = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "created_by": action.get("_creator", "Showrunner"),
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


# ── New action handlers — line editing, diff, search, scaffold, tools ────

def _action_replace_lines(mission, action):
    """Replace a range of lines in a file — avoids full file rewrites for large files."""
    path = action.get("path", "")
    start_line = action.get("start_line")
    end_line = action.get("end_line")
    new_content = action.get("content", "")

    if not path or start_line is None or end_line is None:
        return {"ok": False, "error": "path, start_line, end_line, and content required"}

    start_line = max(1, int(start_line))
    end_line = max(start_line, int(end_line))

    ok, total_lines = _replace_lines(mission.container_id, path, start_line, end_line, new_content)
    if not ok:
        return {"ok": False, "error": f"file not found or write failed: {path}"}

    new_line_count = len(new_content.split("\n")) if new_content else 0
    replaced_count = end_line - start_line + 1
    mission.log_event("REPLACE_LINES",
                      f"path={path} lines {start_line}-{end_line} "
                      f"(-{replaced_count} +{new_line_count} = {total_lines} total)")
    result = {"ok": True, "path": path, "replaced_lines": f"{start_line}-{end_line}",
              "new_line_count": new_line_count, "total_lines": total_lines}

    # Auto syntax check
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
        check_ok, errors = _syntax_check(mission.container_id, path)
        if not check_ok:
            result["syntax_errors"] = errors
            result["syntax_ok"] = False
        else:
            result["syntax_ok"] = True
    return result


def _action_apply_diff(mission, action):
    """Apply a unified diff to a file."""
    path = action.get("path", "")
    diff = action.get("diff", "")
    if not diff:
        return {"ok": False, "error": "diff content required"}

    ok, output = _apply_diff(mission.container_id, path, diff)
    mission.log_event("APPLY_DIFF", f"path={path or 'multi'} ok={ok}")
    result = {"ok": ok, "output": output}

    # Auto syntax check if single file
    if ok and path:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
            check_ok, errors = _syntax_check(mission.container_id, path)
            if not check_ok:
                result["syntax_errors"] = errors
    return result


def _action_find_files(mission, action):
    """Find files matching a glob pattern."""
    pattern = action.get("pattern", "")
    path = action.get("path", "/home/mission/")
    if not pattern:
        return {"ok": False, "error": "pattern required (e.g. '*.py', 'test_*.js')"}

    files = _find_files(mission.container_id, pattern, path)
    mission.log_event("FIND_FILES", f"pattern={pattern} path={path} found={len(files)}")
    return {"ok": True, "files": files, "count": len(files)}


def _action_file_info(mission, action):
    """Get file metadata without reading content."""
    path = action.get("path", "")
    if not path:
        return {"ok": False, "error": "path required"}

    info = _file_info(mission.container_id, path)
    if not info:
        return {"ok": False, "error": f"file not found: {path}"}
    return {"ok": True, **info}


def _action_run_tool(mission, action):
    """Run a previously created tool from the manifest."""
    name = action.get("name", "")
    args = action.get("args", [])
    if not name:
        return {"ok": False, "error": "tool name required"}

    # Look up tool in manifest
    tool = None
    for t in mission.tools:
        if t["name"] == name:
            tool = t
            break
    if not tool:
        available = [t["name"] for t in mission.tools]
        return {"ok": False, "error": f"tool '{name}' not found. Available: {', '.join(available) or 'none'}"}

    tool_path = f"/home/mission/tools/{name}"
    # Build command with args
    if isinstance(args, list):
        arg_str = " ".join(shlex.quote(str(a)) for a in args)
    elif isinstance(args, str):
        arg_str = args
    else:
        arg_str = ""

    cmd = f"{shlex.quote(tool_path)} {arg_str}"
    timeout = min(int(action.get("timeout", 120)), _SHELL_TIMEOUT_DEFAULT)
    out, err, rc = _container_exec(mission.container_id, cmd, timeout=timeout)

    mission.log_event("RUN_TOOL", f"tool={name} args={arg_str[:100]} rc={rc}")

    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    return {
        "ok": rc == 0,
        "exit_code": rc,
        "stdout": _smart_truncate(out, limits["smart_truncate_max"], is_own_content=True),
        "stderr": err[:1000] if err else "",
    }


def _action_scaffold(mission, action):
    """Create a project skeleton from a template."""
    template = action.get("template", "")
    base_path = action.get("path", "/home/mission/")
    if not template:
        available = ", ".join(sorted(_SCAFFOLD_TEMPLATES.keys()))
        return {"ok": False, "error": f"template required. Available: {available}"}

    ok, created, description = _scaffold_project(mission.container_id, template, base_path)
    if not ok:
        return {"ok": False, "error": description}  # description contains error message

    mission.log_event("SCAFFOLD", f"template={template} files={len(created)} path={base_path}")
    return {"ok": True, "template": template, "description": description,
            "files_created": created, "count": len(created)}


# ── Git checkpoint / restore / knowledge actions ─────────────────────────

def _action_checkpoint(mission, action):
    """Create a named git checkpoint (snapshot of all files)."""
    name = action.get("name", "checkpoint")
    description = action.get("description", "")
    ok, result = _git_checkpoint(mission.container_id, name, description)
    if not ok:
        return {"ok": False, "error": f"checkpoint failed: {result}"}
    mission.log_event("CHECKPOINT", f"name={name} hash={result} desc={description[:100]}")
    return {"ok": True, "name": name, "hash": result}


def _action_restore(mission, action):
    """Restore workspace to a previous checkpoint."""
    ref = action.get("ref", "") or action.get("hash", "")
    if not ref:
        return {"ok": False, "error": "ref (commit hash or reference) required"}
    ok, output = _git_restore(mission.container_id, ref)
    if not ok:
        return {"ok": False, "error": f"restore failed: {output}"}
    mission.log_event("RESTORE", f"ref={ref}")
    # Invalidate workspace tree cache
    mission._workspace_tree_at = 0
    return {"ok": True, "ref": ref, "output": output}


def _action_list_checkpoints(mission, action):
    """List recent checkpoints."""
    entries = _git_list_checkpoints(mission.container_id)
    return {"ok": True, "checkpoints": entries, "count": len(entries)}


def _action_diff_since(mission, action):
    """Show changes since a checkpoint."""
    ref = action.get("ref", "HEAD~1")
    diff = _git_diff_since(mission.container_id, ref)
    sr_ctx = _get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = _scaled_limits(sr_ctx)
    truncated = _smart_truncate(diff, limits["read_file_max"], is_own_content=True) if diff else ""
    return {"ok": True, "diff": truncated, "ref": ref}


def _action_save_knowledge(mission, action):
    """Save a key-value entry to the mission-wide knowledge base (visible to all agents)."""
    key = action.get("key", "").strip()
    value = action.get("value", "").strip()
    if not key or not value:
        return {"ok": False, "error": "key and value required"}
    key = key[:100]
    value = value[:3000]
    if not hasattr(mission, "knowledge_base"):
        mission.knowledge_base = {}
    existing = key in mission.knowledge_base
    mission.knowledge_base[key] = value
    # Cap at 50 entries
    if len(mission.knowledge_base) > 50:
        oldest_key = next(iter(mission.knowledge_base))
        del mission.knowledge_base[oldest_key]
    mission.log_event("KNOWLEDGE", f"{'Updated' if existing else 'Added'}: {key}")
    return {"ok": True, "action": "updated" if existing else "created", "key": key}


def _action_advance_phase(mission, action):
    """Advance the mission to the next phase (or a specific phase)."""
    target = action.get("phase", "").strip()
    current = getattr(mission, "mission_phase", "planning")
    if target:
        if target not in _MISSION_PHASES:
            return {"ok": False, "error": f"Unknown phase '{target}'. Valid: {', '.join(_MISSION_PHASES)}"}
        cur_idx = _MISSION_PHASES.index(current) if current in _MISSION_PHASES else 0
        tgt_idx = _MISSION_PHASES.index(target)
        if tgt_idx < cur_idx:
            return {"ok": False, "error": f"Cannot go backward from '{current}' to '{target}'"}
        new_phase = target
    else:
        cur_idx = _MISSION_PHASES.index(current) if current in _MISSION_PHASES else 0
        if cur_idx >= len(_MISSION_PHASES) - 1:
            return {"ok": False, "error": f"Already at final phase '{current}'"}
        new_phase = _MISSION_PHASES[cur_idx + 1]
    # Record phase transition
    if not hasattr(mission, "phase_history"):
        mission.phase_history = []
    mission.phase_history.append({
        "phase": current, "exited_at": time.time(),
    })
    mission.mission_phase = new_phase
    mission.log_event("PHASE", f"Advanced: {current} → {new_phase}")
    return {"ok": True, "previous": current, "current": new_phase}


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
    "run_tool":            _action_run_tool,
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
    "replace_lines":       _action_replace_lines,
    "apply_diff":          _action_apply_diff,
    "find_files":          _action_find_files,
    "file_info":           _action_file_info,
    "scaffold":            _action_scaffold,
    "checkpoint":          _action_checkpoint,
    "restore":             _action_restore,
    "list_checkpoints":    _action_list_checkpoints,
    "diff_since":          _action_diff_since,
    "save_knowledge":      _action_save_knowledge,
    "advance_phase":       _action_advance_phase,
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

            # Auto-run verify tool if it exists
            verify_out, _, verify_rc = _container_exec(
                mission.container_id,
                "test -x /home/mission/tools/verify && /home/mission/tools/verify /home/mission 2>&1 || echo 'verify tool not available'",
                timeout=60)
            # Auto-run diff since init
            diff_out = _git_diff_since(mission.container_id, "HEAD~5") if mission.container_id else ""

            return {
                "ok": False,
                "verification_required": True,
                "message": (
                    f"⚠ VERIFICATION REQUIRED before completion (elapsed: {elapsed_min:.0f}min)\n\n"
                    f"=== Automated Verification ===\n{verify_out[:3000]}\n\n"
                    f"=== state.json ===\n{state_json[:3000]}\n\n"
                    f"=== Workspace ===\n{ls_out}\n\n"
                    f"=== Recent Changes ===\n{diff_out[:2000]}\n\n"
                    "Before completing, verify:\n"
                    "1. Check the automated verification results above — fix any FAIL items\n"
                    "2. Check EACH requirement in state.json — is it truly met?\n"
                    "3. Read your deliverable files — are they complete and thorough?\n"
                    "4. For code: run it to verify it works\n"
                    "5. Mark each requirement verified:true in state.json\n"
                    "6. If anything is lacking, fix it NOW before completing\n\n"
                    "If everything checks out, emit 'complete' again with an accurate summary."
                ),
            }
        return _action_complete(mission, action)

    handler = _ACTION_HANDLERS.get(atype)
    if handler:
        return handler(mission, action)

    mission.log_event("WARN", f"Unknown action type: {atype}")
    return {"ok": False, "error": f"unknown action type: {atype}"}
