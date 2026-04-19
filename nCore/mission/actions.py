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
    _SYNTAX_CHECK_EXTENSIONS,
)
from .scoring import (
    model_quality_tier,
    score_task_complexity,
    find_better_agent,
    get_endpoint_ctx,
    scaled_limits,
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
from .showrunner import compress_agent_history
from .flock import generate_agent_identity_prompt, reassign_flock_roles


# ── Individual action handlers ───────────────────────────────────────────

def _action_dispatch(mission, action):
    """Dispatch a task to a named agent — autonomous loop with tools."""
    from .agent_loop import agent_autonomous_loop

    agent_name = action.get("agent", "")
    goal = action.get("goal", "") or action.get("prompt", "")
    constraints = action.get("constraints", {})
    context = action.get("context", "")

    agent = mission.flock.get(agent_name)
    if not agent:
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

    prompt_parts = [f"GOAL: {goal}"]
    if context:
        prompt_parts.append(f"\nCONTEXT: {context}")
    if constraints.get("success_criteria"):
        prompt_parts.append(f"\nSUCCESS CRITERIA: {constraints['success_criteria']}")
    if constraints.get("working_dir"):
        prompt_parts.append(f"\nWORKING DIRECTORY: {constraints['working_dir']}")
    elapsed_min = (time.time() - mission.created_at) / 60
    prompt_parts.append(
        f"\nTIME CONTEXT: Mission running {elapsed_min:.0f}min. Work efficiently. "
        f"When done, emit a 'done' action with a summary.")
    prompt_text = "\n".join(prompt_parts)

    task_complexity = score_task_complexity(goal)
    mismatch_warning = None
    if task_complexity >= 3 and model_quality_tier(agent.model) < 2:
        better_name, reason = find_better_agent(mission, agent, task_complexity)
        if better_name:
            better_agent = mission.flock.get(better_name)
            if better_agent and better_agent.status == "available":
                mission.log_event("WARN",
                                  f"Auto-redirected complex task from {agent_name} to {better_name}",
                                  agent=agent_name)
                agent = better_agent
                agent_name = better_name
                mismatch_warning = f"⚠ REDIRECTED: {reason}"
            else:
                mismatch_warning = f"⚠ CAPABILITY MISMATCH: {reason}"
    elif task_complexity >= 2 and model_quality_tier(agent.model) < 2:
        mismatch_warning = f"⚠ LOW-CAPABILITY: {agent_name} is tier-1. Consider tier-2+ for this."

    task = AgentTask(
        mission_id=mission.mission_id,
        agent_name=agent_name,
        prompt=prompt_text,
        capabilities=["shell", "write_file", "read_file", "search", "patch_file",
                       "batch_read", "workspace_tree"],
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

    compress_agent_history(mission, agent)

    t = threading.Thread(target=agent_autonomous_loop, args=(mission, task, agent),
                         daemon=True, name=f"auto-{task.task_id}")
    t.start()


    result = {"ok": True, "task_id": task.task_id, "agent": agent_name}
    if mismatch_warning:
        result["warning"] = mismatch_warning
    return result


def _action_cancel_task(mission, action):
    task_id = action.get("task_id", "")
    reason = action.get("reason", "Cancelled by Showrunner")
    task = mission.tasks.get(task_id)
    if not task:
        return {"ok": False, "error": f"task '{task_id}' not found or already completed"}
    task._cancel_event.set()
    mission.log_event("CANCEL_TASK", f"task={task_id} reason={reason}", task_id=task_id)
    return {"ok": True, "task_id": task_id, "message": f"Cancel signal sent: {reason}"}


def _action_wait_for_flock(mission, action):
    timeout = min(int(action.get("timeout", 600)), _AUTO_TIMEOUT)
    start = time.time()
    if not mission.tasks:
        return {"ok": True, "completed": 0, "still_running": 0, "results": [],
                "message": "No active tasks."}
    mission.log_event("INFO", f"Showrunner waiting for {len(mission.tasks)} tasks (timeout={timeout}s)")
    mission.status_message = f"Waiting for {len(mission.tasks)} task(s)..."
    while mission.tasks and (time.time() - start) < timeout:
        if mission._stop_event.is_set():
            break
        mission.status_message = f"Waiting for {len(mission.tasks)} task(s)..."
        time.sleep(2)

    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    result_limit = limits["agent_result_max"]
    results = []
    for td in mission.task_history:
        if not td.get("_reported"):
            td["_reported"] = True
            results.append({
                "agent": td.get("agent_name", "?"),
                "status": td.get("status", "?"),
                "result": (td.get("result") or td.get("error", "no output"))[:result_limit],
            })
    elapsed = time.time() - start
    return {"ok": True, "completed": len(results),
            "still_running": len(mission.tasks), "results": results,
            "elapsed": round(elapsed, 1)}


def _action_shell(mission, action):
    command = action.get("command", "")
    if not command:
        return {"ok": False, "error": "no command"}
    working_dir = action.get("working_dir", "/home/mission/")
    command = f"cd {shlex.quote(working_dir)} && {command}"
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    timeout = min(int(action.get("timeout", _SHELL_TIMEOUT_DEFAULT)), _SHELL_TIMEOUT_INSTALL)
    mission.log_event("SHELL", f"$ {command[:200]} (timeout={timeout}s)")
    out, err, rc = _container_exec(mission.container_id, command, timeout=timeout)
    stdout_out = _smart_truncate(out, limits["smart_truncate_max"], is_own_content=True)
    if len(stdout_out) > _SHELL_STDOUT_HARD_CAP:
        stdout_out = stdout_out[:_SHELL_STDOUT_HARD_CAP] + (
            f"\n[TRUNCATED — {len(out)} bytes total. Pipe through head/tail/grep.]")
    stderr_limit = max(limits["smart_truncate_max"] // 3, 1500)
    mission.log_event("SHELL_RESULT", f"rc={rc} out={len(out)}B err={len(err)}B", exit_code=rc)
    return {"ok": rc == 0, "exit_code": rc, "stdout": stdout_out, "stderr": err[:stderr_limit]}


def _action_write_file(mission, action):
    path = action.get("path", "")
    content = action.get("content", "")
    append = action.get("append", False)
    if not path:
        return {"ok": False, "error": "no path"}
    parent = "/".join(path.split("/")[:-1])
    if parent:
        _container_exec(mission.container_id, f"mkdir -p {shlex.quote(parent)}", timeout=10)
    if append:
        existing = _container_read_file(mission.container_id, path) or ""
        content = existing + content
        ok = _container_write_file(mission.container_id, path, content)
        mission.log_event("WRITE_FILE", f"path={path} append=true +{len(content) - len(existing)}B total={len(content)}B ok={ok}")
    else:
        ok = _container_write_file(mission.container_id, path, content)
        mission.log_event("WRITE_FILE", f"path={path} size={len(content)}B ok={ok}")
    result = {"ok": ok, "path": path, "size": len(content)}
    if ok:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in _SYNTAX_CHECK_EXTENSIONS:
            check_ok, errors = _syntax_check(mission.container_id, path)
            if not check_ok:
                result["syntax_errors"] = errors
                result["syntax_ok"] = False
            else:
                result["syntax_ok"] = True
    return result


def _action_read_file(mission, action):
    path = action.get("path", "")
    if not path:
        return {"ok": False, "error": "no path"}
    start_line = action.get("start_line")
    end_line = action.get("end_line")
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    max_read = limits["read_file_max"]

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
    pattern = action.get("pattern", "")
    path = action.get("path", "/home/mission/")
    if not pattern:
        return {"ok": False, "error": "no search pattern"}
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    max_lines = max(60, limits["search_max"] // 100)
    is_regex = action.get("regex", False)
    grep_flag = "-rn" if is_regex else "-rnF"
    cmd = f"grep {grep_flag} --include='*' {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -{max_lines}"
    mission.log_event("SEARCH", f"pattern={pattern} path={path}")
    out, err, rc = _container_exec(mission.container_id, cmd, timeout=30)
    if rc == 1 and not out:
        return {"ok": True, "matches": 0, "content": "No matches found."}
    return {"ok": True, "matches": out.count('\n'), "content": out[:limits["search_max"]]}


def _action_batch_read(mission, action):
    paths = action.get("paths", [])
    if not paths or not isinstance(paths, list):
        return {"ok": False, "error": "paths must be a non-empty array"}
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    per_file_limit = max(limits["read_file_max"] // max(len(paths), 1), 2000)
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
            results[path] = _smart_truncate(content, limit, is_own_content=True) if len(content) > limit else content
            total_chars += len(results[path])
    mission.log_event("BATCH_READ", f"{len(paths)} files, {total_chars} chars total")
    return {"ok": True, "files": results}


def _action_workspace_tree(mission, action):
    path = action.get("path", "/home/mission/")
    tree = _build_workspace_tree(mission.container_id, path)
    if not tree:
        return {"ok": False, "error": "could not build tree"}
    mission.log_event("WORKSPACE_TREE", f"path={path} ({len(tree)} chars)")
    return {"ok": True, "tree": tree}


def _action_patch_file(mission, action):
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
        return {"ok": False, "error": "old text not found — read_file first"}
    if count > 1:
        return {"ok": False, "error": f"old text matches {count} locations — be more specific"}
    new_content = content.replace(old_text, new_text, 1)
    ok = _container_write_file(mission.container_id, path, new_content)
    mission.log_event("PATCH_FILE", f"path={path} ok={ok} (-{len(old_text)}B +{len(new_text)}B)")
    result = {"ok": ok, "path": path}
    if ok:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in _SYNTAX_CHECK_EXTENSIONS:
            check_ok, errors = _syntax_check(mission.container_id, path)
            if not check_ok:
                result["syntax_errors"] = errors
                result["syntax_ok"] = False
            else:
                result["syntax_ok"] = True
    return result


def _action_reflect(mission, action):
    mission.log_event("REFLECT", action.get("thought", "")[:2000])
    return {"ok": True, "noted": True}


def _action_set_context_window(mission, action):
    requested = action.get("window")
    if not requested or not isinstance(requested, (int, float)) or requested < 1:
        return {"ok": False, "error": "window must be a positive integer"}
    requested = int(requested)
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    default_window = limits["conversation_window"]
    mission.conversation_window_override = requested
    mission.log_event("CONFIG", f"Context window set to {requested} (default {default_window})")
    return {"ok": True, "window": requested, "default": default_window}


def _action_batch_write(mission, action):
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
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext in _SYNTAX_CHECK_EXTENSIONS:
                check_ok, errors = _syntax_check(mission.container_id, path)
                if not check_ok:
                    results[path]["syntax_errors"] = errors
    mission.log_event("BATCH_WRITE", f"{ok_count}/{len(files)} files written")
    return {"ok": ok_count > 0, "written": ok_count, "total": len(files), "files": results}


def _action_multi_patch(mission, action):
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
            results.append({"path": path, "ok": False, "error": "path and old required"})
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
        results.append({"path": path, "ok": ok, "delta": f"-{len(old_text)}B +{len(new_text)}B"})
        if ok:
            ok_count += 1
    mission.log_event("MULTI_PATCH", f"{ok_count}/{len(patches)} patches applied")
    return {"ok": ok_count > 0, "applied": ok_count, "total": len(patches), "results": results}


def _action_save_note(mission, action):
    key = action.get("key", "").strip()[:100]
    value = action.get("value", "").strip()[:2000]
    if not key or not value:
        return {"ok": False, "error": "key and value required"}
    for note in mission.notes:
        if note["key"] == key:
            note["value"] = value
            mission.knowledge_base[key] = value
            mission.log_event("NOTE", f"Updated note: {key}")
            return {"ok": True, "action": "updated", "key": key}
    mission.notes.append({"key": key, "value": value})
    if len(mission.notes) > 50:
        mission.notes = mission.notes[-50:]
    mission.knowledge_base[key] = value
    mission.log_event("NOTE", f"Saved note: {key}")
    return {"ok": True, "action": "created", "key": key}


def _action_create_tool(mission, action):
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
    _container_exec(mission.container_id, f"{shlex.quote(tool_path)} --help 2>/dev/null || true")
    tool_entry = {
        "name": name, "description": description,
        "input_schema": action.get("input_schema", []),
        "created_by": action.get("_creator", "Showrunner"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    mission.tools.append(tool_entry)
    _container_write_file(mission.container_id, "/home/mission/tools/manifest.json",
                          json.dumps(mission.tools, indent=2))
    mission.log_event("TOOL_CREATED", f"name={name}: {description}")
    return {"ok": True, "name": name}


def _action_status(mission, action):
    mission.status_message = action.get("message", "")
    mission.status_progress = action.get("progress", -1)
    mission.log_event("STATUS", mission.status_message, progress=mission.status_progress)
    return {"ok": True}


def _action_user_prompt(mission, action):
    question = action.get("question", "")
    blocking = action.get("blocking", False)
    if len(mission.pending_prompts) >= _PROMPT_STACK_MAX:
        return {"ok": False, "error": f"max {_PROMPT_STACK_MAX} pending prompts reached"}
    prompt_entry = {
        "id": "up-" + secrets.token_hex(4), "question": question,
        "blocking": blocking, "asked_at": time.time(),
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "answered": False, "response": None,
    }
    mission.pending_prompts.append(prompt_entry)
    mission.log_event("USER_PROMPT", f"Question: {question[:200]}", blocking=blocking)
    return {"ok": True, "prompt_id": prompt_entry["id"], "blocking": blocking}


def _action_user_message(mission, action):
    mission.log_event("USER_MESSAGE", action.get("message", ""))
    return {"ok": True}


def _action_create_result(mission, action):
    html = action.get("html", "")
    if not html:
        return {"ok": False, "error": "html content required"}
    existing = _container_read_file(mission.container_id, "/home/mission/result.html")
    if existing and len(existing) > len(html) * 2 and len(existing) > 2000:
        mission._has_result = True
        return {"ok": True, "path": "/home/mission/result.html",
                "message": "Kept existing result.html (larger than incoming)"}
    ok = _container_write_file(mission.container_id, "/home/mission/result.html", html)
    if not ok:
        return {"ok": False, "error": "failed to write result.html"}
    mission._has_result = True
    mission.log_event("WRITE_FILE", f"result.html ({len(html)} bytes)")
    return {"ok": True, "path": "/home/mission/result.html"}


def _action_complete(mission, action):
    summary = action.get("summary", "Mission completed.")
    mission.status = "completed"
    mission.status_message = summary
    mission.status_progress = 100
    mission.log_event("COMPLETE", summary)

    try:
        from .memory import auto_extract_memories
        extracted = auto_extract_memories(mission)
        if extracted:
            mission.log_event("MEMORY", f"Auto-extracted {extracted} long-term memories")
    except Exception as e:
        mission.log_event("WARN", f"Memory extraction failed: {e}")

    if mission.container_id and not mission._has_result:
        out, _, rc = _container_exec(mission.container_id,
                                     "test -f /home/mission/result.html && echo yes")
        if rc == 0 and "yes" in (out or ""):
            mission._has_result = True

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

    if mission.container_id:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        _container_exec(mission.container_id,
                        f"echo '\\n=== MISSION COMPLETE ===\\n{ts}\\n' >> /home/mission/mission_log.md")

    from .persistence import persist_missions
    persist_missions()
    return {"ok": True, "summary": summary}


def _action_replace_lines(mission, action):
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
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _SYNTAX_CHECK_EXTENSIONS:
        check_ok, errors = _syntax_check(mission.container_id, path)
        if not check_ok:
            result["syntax_errors"] = errors
            result["syntax_ok"] = False
        else:
            result["syntax_ok"] = True
    return result


def _action_apply_diff(mission, action):
    path = action.get("path", "")
    diff = action.get("diff", "")
    if not diff:
        return {"ok": False, "error": "diff content required"}
    ok, output = _apply_diff(mission.container_id, path, diff)
    mission.log_event("APPLY_DIFF", f"path={path or 'multi'} ok={ok}")
    result = {"ok": ok, "output": output}
    if ok and path:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in _SYNTAX_CHECK_EXTENSIONS:
            check_ok, errors = _syntax_check(mission.container_id, path)
            if not check_ok:
                result["syntax_errors"] = errors
    return result


def _action_find_files(mission, action):
    pattern = action.get("pattern", "")
    path = action.get("path", "/home/mission/")
    if not pattern:
        return {"ok": False, "error": "pattern required"}
    files = _find_files(mission.container_id, pattern, path)
    mission.log_event("FIND_FILES", f"pattern={pattern} path={path} found={len(files)}")
    return {"ok": True, "files": files, "count": len(files)}


def _action_file_info(mission, action):
    path = action.get("path", "")
    if not path:
        return {"ok": False, "error": "path required"}
    info = _file_info(mission.container_id, path)
    if not info:
        return {"ok": False, "error": f"file not found: {path}"}
    return {"ok": True, **info}


def _action_run_tool(mission, action):
    name = action.get("name", "")
    args = action.get("args", [])
    if not name:
        return {"ok": False, "error": "tool name required"}
    tool = None
    for t in mission.tools:
        if t["name"] == name:
            tool = t
            break
    if not tool:
        available = [t["name"] for t in mission.tools]
        return {"ok": False, "error": f"tool '{name}' not found. Available: {', '.join(available) or 'none'}"}
    tool_path = f"/home/mission/tools/{name}"
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
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    return {"ok": rc == 0, "exit_code": rc,
            "stdout": _smart_truncate(out, limits["smart_truncate_max"], is_own_content=True),
            "stderr": err[:1000] if err else ""}


def _action_scaffold(mission, action):
    template = action.get("template", "")
    base_path = action.get("path", "/home/mission/")
    if not template:
        available = ", ".join(sorted(_SCAFFOLD_TEMPLATES.keys()))
        return {"ok": False, "error": f"template required. Available: {available}"}
    ok, created, description = _scaffold_project(mission.container_id, template, base_path)
    if not ok:
        return {"ok": False, "error": description}
    mission.log_event("SCAFFOLD", f"template={template} files={len(created)} path={base_path}")
    return {"ok": True, "template": template, "description": description,
            "files_created": created, "count": len(created)}


def _action_checkpoint(mission, action):
    name = action.get("name", "checkpoint")
    description = action.get("description", "")
    ok, result = _git_checkpoint(mission.container_id, name, description)
    if not ok:
        return {"ok": False, "error": f"checkpoint failed: {result}"}
    mission.log_event("CHECKPOINT", f"name={name} hash={result} desc={description[:100]}")
    return {"ok": True, "name": name, "hash": result}


def _action_restore(mission, action):
    ref = action.get("ref", "") or action.get("hash", "")
    if not ref:
        return {"ok": False, "error": "ref (commit hash or reference) required"}
    ok, output = _git_restore(mission.container_id, ref)
    if not ok:
        return {"ok": False, "error": f"restore failed: {output}"}
    mission.log_event("RESTORE", f"ref={ref}")
    mission._workspace_tree_at = 0
    return {"ok": True, "ref": ref, "output": output}


def _action_list_checkpoints(mission, action):
    entries = _git_list_checkpoints(mission.container_id)
    return {"ok": True, "checkpoints": entries, "count": len(entries)}


def _action_diff_since(mission, action):
    ref = action.get("ref", "HEAD~1")
    diff = _git_diff_since(mission.container_id, ref)
    sr_ctx = get_endpoint_ctx(mission.showrunner_node_id, mission.showrunner_model)
    limits = scaled_limits(sr_ctx)
    truncated = _smart_truncate(diff, limits["read_file_max"], is_own_content=True) if diff else ""
    return {"ok": True, "diff": truncated, "ref": ref}


def _action_save_knowledge(mission, action):
    key = action.get("key", "").strip()[:100]
    value = action.get("value", "").strip()[:3000]
    if not key or not value:
        return {"ok": False, "error": "key and value required"}
    existing = key in mission.knowledge_base
    mission.knowledge_base[key] = value
    if len(mission.knowledge_base) > 50:
        oldest_key = next(iter(mission.knowledge_base))
        del mission.knowledge_base[oldest_key]
    mission.log_event("KNOWLEDGE", f"{'Updated' if existing else 'Added'}: {key}")
    return {"ok": True, "action": "updated" if existing else "created", "key": key}


def _action_publish_artifact(mission, action):
    name = (action.get("name") or "").strip()
    artifact_type = (action.get("artifact_type") or action.get("type_name") or "file").strip()
    path = (action.get("path") or "").strip()
    summary = (action.get("summary") or "").strip()
    if not name:
        return {"ok": False, "error": "artifact name required"}
    if not path:
        return {"ok": False, "error": "artifact path required"}
    if artifact_type not in ("file", "contract", "schema"):
        artifact_type = "file"
    if not path.startswith("/"):
        path = f"/home/mission/{path}"
    if mission.container_id:
        _, _, rc = _container_exec(mission.container_id, f"test -f {path}", timeout=5)
        if rc != 0:
            return {"ok": False, "error": f"file not found: {path}"}
    if not summary and mission.container_id:
        content = _container_read_file(mission.container_id, path)
        if content:
            summary = content[:500]
    artifact = {"name": name[:100], "type": artifact_type, "path": path,
                "summary": (summary or "")[:2000]}
    attached = False
    if mission.plan:
        for pt in mission.plan.tasks:
            if pt.status == "dispatched" and pt.assigned_agent:
                for task in mission.tasks.values():
                    if (task.agent_name == pt.assigned_agent and task.status == "running" and
                            task.constraints.get("plan_task_id") == pt.id):
                        pt.artifacts = [a for a in pt.artifacts if a["name"] != name]
                        pt.artifacts.append(artifact)
                        attached = True
                        break
                if attached:
                    break
    mission.knowledge_base[f"artifact:{name}"] = f"{artifact_type} at {path}: {(summary or '')[:500]}"
    mission.log_event("ARTIFACT", f"Published: {name} ({artifact_type}) at {path}", artifact=name)
    return {"ok": True, "artifact": name, "attached_to_plan_task": attached}


def _action_test_runner(mission, action):
    import re as _re
    command = (action.get("command") or "").strip()
    work_dir = (action.get("path") or "/home/mission/").strip()
    timeout = min(int(action.get("timeout", 120)), 300)
    if not mission.container_id:
        return {"ok": False, "error": "no container"}
    if not command:
        _, _, rc_py = _container_exec(mission.container_id,
            f"cd {work_dir} && test -d tests || test -f test_*.py || test -f conftest.py", timeout=5)
        if rc_py == 0:
            command = "python3 -m pytest -v --tb=short 2>&1"
        else:
            _, _, rc_js = _container_exec(mission.container_id,
                f"cd {work_dir} && test -f package.json && grep -q '\"test\"' package.json", timeout=5)
            if rc_js == 0:
                command = "npm test 2>&1"
            else:
                return {"ok": False, "error": "no test framework detected — provide command"}
    out, err, rc = _container_exec(mission.container_id, f"cd {work_dir} && {command}", timeout=timeout)
    raw_output = (out or "") + (err or "")
    passed, failed, errors_list = 0, 0, []
    m_pytest = _re.search(r'(\d+)\s+passed', raw_output)
    m_pytest_f = _re.search(r'(\d+)\s+failed', raw_output)
    m_pytest_e = _re.search(r'(\d+)\s+error', raw_output)
    if m_pytest:
        passed = int(m_pytest.group(1))
    if m_pytest_f:
        failed = int(m_pytest_f.group(1))
    if m_pytest_e:
        failed += int(m_pytest_e.group(1))
    failure_blocks = _re.findall(
        r'(?:FAILED|ERROR)\s+([\w/.:]+(?:\[.*?\])?)\s*[-—]?\s*(.*?)(?=\n(?:FAILED|ERROR|=====|$))',
        raw_output, _re.DOTALL)
    for test_name, detail in failure_blocks[:10]:
        errors_list.append({"test": test_name.strip(), "message": detail.strip()[:300]})
    if not m_pytest:
        m_jest_p = _re.search(r'Tests:\s*(\d+)\s+passed', raw_output)
        m_jest_f = _re.search(r'Tests:\s*(\d+)\s+failed', raw_output)
        if m_jest_p:
            passed = int(m_jest_p.group(1))
        if m_jest_f:
            failed = int(m_jest_f.group(1))
        jest_failures = _re.findall(r'●\s+(.*?)\n\s*(.*?)(?=\n\s*●|\n\n)', raw_output, _re.DOTALL)
        for test_name, detail in jest_failures[:10]:
            errors_list.append({"test": test_name.strip(), "message": detail.strip()[:300]})
    if passed == 0 and failed == 0:
        if rc == 0:
            passed = 1
        else:
            failed = 1
    mission.log_event("TEST_RUN", f"command={command[:80]} passed={passed} failed={failed} rc={rc}")
    return {"ok": rc == 0, "passed": passed, "failed": failed, "exit_code": rc,
            "errors": errors_list, "output": raw_output[:2000]}


def _action_save_memory(mission, action):
    scope = action.get("scope", "mission").lower()
    content = (action.get("content") or action.get("value", "")).strip()
    category = action.get("category", "observation").strip()
    if scope == "global":
        from .memory import save_long_term
        key = action.get("key", "").strip()
        tags = action.get("tags", [])
        if not key or not content:
            return {"ok": False, "error": "key and content required for global memory"}
        result = save_long_term(category=category, key=key, value=content,
                                source_mission=mission.mission_id,
                                tags=tags if isinstance(tags, list) else [])
        if result.get("ok"):
            mission.log_event("MEMORY", f"Global memory {result['action']}: {key}")
        return result
    else:
        from .memory import save_working_memory
        if not content:
            return {"ok": False, "error": "content required"}
        result = save_working_memory(mission.container_id, category, content,
                                     round_trip=mission.round_trips)
        if result.get("ok"):
            mission.log_event("MEMORY", f"Working memory saved ({category})")
        return result


def _action_recall_memory(mission, action):
    from .memory import recall_long_term
    query = action.get("query", "").strip()
    limit = action.get("limit", 10)
    if not query:
        return {"ok": False, "error": "query required"}
    results = recall_long_term(query, limit=min(limit, 20))
    formatted = [{"key": m["key"], "category": m.get("category", "?"),
                  "value": m["value"], "source_mission": m.get("source_mission")}
                 for m in results]
    mission.log_event("MEMORY", f"Recalled {len(formatted)} memories for: {query[:100]}")
    return {"ok": True, "count": len(formatted), "memories": formatted}


def _action_memory_file(mission, action, operation):
    """Handle file-based memory operations (create/read/update/append/delete/list)."""
    from .memory import (
        memory_create, memory_read, memory_update, memory_append,
        memory_delete, memory_list,
    )
    path = action.get("path", "").strip()
    content = action.get("content", "").strip()

    if operation == "list":
        result = memory_list(mission.container_id)
        if result.get("ok"):
            mission.log_event("MEMORY", f"Listed {result['count']} memory files")
        return result

    if operation == "create":
        if not path or not content:
            return {"ok": False, "error": "path and content required"}
        result = memory_create(mission.container_id, path, content)
        if result.get("ok"):
            mission.log_event("MEMORY", f"Created memory: {path}")
        return result

    if operation == "read":
        if not path:
            return {"ok": False, "error": "path required"}
        result = memory_read(mission.container_id, path)
        if result.get("ok"):
            mission.log_event("MEMORY", f"Read memory: {path}")
        return result

    if operation == "update":
        if not path or not content:
            return {"ok": False, "error": "path and content required"}
        result = memory_update(mission.container_id, path, content)
        if result.get("ok"):
            mission.log_event("MEMORY", f"Updated memory: {path}")
        return result

    if operation == "append":
        if not path or not content:
            return {"ok": False, "error": "path and content required"}
        result = memory_append(mission.container_id, path, content)
        if result.get("ok"):
            mission.log_event("MEMORY", f"Appended to memory: {path}")
        return result

    if operation == "delete":
        if not path:
            return {"ok": False, "error": "path required"}
        result = memory_delete(mission.container_id, path)
        if result.get("ok"):
            mission.log_event("MEMORY", f"Deleted memory: {path}")
        return result

    return {"ok": False, "error": f"Unknown memory operation: {operation}"}


def _action_advance_phase(mission, action):
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
    if not hasattr(mission, "phase_history"):
        mission.phase_history = []
    mission.phase_history.append({"phase": current, "exited_at": time.time()})
    mission.mission_phase = new_phase
    mission.log_event("PHASE", f"Advanced: {current} → {new_phase}")
    if mission.container_id:
        state_raw = _container_read_file(mission.container_id, "/home/mission/state.json")
        if state_raw:
            try:
                state_obj = json.loads(state_raw)
                state_obj["mission_phase"] = new_phase
                state_obj["phase_advanced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                _container_write_file(mission.container_id, "/home/mission/state.json",
                                      json.dumps(state_obj, indent=2))
            except (json.JSONDecodeError, TypeError):
                pass
    return {"ok": True, "previous": current, "current": new_phase}


def _action_reassign_agent(mission, action):
    agent_name = action.get("agent", "").strip()
    new_role = action.get("role", "").strip()
    new_experience = action.get("experience", "").strip()
    new_job_description = action.get("job_description", "").strip()
    if not agent_name:
        return {"ok": False, "error": "agent name required"}
    agent = mission.flock.get(agent_name)
    if not agent:
        available = ", ".join(mission.flock.keys()) or "none"
        return {"ok": False, "error": f"Unknown agent '{agent_name}'. Available: {available}"}
    if agent.assigned_task:
        return {"ok": False, "error": f"{agent_name} is busy on task {agent.assigned_task}"}
    if not new_role:
        return {"ok": False, "error": "role required"}
    old_role = agent.role
    agent.role = new_role[:100]
    if new_experience:
        agent.experience = new_experience
    if new_job_description:
        agent.system_prompt = generate_agent_identity_prompt(
            agent.name, agent.role, agent.experience, new_job_description, agent.model)
    else:
        agent.system_prompt = generate_agent_identity_prompt(
            agent.name, agent.role, agent.experience,
            f"You are now responsible for: {agent.role}.", agent.model)
    agent.conversation_history = []
    agent.scratchpad = {}
    agent.failures = 0
    mission.log_event("FLOCK", f"Reassigned {agent_name}: {old_role} → {agent.role}")
    return {"ok": True, "agent": agent_name, "old_role": old_role, "new_role": agent.role}


def _action_rebuild_flock(mission, action):
    busy = [n for n, a in mission.flock.items() if a.assigned_task]
    if busy:
        return {"ok": False, "error": f"Agents busy: {', '.join(busy)}. Wait or cancel first."}
    if not mission.flock:
        return {"ok": False, "error": "No flock agents."}
    old_roles = {n: a.role for n, a in mission.flock.items()}
    reassign_flock_roles(mission)
    new_roles = {n: a.role for n, a in mission.flock.items()}
    for agent in mission.flock.values():
        agent.conversation_history = []
        agent.scratchpad = {}
        agent.failures = 0
    changes = [f"{name}: {old_roles.get(name, '?')} → {role}"
               for name, role in new_roles.items() if old_roles.get(name) != role]
    mission.log_event("FLOCK", f"Flock rebuilt: {len(changes)} role changes")
    return {"ok": True, "agents": len(mission.flock), "changes": changes or ["no changes"]}


# ── Action registry ──────────────────────────────────────────────────────

ACTION_HANDLERS = {
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
    "publish_artifact":    _action_publish_artifact,
    "test_runner":         _action_test_runner,
    "save_memory":         _action_save_memory,
    "recall_memory":       _action_recall_memory,
    "memory_create":       lambda m, a: _action_memory_file(m, a, "create"),
    "memory_read":         lambda m, a: _action_memory_file(m, a, "read"),
    "memory_update":       lambda m, a: _action_memory_file(m, a, "update"),
    "memory_append":       lambda m, a: _action_memory_file(m, a, "append"),
    "memory_delete":       lambda m, a: _action_memory_file(m, a, "delete"),
    "memory_list":         lambda m, a: _action_memory_file(m, a, "list"),
    "advance_phase":       _action_advance_phase,
    "reassign_agent":      _action_reassign_agent,
    "rebuild_flock":       _action_rebuild_flock,
    "wait":                _action_wait_for_flock,
}


# ── Hardcoded deliverable verification ───────────────────────────────────

def _deliverable_verification_gate(mission):
    """Verify deliverables against mission request before allowing completion.

    Returns None if verification passes, or an error result dict if blocked.
    Checks are driven by the SR's own state.json — no hardcoded file types.
    """
    issues = []
    state_obj = None

    # 1. Parse state.json
    state_raw = _container_read_file(mission.container_id, "/home/mission/state.json")
    if state_raw:
        try:
            state_obj = json.loads(state_raw)
        except (json.JSONDecodeError, TypeError):
            issues.append("state.json is not valid JSON — fix it before completing")
    else:
        issues.append("state.json not found — create it with requirements tracking")

    # 2. Check all requirements are verified
    if state_obj is not None:
        reqs = state_obj.get("requirements", [])
        if reqs:
            unverified = [r for r in reqs if not r.get("verified")]
            if unverified:
                names = [r.get("id", r.get("desc", "?"))[:60] for r in unverified[:5]]
                issues.append(
                    f"UNVERIFIED REQUIREMENTS ({len(unverified)}/{len(reqs)}): "
                    + ", ".join(names)
                )
        else:
            issues.append(
                "state.json has no requirements array — "
                "you must extract requirements from the mission and track them"
            )

    # 3. Check SR-defined deliverables exist and are non-empty
    if state_obj is not None:
        deliverables = state_obj.get("deliverables", [])
        if deliverables:
            missing = []
            empty = []
            for path in deliverables:
                path = str(path).strip()
                if not path:
                    continue
                out, _, rc = _container_exec(
                    mission.container_id,
                    f"test -f {shlex.quote(path)} && wc -c < {shlex.quote(path)}",
                    timeout=5,
                )
                if rc != 0:
                    missing.append(path)
                else:
                    try:
                        size = int((out or "0").strip())
                        if size == 0:
                            empty.append(path)
                    except ValueError:
                        pass
            if missing:
                issues.append(
                    f"MISSING DELIVERABLES ({len(missing)}): "
                    + ", ".join(missing[:8])
                )
            if empty:
                issues.append(
                    f"EMPTY DELIVERABLES ({len(empty)}): "
                    + ", ".join(empty[:8])
                )
        else:
            issues.append(
                "state.json has no 'deliverables' array — "
                "list the file paths the mission must produce "
                "(e.g. [\"/home/mission/index.html\"])"
            )

    # 4. Check result.html exists
    res_out, _, res_rc = _container_exec(
        mission.container_id,
        "test -f /home/mission/result.html && wc -c < /home/mission/result.html",
        timeout=5,
    )
    if res_rc != 0:
        issues.append(
            "result.html not found — write it as your final deliverable summary"
        )
    elif res_out:
        try:
            size = int(res_out.strip())
            if size < 100:
                issues.append(f"result.html is only {size} bytes — it looks empty/stub")
        except (ValueError, IndexError):
            pass

    if issues:
        mission._completion_verified = False  # reset so SR gets the verification gate again
        mission.log_event("VERIFY_BLOCK",
                          f"Deliverable verification FAILED: {len(issues)} issue(s)")
        issue_text = "\n".join(f"  ✗ {issue}" for issue in issues)
        return {
            "ok": False,
            "error": (
                f"⛔ COMPLETION BLOCKED — deliverable verification failed:\n\n"
                f"{issue_text}\n\n"
                "Fix ALL issues above, then emit 'complete' again.\n"
                "The system will re-verify before allowing completion."
            ),
        }

    mission.log_event("VERIFY_PASS", "Deliverable verification passed")
    return None


def execute_action(mission, action):
    """Execute a single action. Returns result dict."""
    atype = action.get("type", "")

    if atype == "complete":
        if not getattr(mission, '_completion_verified', False):
            mission._completion_verified = True
            mission.log_event("VERIFY", "Pre-completion verification gate triggered")
            state_json = _container_read_file(mission.container_id, "/home/mission/state.json") or "not found"
            ls_out, _, _ = _container_exec(mission.container_id, "ls -la /home/mission/", timeout=5)
            elapsed_min = (time.time() - mission.created_at) / 60
            verify_out, _, verify_rc = _container_exec(
                mission.container_id,
                "test -x /home/mission/tools/verify && /home/mission/tools/verify /home/mission 2>&1 "
                "|| echo 'verify tool not available'", timeout=60)
            diff_out = _git_diff_since(mission.container_id, "HEAD~5") if mission.container_id else ""
            return {
                "ok": False, "verification_required": True,
                "message": (
                    f"⚠ VERIFICATION REQUIRED before completion (elapsed: {elapsed_min:.0f}min)\n\n"
                    f"=== Automated Verification ===\n{verify_out[:3000]}\n\n"
                    f"=== state.json ===\n{state_json[:3000]}\n\n"
                    f"=== Workspace ===\n{ls_out}\n\n"
                    f"=== Recent Changes ===\n{diff_out[:2000]}\n\n"
                    "Before completing, verify:\n"
                    "1. Check automated verification results — fix any FAIL items\n"
                    "2. Check EACH requirement in state.json — is it truly met?\n"
                    "3. Read deliverable files — are they complete?\n"
                    "4. For code: run it to verify it works\n"
                    "5. Mark each requirement verified:true in state.json\n"
                    "6. If anything is lacking, fix it NOW\n\n"
                    "If everything checks out, emit 'complete' again."
                ),
            }
        # ── Hardcoded deliverable verification gate ──
        gate = _deliverable_verification_gate(mission)
        if gate:
            return gate
        return _action_complete(mission, action)

    handler = ACTION_HANDLERS.get(atype)
    if handler:
        return handler(mission, action)

    mission.log_event("WARN", f"Unknown action type: {atype}")
    return {"ok": False, "error": f"unknown action type: {atype}"}
