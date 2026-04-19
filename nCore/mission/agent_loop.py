"""Autonomous agent iteration loop — prompt → parse → execute → repeat."""

import re
import shlex
import time

from .state import (
    _lock,
    _AUTO_MAX_ITERATIONS,
    _AUTO_MAX_SHELL,
    _AUTO_TIMEOUT,
    _AUTO_CHECKPOINT_INTERVAL,
    _AUTO_CHECKPOINT_SECONDS,
    _PREFLIGHT_HEADROOM,
    _CHARS_PER_TOKEN,
    _SHELL_TIMEOUT_DEFAULT,
    _SHELL_TIMEOUT_INSTALL,
    _AGENT_READONLY_FIRST_ITER,
    _READONLY_ACTIONS,
    _READONLY_SHELL_PREFIXES,
    _MAX_CONSECUTIVE_FAILURES,
    _SYNTAX_CHECK_EXTENSIONS,
)
from .scoring import (
    get_endpoint_ctx,
    is_context_overflow,
    model_quality_tier,
)
from registry import correct_endpoint_ctx
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
)
from .parsing import parse_response, diagnose_failure
from .showrunner import send_prompt_to_endpoint, wait_for_result
from .flock import build_agent_system_prompt


# ── Shared loop context ─────────────────────────────────────────────────

class _AgentCtx:
    """Mutable state shared across action handlers within one agent loop."""
    __slots__ = (
        'mission', 'task', 'agent', 'working_dir', 'max_shell',
        'agent_read_limit', 'files_written', 'files_read', 'blind_writes',
        'consecutive_failures', 'consecutive_install_failures',
        'shell_count', 'last_action_summary',
        'recent_errors', 'last_verify_at', 'verify_passes',
    )

    def __init__(self, mission, task, agent, working_dir, max_shell):
        self.mission = mission
        self.task = task
        self.agent = agent
        self.working_dir = working_dir
        self.max_shell = max_shell
        self.agent_read_limit = 2000
        self.files_written = []
        self.files_read = set()
        self.blind_writes = 0
        self.consecutive_failures = 0
        self.consecutive_install_failures = 0
        self.shell_count = 0
        self.last_action_summary = ""
        self.recent_errors = []
        self.last_verify_at = 0.0
        self.verify_passes = 0


# ── Action handlers ──────────────────────────────────────────────────────

def _handle_save_note(ctx, act):
    key = act.get("key", "").strip()[:100]
    value = act.get("value", "").strip()[:2000]
    if key and value:
        ctx.agent.scratchpad[key] = value
        if len(ctx.agent.scratchpad) > 20:
            oldest = next(iter(ctx.agent.scratchpad))
            del ctx.agent.scratchpad[oldest]
        result = f"save_note: saved '{key}'"
    else:
        result = "save_note: key and value required"
    ctx.consecutive_failures = 0
    ctx.last_action_summary = f"save_note: {key}"
    return result


def _handle_search(ctx, act):
    pattern = act.get("pattern", "")
    spath = act.get("path", ctx.working_dir)
    if not pattern:
        return "search: empty pattern"
    is_regex = act.get("regex", False)
    grep_flag = "-rn" if is_regex else "-rnF"
    search_lines = max(60, ctx.agent_read_limit // 100)
    cmd = (f"grep {grep_flag} --include='*' {shlex.quote(pattern)} "
           f"{shlex.quote(spath)} 2>/dev/null | head -{search_lines}")
    out, err, rc = _container_exec(ctx.mission.container_id, cmd, timeout=30)
    ctx.consecutive_failures = 0
    ctx.last_action_summary = f"search: {pattern}"
    if rc == 1 and not out:
        return "search: no matches"
    return f"search: {out.count(chr(10))} matches\n{out[:ctx.agent_read_limit]}"


def _handle_find_files(ctx, act):
    pattern = act.get("pattern", "")
    fpath = act.get("path", ctx.working_dir)
    if not pattern:
        return "find_files: pattern required (e.g. '*.py')"
    found = _find_files(ctx.mission.container_id, pattern, fpath)
    ctx.consecutive_failures = 0
    ctx.last_action_summary = f"find_files: {pattern}"
    return f"find_files: {len(found)} matches\n" + "\n".join(found[:100])


def _handle_file_info(ctx, act):
    fpath = act.get("path", "")
    if not fpath:
        return "file_info: path required"
    info = _file_info(ctx.mission.container_id, fpath)
    ctx.consecutive_failures = 0
    ctx.last_action_summary = f"file_info: {fpath}"
    if info:
        return (f"file_info: {fpath} — {info.get('lines', '?')} lines, "
                f"{info.get('size', 0)}B, type={info.get('type', '?')}")
    return f"file_info: {fpath} NOT FOUND"


def _handle_run_tool(ctx, act):
    tool_name = act.get("name", "")
    tool_args = act.get("args", [])
    if not tool_name:
        return "run_tool: name required"
    tool_entry = None
    for t in ctx.mission.tools:
        if t["name"] == tool_name:
            tool_entry = t
            break
    if not tool_entry:
        available = [t["name"] for t in ctx.mission.tools]
        return f"run_tool: '{tool_name}' not found. Available: {', '.join(available) or 'none'}"
    tool_path = f"/home/mission/tools/{tool_name}"
    if isinstance(tool_args, list):
        arg_str = " ".join(shlex.quote(str(a)) for a in tool_args)
    else:
        arg_str = str(tool_args)
    tool_cmd = f"cd {shlex.quote(ctx.working_dir)} && {shlex.quote(tool_path)} {arg_str}"
    tool_timeout = min(int(act.get("timeout", 120)), _SHELL_TIMEOUT_DEFAULT)
    out, err, rc = _container_exec(ctx.mission.container_id, tool_cmd, timeout=tool_timeout)
    result_str = f"run_tool({tool_name}): rc={rc}"
    if out:
        result_str += f" stdout={_smart_truncate(out, ctx.agent_read_limit, is_own_content=True)}"
    if err:
        result_str += f" stderr={err[:500]}"
    if rc == 0:
        ctx.consecutive_failures = 0
    else:
        ctx.consecutive_failures += 1
    ctx.last_action_summary = f"run_tool: {tool_name} → rc={rc}"
    return result_str


def _handle_shell(ctx, act):
    if ctx.shell_count >= ctx.max_shell:
        return "shell: LIMIT REACHED (max shell commands exceeded)"
    command = act.get("command", "")
    if not command:
        return "shell: empty command"
    shell_timeout = min(int(act.get("timeout", _SHELL_TIMEOUT_DEFAULT)), _SHELL_TIMEOUT_INSTALL)
    full_cmd = f"cd {shlex.quote(ctx.working_dir)} && {command}"
    out, err, rc = _container_exec(ctx.mission.container_id, full_cmd, timeout=shell_timeout)
    ctx.shell_count += 1
    result_str = f"shell: rc={rc}"
    if out:
        truncated_out = _smart_truncate(out, ctx.agent_read_limit, is_own_content=True)
        result_str += f" stdout={truncated_out}"
        if len(out) > ctx.agent_read_limit:
            result_str += " [OUTPUT TRUNCATED — pipe through head/tail/grep to narrow results]"
    if err:
        result_str += f" stderr={err[:ctx.agent_read_limit // 3]}"
    ctx.mission.log_event("SHELL", f"{command[:120]} → rc={rc}",
                          task_id=ctx.task.task_id, agent=ctx.agent.name)
    ctx.last_action_summary = f"shell: {command[:80]} → rc={rc}"
    if rc != 0:
        ctx.consecutive_failures += 1
        parsed_err = _parse_shell_error(err or out or "", rc)
        if parsed_err:
            category, key_info, suggestion = parsed_err
            result_str += (f"\n⚠ COMMAND FAILED (rc={rc})"
                           f"\n  ERROR TYPE: {category}"
                           f"\n  KEY INFO: {key_info}"
                           f"\n  SUGGESTION: {suggestion}")
            ctx.recent_errors.append(category)
            if len(ctx.recent_errors) > 5:
                ctx.recent_errors = ctx.recent_errors[-5:]
            recurrence = ctx.recent_errors.count(category)
            if recurrence >= 2:
                result_str += (f"\n  🔴 RECURRING ERROR: You've hit '{category}' "
                               f"{recurrence} times. Change your approach.")
            if recurrence <= 1 or recurrence >= 3:
                ctx.mission.log_event("ERROR_ANALYSIS",
                    f"agent={ctx.agent.name} {category}: {key_info[:100]}"
                    + (f" (×{recurrence})" if recurrence >= 2 else ""),
                    task_id=ctx.task.task_id, agent=ctx.agent.name)
            if isinstance(ctx.agent.scratchpad, dict):
                ctx.agent.scratchpad["last_error"] = f"{category}: {key_info}"[:200]

        _cmd_lower = command.lower()
        if any(kw in _cmd_lower for kw in ("pip install", "pip3 install", "apt-get install", "npm install")):
            ctx.consecutive_install_failures += 1
            if ctx.consecutive_install_failures >= 3:
                ctx.mission.log_event("AUTO_BAIL",
                    f"agent={ctx.agent.name} stuck in install loop "
                    f"({ctx.consecutive_install_failures} consecutive failures: {command[:80]})",
                    task_id=ctx.task.task_id, agent=ctx.agent.name)
                ctx.task.status = "failed"
                ctx.task.error = f"Install loop detected ({ctx.consecutive_install_failures} failures)"
                ctx.task.completed_at = time.time()
        else:
            ctx.consecutive_install_failures = 0
    else:
        ctx.consecutive_failures = 0
        ctx.consecutive_install_failures = 0
    return result_str


def _handle_write_file(ctx, act):
    path = act.get("path", "")
    content = act.get("content", "")
    append = act.get("append", False)
    if not path:
        return "write_file: no path"
    if not path.startswith(ctx.working_dir) and not path.startswith("/home/mission/"):
        path = ctx.working_dir.rstrip("/") + "/" + path.lstrip("/")
    parent = "/".join(path.split("/")[:-1])
    if parent:
        _container_exec(ctx.mission.container_id, f"mkdir -p {shlex.quote(parent)}", timeout=10)
    blind_write_warn = ""
    if not append and path not in ctx.files_read:
        _, _, _exists_rc = _container_exec(ctx.mission.container_id,
            f"test -f {shlex.quote(path)}", timeout=5)
        if _exists_rc == 0:
            ctx.blind_writes += 1
            blind_write_warn = (
                f"\n⚠ BLIND WRITE: You overwrote {path} without reading it first. "
                "Always read_file before write_file on existing files.")
    if append:
        existing = _container_read_file(ctx.mission.container_id, path) or ""
        full_content = existing + content
        ok = _container_write_file(ctx.mission.container_id, path, full_content)
        result_str = f"write_file(append): {path} ok={ok} (+{len(content)}B total={len(full_content)}B)"
    else:
        ok = _container_write_file(ctx.mission.container_id, path, content)
        result_str = f"write_file: {path} ok={ok} ({len(content)}B)"
        if ok:
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext in _SYNTAX_CHECK_EXTENSIONS:
                check_ok, errors = _syntax_check(ctx.mission.container_id, path)
                if not check_ok:
                    result_str += f" ⚠ SYNTAX ERROR: {errors}"
    if blind_write_warn:
        result_str += blind_write_warn
    if ok:
        ctx.mission.log_event("WRITE_FILE", f"{path} ({len(content)}B)",
                              task_id=ctx.task.task_id, agent=ctx.agent.name)
    ctx.last_action_summary = f"write_file: {path} ({len(content)}B)"
    if ok:
        ctx.files_written.append(path)
        ctx.consecutive_failures = 0
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("py", "js", "mjs", "ts") and len(ctx.files_written) % 3 == 0:
            lint_out, _, lint_rc = _container_exec(
                ctx.mission.container_id,
                f"test -x /home/mission/tools/lint && /home/mission/tools/lint {shlex.quote(path)} 2>&1 || true",
                timeout=15)
            if lint_rc != 0 and lint_out:
                result_str += f"\nauto-lint: ⚠ {lint_out[:500]}"
    else:
        ctx.consecutive_failures += 1
    return result_str


def _handle_read_file(ctx, act):
    path = act.get("path", "")
    if not path:
        return "read_file: no path"
    start_line = act.get("start_line")
    end_line = act.get("end_line")
    ctx.last_action_summary = f"read_file: {path}"
    if start_line is not None and end_line is not None:
        start_line = max(1, int(start_line))
        end_line = max(start_line, int(end_line))
        cmd = f"sed -n '{start_line},{end_line}p' {shlex.quote(path)}"
        fcontent, err, rc = _container_exec(ctx.mission.container_id, cmd, timeout=30)
        if rc != 0 or fcontent is None:
            ctx.consecutive_failures += 1
            return f"read_file: {path} NOT FOUND"
        ctx.files_read.add(path)
        ctx.consecutive_failures = 0
        display = _smart_truncate(fcontent, ctx.agent_read_limit, is_own_content=True)
        return f"read_file: {path} lines {start_line}-{end_line} ({len(fcontent)}B)\n{display}"
    fcontent = _container_read_file(ctx.mission.container_id, path)
    if fcontent is not None:
        ctx.files_read.add(path)
        total_lines = fcontent.count('\n') + (1 if fcontent and not fcontent.endswith('\n') else 0)
        if len(fcontent) > ctx.agent_read_limit:
            lines_list = fcontent.split("\n")
            head = "\n".join(lines_list[:20])
            tail = "\n".join(lines_list[-10:]) if len(lines_list) > 30 else ""
            display = head
            if tail:
                display += f"\n\n... [{total_lines - 30} lines omitted] ...\n\n{tail}"
            display += (f"\n\n[FILE: {total_lines} lines, {len(fcontent)}B — "
                        f"use read_file with start_line/end_line for specific sections]")
        else:
            display = fcontent
        ctx.consecutive_failures = 0
        return f"read_file: {path} ({len(fcontent)}B, {total_lines} lines)\n{display}"
    ctx.consecutive_failures += 1
    return f"read_file: {path} NOT FOUND"


def _handle_batch_read(ctx, act):
    paths = act.get("paths", [])
    if not paths or not isinstance(paths, list):
        return "batch_read: paths must be a non-empty array"
    per_file_limit = max(ctx.agent_read_limit // max(len(paths), 1), 1500)
    batch_parts = []
    for p in paths[:15]:
        fcontent = _container_read_file(ctx.mission.container_id, p)
        if fcontent is None:
            batch_parts.append(f"--- {p}: NOT FOUND ---")
        else:
            ctx.files_read.add(p)
            display = _smart_truncate(fcontent, per_file_limit, is_own_content=True)
            batch_parts.append(f"--- {p} ({len(fcontent)}B) ---\n{display}")
    ctx.consecutive_failures = 0
    ctx.last_action_summary = f"batch_read: {len(paths)} files"
    return f"batch_read: {len(paths)} files\n" + "\n".join(batch_parts)


def _handle_workspace_tree(ctx, act):
    tree_path = act.get("path", "/home/mission/")
    tree = _build_workspace_tree(ctx.mission.container_id, tree_path)
    ctx.last_action_summary = f"workspace_tree: {tree_path}"
    if tree:
        ctx.consecutive_failures = 0
        return f"workspace_tree: {tree_path}\n{tree}"
    return "workspace_tree: empty or failed"


def _handle_patch_file(ctx, act):
    path = act.get("path", "")
    old_text = act.get("old", "")
    new_text = act.get("new", "")
    if not path or not old_text:
        return "patch_file: path and old text required"
    fcontent = _container_read_file(ctx.mission.container_id, path)
    if fcontent is None:
        ctx.consecutive_failures += 1
        return f"patch_file: {path} NOT FOUND"
    cnt = fcontent.count(old_text)
    if cnt == 0:
        ctx.consecutive_failures += 1
        return "patch_file: old text not found — read_file first to see exact content"
    if cnt > 1:
        ctx.consecutive_failures += 1
        return f"patch_file: old text matches {cnt} locations — include more context lines to be specific"
    new_content = fcontent.replace(old_text, new_text, 1)
    ok = _container_write_file(ctx.mission.container_id, path, new_content)
    result_str = f"patch_file: {path} ok={ok} (-{len(old_text)}B +{len(new_text)}B)"
    if ok:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in _SYNTAX_CHECK_EXTENSIONS:
            check_ok, errors = _syntax_check(ctx.mission.container_id, path)
            if not check_ok:
                result_str += f" ⚠ SYNTAX ERROR: {errors}"
        ctx.mission.log_event("PATCH_FILE", f"{path} (-{len(old_text)}B +{len(new_text)}B)",
                              task_id=ctx.task.task_id, agent=ctx.agent.name)
        ctx.files_written.append(path)
        ctx.consecutive_failures = 0
    else:
        ctx.consecutive_failures += 1
    ctx.last_action_summary = f"patch_file: {path}"
    return result_str


def _handle_replace_lines(ctx, act):
    path = act.get("path", "")
    sl = act.get("start_line")
    el = act.get("end_line")
    new_text = act.get("content", "")
    if not path or sl is None or el is None:
        return "replace_lines: path, start_line, end_line, and content required"
    sl = max(1, int(sl))
    el = max(sl, int(el))
    ok, total = _replace_lines(ctx.mission.container_id, path, sl, el, new_text)
    if not ok:
        ctx.consecutive_failures += 1
        return f"replace_lines: {path} FAILED (file not found or write error)"
    new_line_count = len(new_text.split("\n")) if new_text else 0
    result_str = f"replace_lines: {path} lines {sl}-{el} replaced with {new_line_count} lines (total: {total})"
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _SYNTAX_CHECK_EXTENSIONS:
        check_ok, errors = _syntax_check(ctx.mission.container_id, path)
        if not check_ok:
            result_str += f" ⚠ SYNTAX ERROR: {errors}"
    ctx.mission.log_event("REPLACE_LINES", f"{path} lines {sl}-{el}",
                          task_id=ctx.task.task_id, agent=ctx.agent.name)
    ctx.files_written.append(path)
    ctx.consecutive_failures = 0
    ctx.last_action_summary = f"replace_lines: {path} {sl}-{el}"
    return result_str


def _handle_apply_diff(ctx, act):
    diff_text = act.get("diff", "")
    diff_path = act.get("path", "")
    if not diff_text:
        return "apply_diff: diff content required"
    ok, output = _apply_diff(ctx.mission.container_id, diff_path, diff_text)
    result_str = f"apply_diff: ok={ok} {output}"
    if ok and diff_path:
        ext = diff_path.rsplit(".", 1)[-1].lower() if "." in diff_path else ""
        if ext in _SYNTAX_CHECK_EXTENSIONS:
            check_ok, errors = _syntax_check(ctx.mission.container_id, diff_path)
            if not check_ok:
                result_str += f" ⚠ SYNTAX ERROR: {errors}"
    if ok:
        ctx.consecutive_failures = 0
    else:
        ctx.consecutive_failures += 1
    ctx.last_action_summary = f"apply_diff: {diff_path or 'multi'}"
    return result_str


def _handle_test_runner(ctx, act):
    from .actions import _action_test_runner
    test_result = _action_test_runner(ctx.mission, act)
    if test_result.get("ok"):
        result_str = (f"test_runner: ✓ PASSED — "
                      f"{test_result.get('passed', 0)} passed, "
                      f"{test_result.get('failed', 0)} failed")
        ctx.consecutive_failures = 0
    else:
        err_summary = ""
        for err in test_result.get("errors", [])[:5]:
            err_summary += f"\n  ✗ {err.get('test', '?')}: {err.get('message', '')[:150]}"
        result_str = (f"test_runner: ✗ FAILED — "
                      f"{test_result.get('passed', 0)} passed, "
                      f"{test_result.get('failed', 0)} failed"
                      f"{err_summary}")
        if not err_summary and test_result.get("output"):
            result_str += f"\n  Output: {test_result['output'][:500]}"
        ctx.consecutive_failures += 1
    ctx.last_action_summary = f"test_runner: {'PASS' if test_result.get('ok') else 'FAIL'}"
    return result_str


# ── Mid-task verification ────────────────────────────────────────────────

_MIDTASK_VERIFY_COOLDOWN = 60
_MIDTASK_VERIFY_FILE_THRESHOLD = 3


def _maybe_run_midtask_verify(ctx):
    """Run verify_cmd mid-task if conditions met. Returns feedback string or empty."""
    verify_cmd = ctx.task.constraints.get("verify_cmd")
    if not verify_cmd or not ctx.mission.container_id:
        return ""
    if len(ctx.files_written) < _MIDTASK_VERIFY_FILE_THRESHOLD:
        return ""
    now = time.time()
    if now - ctx.last_verify_at < _MIDTASK_VERIFY_COOLDOWN:
        return ""
    ctx.last_verify_at = now
    v_cmd = f"cd /home/mission && {verify_cmd}"
    v_out, v_err, v_rc = _container_exec(ctx.mission.container_id, v_cmd, timeout=30)
    v_output = ((v_out or "") + (v_err or ""))[:800]
    if v_rc == 0:
        ctx.verify_passes += 1
        ctx.mission.log_event("MID_VERIFY",
            f"task={ctx.task.task_id} PASS (pass #{ctx.verify_passes})",
            task_id=ctx.task.task_id, agent=ctx.agent.name)
        return "\n📋 MID-TASK VERIFICATION: ✓ PASSED"
    else:
        ctx.mission.log_event("MID_VERIFY",
            f"task={ctx.task.task_id} FAIL rc={v_rc}",
            task_id=ctx.task.task_id, agent=ctx.agent.name)
        return (f"\n📋 MID-TASK VERIFICATION: ✗ FAILED (rc={v_rc})\n"
                f"{v_output}\n"
                f"Fix these issues before claiming done.")


# ── Structured shell error parsing ───────────────────────────────────────

_ERROR_PATTERNS = [
    (r'File "(.+?)", line (\d+)', "PythonError",
     lambda m: f"{m.group(1)}:{m.group(2)}"),
    (r'(\w+Error): (.+)', "PythonException",
     lambda m: f"{m.group(1)}: {m.group(2)[:120]}"),
    (r'error TS(\d+): (.+)', "TypeScriptError",
     lambda m: f"TS{m.group(1)}: {m.group(2)[:120]}"),
    (r'SyntaxError: (.+)', "SyntaxError",
     lambda m: f"SyntaxError: {m.group(1)[:120]}"),
    (r'ModuleNotFoundError: (.+)', "ModuleNotFound",
     lambda m: f"ModuleNotFoundError: {m.group(1)[:120]}"),
    (r'ENOENT.*no such file.*[\'"](.+?)[\'"]', "FileNotFound",
     lambda m: f"File not found: {m.group(1)}"),
    (r'command not found: (\S+)', "CommandNotFound",
     lambda m: f"Command not found: {m.group(1)}"),
    (r'Permission denied', "PermissionDenied",
     lambda m: "Permission denied"),
    (r'Cannot find module [\'"](.+?)[\'"]', "ModuleNotFound",
     lambda m: f"Cannot find module: {m.group(1)}"),
]

_ERROR_SUGGESTIONS = {
    "PythonError": "Check the file and line number indicated. Read the traceback bottom-up.",
    "PythonException": "Read the exception message. Check imports and variable names.",
    "TypeScriptError": "Fix the TypeScript error at the indicated location.",
    "SyntaxError": "Check for missing brackets, quotes, or colons near the error location.",
    "ModuleNotFound": "Install the missing module (pip install / npm install) or fix the import path.",
    "FileNotFound": "Verify the file path exists. Use find_files or ls to check.",
    "CommandNotFound": "Install the required tool or check the command spelling.",
    "PermissionDenied": "Check file permissions. You may need chmod or sudo.",
}


def _parse_shell_error(stderr, rc):
    """Parse shell stderr for common error patterns."""
    combined = stderr or ""
    for pattern, category, extractor in _ERROR_PATTERNS:
        match = re.search(pattern, combined)
        if match:
            key_info = extractor(match)
            suggestion = _ERROR_SUGGESTIONS.get(category, "Read the error message and adapt your approach.")
            return category, key_info, suggestion
    return None


_AGENT_ACTION_HANDLERS = {
    "save_note": _handle_save_note,
    "search": _handle_search,
    "find_files": _handle_find_files,
    "file_info": _handle_file_info,
    "run_tool": _handle_run_tool,
    "shell": _handle_shell,
    "write_file": _handle_write_file,
    "read_file": _handle_read_file,
    "batch_read": _handle_batch_read,
    "workspace_tree": _handle_workspace_tree,
    "patch_file": _handle_patch_file,
    "replace_lines": _handle_replace_lines,
    "apply_diff": _handle_apply_diff,
    "test_runner": _handle_test_runner,
}


# ── Quality gates ────────────────────────────────────────────────────────

_PLACEHOLDER_MARKERS = ("placeholder", "todo", "lorem ipsum", "sample output",
                        "will be", "to be completed", "tbd", "example")
_MIN_WORK_ITERS = {"implement": 4, "fix": 3}


def _check_done_quality(ctx, done_summary, iteration, max_iterations):
    """Check quality gates for agent 'done' claim. Returns rejection message or None."""
    agent_tier = model_quality_tier(ctx.agent.model)
    if agent_tier <= 1:
        is_placeholder = len(done_summary.strip()) < 30
    else:
        is_placeholder = (
            len(done_summary.strip()) < 50
            or any(m in done_summary.lower() for m in _PLACEHOLDER_MARKERS)
        )
    if is_placeholder and iteration < max_iterations:
        ctx.mission.log_event("QUALITY_REJECT",
            f"task={ctx.task.task_id} agent={ctx.agent.name} — rejected placeholder result "
            f"({len(done_summary)} chars) at iter {iteration}",
            task_id=ctx.task.task_id, agent=ctx.agent.name)
        ctx.consecutive_failures += 1
        return ("❌ REJECTED: Your 'done' summary is too short or appears to be a placeholder. "
                "A task result must contain substantive completed work (>50 chars, no placeholders). "
                "Continue working and deliver real results.")

    pt_type = ctx.task.constraints.get("plan_task_type", "")
    min_required = _MIN_WORK_ITERS.get(pt_type, 0)
    if iteration < min_required and iteration < max_iterations:
        ctx.mission.log_event("QUALITY_REJECT",
            f"task={ctx.task.task_id} agent={ctx.agent.name} — premature done at iter "
            f"{iteration} (min {min_required} for {pt_type})",
            task_id=ctx.task.task_id, agent=ctx.agent.name)
        ctx.consecutive_failures += 1
        return (f"❌ REJECTED: You claimed 'done' at iteration {iteration}, but "
                f"{pt_type} tasks require substantive work. You've barely started. "
                "Verify your output actually meets the requirements. "
                "Continue working.")

    verify_cmd = ctx.task.constraints.get("verify_cmd")
    if verify_cmd and ctx.mission.container_id and iteration < max_iterations:
        v_cmd = f"cd /home/mission && {verify_cmd}"
        v_out, v_err, v_rc = _container_exec(ctx.mission.container_id, v_cmd, timeout=30)
        if v_rc != 0:
            v_output = ((v_out or "") + (v_err or ""))[:1500]
            ctx.mission.log_event("VERIFY_REJECT",
                f"task={ctx.task.task_id} agent={ctx.agent.name} — verify_cmd failed "
                f"(rc={v_rc}) at iter {iteration}",
                task_id=ctx.task.task_id, agent=ctx.agent.name)
            ctx.consecutive_failures += 1

            pt_type = ctx.task.constraints.get("plan_task_type", "")
            if pt_type in ("test", "verify") and ("pytest" in verify_cmd or "test" in verify_cmd):
                failure_lines = []
                for line in v_output.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("FAILED ") or stripped.startswith("ERROR "):
                        failure_lines.append(stripped)
                    elif "AssertionError" in stripped or "assert " in stripped:
                        failure_lines.append(stripped)
                if failure_lines:
                    structured_feedback = "\n".join(failure_lines[:10])
                    return (f"❌ TEST FAILURES ({len(failure_lines)} failures detected):\n"
                            f"{structured_feedback}\n\n"
                            f"Full output:\n{v_output}\n\n"
                            f"Fix EACH failing test.")
                return (f"❌ TESTS FAILED (exit code {v_rc}):\n"
                        f"{v_output}\n\n"
                        f"Fix the failing tests.")
            return (f"❌ VERIFICATION FAILED:\n"
                    f"Command: {verify_cmd}\n"
                    f"Exit code: {v_rc}\n"
                    f"Output: {v_output}\n\n"
                    f"Fix the issues and try again.")

        ctx.mission.log_event("VERIFY_PASS",
            f"task={ctx.task.task_id} agent={ctx.agent.name} — verify_cmd passed at iter {iteration}",
            task_id=ctx.task.task_id, agent=ctx.agent.name)

    return None


# ── Main loop ────────────────────────────────────────────────────────────

def agent_autonomous_loop(mission, task, agent):
    """Run an autonomous agent loop: prompt → parse actions → execute → repeat."""
    try:
        _agent_autonomous_loop_inner(mission, task, agent)
    except Exception as exc:
        import traceback
        mission.log_event("AUTO_CRASH",
                          f"agent={agent.name} task={task.task_id} CRASHED: {exc}\n"
                          f"{traceback.format_exc()[-500:]}",
                          task_id=task.task_id, agent=agent.name)
        if task.status not in ("done", "failed", "timed_out", "cancelled"):
            task.status = "failed"
            task.error = f"Agent loop crashed: {exc}"
            task.completed_at = time.time()
    finally:
        # Always clean up: free the agent and move task to history
        with _lock:
            if agent.assigned_task == task.task_id:
                agent.assigned_task = None
                agent.status = "available"
            # Ensure task lands in history and is removed from active
            if task.task_id in mission.tasks:
                if task.status not in ("done", "failed", "timed_out", "cancelled"):
                    task.status = "failed"
                    task.error = task.error or "Agent loop ended unexpectedly"
                    task.completed_at = task.completed_at or time.time()
                mission.task_history.append(task.to_dict())
                mission.tasks.pop(task.task_id, None)


def _agent_autonomous_loop_inner(mission, task, agent):
    """Inner loop body — wrapped by agent_autonomous_loop for crash safety."""
    constraints = task.constraints
    max_iterations = constraints.get("max_iterations", _AUTO_MAX_ITERATIONS)
    max_shell = constraints.get("max_shell_commands", _AUTO_MAX_SHELL)
    timeout = constraints.get("timeout", _AUTO_TIMEOUT)
    working_dir = constraints.get("working_dir", "/home/mission/")
    allowed_caps = set(task.capabilities or [
        "shell", "write_file", "read_file", "batch_read", "workspace_tree",
        "search", "patch_file", "replace_lines", "apply_diff",
        "find_files", "file_info", "save_note", "run_tool",
    ])

    # Agent demotion for high failure rates
    _aperf = mission._agent_perf.get(agent.name)
    if _aperf and _aperf.get("total_tasks", 0) >= 3:
        fail_rate = _aperf["task_failures"] / _aperf["total_tasks"]
        if fail_rate >= 0.5:
            demotion_cap = max(3, max_iterations // 2)
            if max_iterations > demotion_cap:
                max_iterations = demotion_cap
                if not _aperf.get("_demoted"):
                    _aperf["_demoted"] = True
                    mission.log_event("DEMOTION",
                        f"{agent.name} fail rate {fail_rate:.0%} — "
                        f"max_iterations capped at {demotion_cap}",
                        agent=agent.name)

    # TPS-based scaling
    agent_tps = agent.toks_per_sec or 0
    if agent_tps > 0 and agent_tps < 30 and max_iterations > 10:
        tps_ratio = min(1.0, agent_tps / 30.0)
        tps_cap = max(5, int(max_iterations * (0.3 + 0.7 * tps_ratio)))
        if tps_cap < max_iterations:
            mission.log_event("DEMOTION",
                f"{agent.name} slow ({agent_tps:.0f} tps) — "
                f"max_iterations {max_iterations}→{tps_cap}",
                agent=agent.name)
            max_iterations = tps_cap

    gen_overrides = {}
    if constraints.get("max_tokens"):
        gen_overrides["max_tokens"] = constraints["max_tokens"]
    if constraints.get("generation_timeout"):
        gen_overrides["generation_timeout"] = constraints["generation_timeout"]
    if constraints.get("no_gen_limit"):
        gen_overrides["no_gen_limit"] = True

    start_time = time.time()
    ctx = _AgentCtx(mission, task, agent, working_dir, max_shell)
    last_checkpoint_time = start_time
    consecutive_agent_errors = 0
    _MAX_CONSECUTIVE_AGENT_ERRORS = 3
    iteration = 0

    mission.log_event("AUTO_START",
                      f"task={task.task_id} agent={agent.name} max_iter={max_iterations} timeout={timeout}s",
                      task_id=task.task_id, agent=agent.name)
    task.status = "running"

    # Load conversation history from prior dispatches
    agent_messages = []
    if agent.conversation_history:
        agent_messages = list(agent.conversation_history)
        while agent_messages and agent_messages[-1].get("role") == "user":
            agent_messages.pop()

    iter_intro = f"\n\n📋 Iteration 1/{max_iterations}."
    if max_iterations == 1:
        iter_intro += (" This is your ONLY iteration — deliver complete results now. "
                      "Emit {\"type\": \"done\", \"summary\": \"...\"} with your final output.")
    elif max_iterations <= 3:
        iter_intro += " Budget is tight — be efficient. Emit {\"type\": \"done\"} when finished."
    else:
        iter_intro += " Emit {\"type\": \"done\", \"summary\": \"...\"} when finished to end early."
    agent_messages.append({"role": "user", "content": task.prompt + iter_intro})

    iteration = 0
    for iteration in range(1, max_iterations + 1):
        # Check cancellation
        if task._cancel_event.is_set():
            mission.log_event("AUTO_CANCELLED",
                              f"task={task.task_id} agent={agent.name} at iteration {iteration}",
                              task_id=task.task_id, agent=agent.name)
            task.status = "cancelled"
            task.result = f"Cancelled at iteration {iteration}. Last action: {ctx.last_action_summary}"
            break

        # Inject SR coaching guidance
        try:
            guidance = task._sr_guidance.popleft()
            agent_messages.append({"role": "user", "content":
                f"📋 SHOWRUNNER GUIDANCE: {guidance}"})
            mission.log_event("SR_GUIDANCE",
                f"Injected guidance to {agent.name}: {guidance[:120]}",
                task_id=task.task_id, agent=agent.name)
        except IndexError:
            pass

        # Wall-clock timeout
        elapsed = time.time() - start_time
        if elapsed > timeout:
            mission.log_event("AUTO_TIMEOUT",
                              f"task={task.task_id} agent={agent.name} elapsed={elapsed:.0f}s",
                              task_id=task.task_id, agent=agent.name)
            task.status = "timed_out"
            task.error = f"Autonomous timeout after {elapsed:.0f}s, {iteration-1} iterations"
            break

        # Checkpoint
        now = time.time()
        if (iteration % _AUTO_CHECKPOINT_INTERVAL == 0 or
                (now - last_checkpoint_time) > _AUTO_CHECKPOINT_SECONDS):
            task.checkpoint = {
                "task_id": task.task_id, "agent": agent.name,
                "iteration": iteration, "max_iterations": max_iterations,
                "elapsed": now - start_time, "shell_commands": ctx.shell_count,
                "last_action": ctx.last_action_summary,
                "files_written": ctx.files_written[-5:],
                "status": "stuck" if ctx.consecutive_failures >= 2 else "working",
            }
            last_checkpoint_time = now
            mission.log_event("AUTO_CHECKPOINT",
                              f"task={task.task_id} iter={iteration}/{max_iterations} "
                              f"elapsed={now - start_time:.0f}s shells={ctx.shell_count}",
                              task_id=task.task_id, agent=agent.name)

        # Bail on repeated failures
        if ctx.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            mission.log_event("AUTO_BAIL",
                f"task={task.task_id} agent={agent.name} — "
                f"{ctx.consecutive_failures} consecutive failures",
                task_id=task.task_id, agent=agent.name)
            task.status = "failed"
            task.error = (
                f"Bailed out after {ctx.consecutive_failures} consecutive failures "
                f"at iteration {iteration}. Last: {ctx.last_action_summary}")
            task.completed_at = time.time()
            if mission.container_id:
                from .memory import save_working_memory
                pt_id = task.constraints.get("plan_task_id", task.task_id)
                save_working_memory(mission.container_id, "error",
                    f"{agent.name} failed {pt_id} after {ctx.consecutive_failures} failures: "
                    f"{ctx.last_action_summary[:200]}",
                    round_trip=iteration)
            break

        # Prompt the agent
        system_prompt = build_agent_system_prompt(agent, mission=mission)

        if isinstance(agent.scratchpad, dict) and agent.scratchpad:
            pad_lines = [f"- {k}: {v}" for k, v in agent.scratchpad.items()]
            system_prompt += "\n\nYOUR SCRATCHPAD (persistent notes):\n" + "\n".join(pad_lines)

        if mission.container_id and iteration == 1:
            from .memory import format_working_memory_for_context
            wm_context = format_working_memory_for_context(mission.container_id)
            if wm_context:
                system_prompt += "\n\n" + wm_context

        if mission.tools:
            tool_lines = [f"- /home/mission/tools/{t['name']}: {t.get('description', '')}"
                          for t in mission.tools]
            system_prompt += ("\n\nAVAILABLE TOOLS (use run_tool or shell to invoke):\n"
                              + "\n".join(tool_lines))

        _kb_lines = []
        if getattr(mission, 'knowledge_base', None):
            _kb_lines.extend(f"- {k}: {v}" for k, v in mission.knowledge_base.items())
        if getattr(mission, 'notes', None):
            _kb_set = set(mission.knowledge_base or {})
            for note in mission.notes:
                if note.get("key") not in _kb_set:
                    _kb_lines.append(f"- {note['key']}: {note['value']}")
        if _kb_lines:
            system_prompt += "\n\nMISSION KNOWLEDGE (shared across all agents):\n" + "\n".join(_kb_lines)

        agent_ctx = agent.context_length or get_endpoint_ctx(agent.node_id, agent.model)
        agent_window = max(6, min(int((agent_ctx or 4096) / 2048), 40))
        messages = [{"role": "system", "content": system_prompt}] + agent_messages[-agent_window:]

        # Preflight: estimate tokens & trim
        est_tokens = sum(len(m.get("content", "")) // _CHARS_PER_TOKEN + 4 for m in messages)
        ctx_budget = int((agent_ctx or 4096) * _PREFLIGHT_HEADROOM)
        while est_tokens > ctx_budget and len(messages) > 3:
            messages.pop(1)
            est_tokens = sum(len(m.get("content", "")) // _CHARS_PER_TOKEN + 4 for m in messages)

        if est_tokens > ctx_budget:
            for m in sorted(messages, key=lambda x: len(x.get("content", "")), reverse=True):
                if m.get("role") == "system":
                    continue
                content = m.get("content", "")
                excess_chars = (est_tokens - ctx_budget) * _CHARS_PER_TOKEN
                if excess_chars <= 0:
                    break
                if len(content) > 2000:
                    cut = min(int(excess_chars), len(content) - 1000)
                    m["content"] = content[:500] + f"\n... [{cut} chars truncated] ...\n" + content[-500:]
                    est_tokens = sum(len(x.get("content", "")) // _CHARS_PER_TOKEN + 4 for x in messages)

        orch_task_id, wait_timeout = send_prompt_to_endpoint(
            agent.node_id, agent.model, messages, mission.mission_id, task.task_id,
            role="worker", overrides=gen_overrides or None,
        )
        result = wait_for_result(orch_task_id, timeout=wait_timeout)

        if not result:
            ctx.consecutive_failures += 1
            consecutive_agent_errors = 0
            ctx.last_action_summary = "inference timeout"
            agent_messages.append({"role": "assistant", "content": '{"thinking":"timeout","actions":[]}'})
            agent_messages.append({"role": "user", "content": "Your last response timed out. Try a simpler approach."})
            continue

        # Agent-side error handling
        if result.get("_agent_error"):
            is_overflow, n_prompt, n_ctx_real = is_context_overflow(result)
            if is_overflow:
                if n_ctx_real and n_ctx_real < (agent.context_length or 999999):
                    mission.log_event("WARN",
                        f"agent={agent.name} context corrected: "
                        f"was={agent.context_length} actual={n_ctx_real}",
                        task_id=task.task_id, agent=agent.name)
                    agent.context_length = n_ctx_real
                    agent_ctx = n_ctx_real
                    correct_endpoint_ctx(agent.node_id, agent.model, n_ctx_real)

                keep_first = 1
                keep_last = 2
                if len(agent_messages) > keep_first + keep_last:
                    agent_messages = agent_messages[:keep_first] + agent_messages[-keep_last:]
                agent_window = max(6, min(int((agent_ctx or 4096) / 2048), 40))

                effective_ctx = n_ctx_real or agent_ctx or 4096
                char_budget = int(effective_ctx * _PREFLIGHT_HEADROOM * _CHARS_PER_TOKEN)
                total_chars = sum(len(m.get("content", "")) for m in agent_messages)
                if total_chars > char_budget:
                    for m in sorted(agent_messages, key=lambda x: len(x.get("content", "")), reverse=True):
                        excess = total_chars - char_budget
                        if excess <= 0:
                            break
                        content = m.get("content", "")
                        if len(content) > 2000:
                            cut = min(excess, len(content) - 1000)
                            m["content"] = content[:500] + f"\n... [{cut} chars truncated] ...\n" + content[-500:]
                            total_chars -= cut

                mission.log_event("CONTEXT",
                    f"agent={agent.name} overflow recovery: trimmed to {len(agent_messages)} msgs",
                    task_id=task.task_id, agent=agent.name)
                ctx.consecutive_failures += 1
                ctx.last_action_summary = "context overflow (trimmed)"
                continue

            ctx.consecutive_failures += 1
            consecutive_agent_errors += 1
            ctx.last_action_summary = f"agent error: {result.get('error', 'unknown')}"
            mission.log_event("AGENT_ERROR", f"agent={agent.name} error={result.get('error', '')}",
                              task_id=task.task_id, agent=agent.name)

            if consecutive_agent_errors >= _MAX_CONSECUTIVE_AGENT_ERRORS:
                mission.log_event("AUTO_BAIL",
                    f"agent={agent.name} — {consecutive_agent_errors} consecutive endpoint errors",
                    task_id=task.task_id, agent=agent.name)
                task.status = "failed"
                task.error = (
                    f"Endpoint errors ({consecutive_agent_errors}x) — "
                    f"node {agent.node_id} likely offline. Last: {result.get('error', '')[:200]}")
                task.completed_at = time.time()
                break

            agent_messages.append({"role": "assistant", "content": '{"thinking":"error","actions":[]}'})
            agent_messages.append({"role": "user", "content": "Agent error occurred. Try again."})
            continue

        # Extract text
        consecutive_agent_errors = 0
        choices = result.get("choices", [])
        msg = choices[0].get("message", {}) if choices else {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        content_sans_think = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip() if content else ""
        if content_sans_think:
            text = content
        elif reasoning:
            text = reasoning
        else:
            text = content
        usage = result.get("usage", {})
        comp_tokens = usage.get("completion_tokens", 0)
        mission.log_event("AGENT_RESPONSE",
                          f"agent={agent.name} iter={iteration} "
                          f"chars={len(text)} completion_tokens={comp_tokens}",
                          task_id=task.task_id, agent=agent.name)

        if not text:
            ctx.consecutive_failures += 1
            ctx.last_action_summary = "empty response"
            agent_messages.append({"role": "assistant", "content": '{"thinking":"empty","actions":[]}'})
            agent_messages.append({"role": "user", "content": "Empty response. Try again."})
            continue

        agent_messages.append({"role": "assistant", "content": text})
        task.task_context = agent_messages[-agent_window:]

        # Parse agent response
        parsed = parse_response(text)
        if not parsed or not parsed.get("actions"):
            ctx.consecutive_failures += 1
            ctx.last_action_summary = "unparseable response"
            diag = diagnose_failure(text)
            agent_messages.append({"role": "user", "content":
                f"Could not parse your response as JSON. {diag}\n"
                "Respond with a JSON object containing 'actions' array. Raw JSON only, no markdown."})
            continue

        # Execute actions
        action_results = []
        done = False
        done_summary = ""
        ctx.agent_read_limit = max(2000, int((agent_ctx or 8192) * 0.25))
        if (agent_ctx or 8192) < 8192:
            ctx.agent_read_limit = min(ctx.agent_read_limit, 2000)

        is_readonly_iter = (_AGENT_READONLY_FIRST_ITER and iteration == 1
                           and max_iterations > 2)

        for act in parsed.get("actions", []):
            atype = act.get("type", "")

            if atype == "done":
                done = True
                done_summary = act.get("summary", "Task completed.")
                break

            if atype not in allowed_caps and atype != "save_note":
                action_results.append(f"{atype}: NOT ALLOWED (capabilities: {', '.join(allowed_caps)})")
                continue

            if is_readonly_iter and atype not in _READONLY_ACTIONS:
                if atype == "shell":
                    cmd_word = act.get("command", "").strip().split()[0] if act.get("command") else ""
                    if not any(cmd_word.startswith(p) for p in _READONLY_SHELL_PREFIXES):
                        action_results.append(
                            f"shell: BLOCKED — first iteration is read-only. Inspect first.")
                        continue
                else:
                    action_results.append(
                        f"{atype}: BLOCKED — first iteration is read-only. "
                        f"Inspect the workspace first, then write/modify in iteration 2.")
                    continue

            handler = _AGENT_ACTION_HANDLERS.get(atype)
            if handler:
                result_str = handler(ctx, act)
                if result_str:
                    action_results.append(result_str)
                if ctx.task.status == "failed":
                    break
            else:
                action_results.append(f"{atype}: unknown action type")

        if task.status == "failed":
            break

        if done:
            rejection = _check_done_quality(ctx, done_summary, iteration, max_iterations)
            if rejection:
                agent_messages.append({"role": "user", "content": rejection})
                continue

            task.status = "done"
            task.result = done_summary
            task.completed_at = time.time()
            latency = task.completed_at - task.created_at
            mission.log_event("AUTO_DONE",
                              f"task={task.task_id} agent={agent.name} iterations={iteration} "
                              f"shells={ctx.shell_count} latency={latency:.0f}s summary={done_summary[:200]}",
                              task_id=task.task_id, agent=agent.name)

            if mission.container_id and ctx.files_written:
                from .memory import save_working_memory
                pt_id = task.constraints.get("plan_task_id", task.task_id)
                wm_entry = (
                    f"{agent.name} completed {pt_id}: "
                    f"{done_summary[:200]} "
                    f"files={', '.join(ctx.files_written[-5:])}"
                )
                save_working_memory(mission.container_id, "decision", wm_entry, round_trip=iteration)

            break

        # Feed action results back
        pad = agent.scratchpad if isinstance(agent.scratchpad, dict) else {}
        plan_nudge = ""
        if is_readonly_iter and not pad.get("plan"):
            plan_nudge = (
                "\n💡 TIP: Use save_note with key='plan' to record your approach before writing."
            )
        elapsed_agent = time.time() - start_time
        remaining_agent = max(0, timeout - elapsed_agent)
        time_note = f"\n⏱ Time: {elapsed_agent:.0f}s elapsed, ~{remaining_agent:.0f}s remaining."
        if remaining_agent < timeout * 0.2:
            time_note += " ⚠ TIME CRITICAL — wrap up now, emit 'done' with what you have."
        elif remaining_agent < timeout * 0.4:
            time_note += " Finish up — make sure your output is complete, then emit 'done'."

        # Compress old tool results to save context
        if len(agent_messages) > 8:
            for i, msg_item in enumerate(agent_messages):
                if i >= len(agent_messages) - 4:
                    break
                if msg_item.get("role") != "user":
                    continue
                content_text = msg_item.get("content", "")
                if not content_text.startswith("Action results:"):
                    continue
                lines = content_text.split("\n")
                compressed = []
                for line in lines:
                    if line.startswith("Action results:"):
                        continue
                    for prefix in ("shell:", "write_file:", "read_file:", "patch_file:",
                                   "search:", "batch_read:", "workspace_tree:", "replace_lines:",
                                   "find_files:", "file_info:", "run_tool:", "apply_diff:", "save_note:"):
                        if line.strip().startswith(prefix):
                            compressed.append(line.strip()[:200])
                            break
                    if line.startswith("📋") or line.startswith("⏱"):
                        compressed.append(line)
                if compressed:
                    msg_item["content"] = "[Prior results summary]\n" + "\n".join(compressed)

        next_iter = iteration + 1
        iters_left = max_iterations - iteration
        if remaining_agent < timeout * 0.2:
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}."
        elif iters_left == 1:
            iter_note = (f"\n📋 Iteration {next_iter}/{max_iterations} — ⚠ THIS IS YOUR LAST ITERATION. "
                        "Deliver your final result now. "
                        'Emit {"type": "done", "summary": "..."} with your completed work.')
        elif iters_left == 2:
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}. Next iteration is your LAST — plan to wrap up."
        elif iters_left <= max(3, int(max_iterations * 0.15)):
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}. {iters_left} iterations remaining — start planning to finish."
        else:
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}."

        progress_note = ""
        pt_files = task.constraints.get("plan_task_files", [])
        if pt_files and mission.container_id and ctx.files_written:
            file_sizes = []
            for fpath in pt_files:
                fp = fpath if fpath.startswith("/") else f"/home/mission/{fpath}"
                sz_out, _, sz_rc = _container_exec(
                    mission.container_id, f"wc -c < {fp} 2>/dev/null", timeout=5)
                if sz_rc == 0 and sz_out and sz_out.strip().isdigit():
                    file_sizes.append(f"{fpath}: {sz_out.strip()} bytes")
            if file_sizes:
                progress_note = "\n📊 Output files: " + ", ".join(file_sizes)

        verify_note = _maybe_run_midtask_verify(ctx) if ctx.files_written else ""

        feedback = ("Action results:\n" + "\n".join(action_results) +
                    verify_note + plan_nudge + time_note + iter_note + progress_note +
                    "\nContinue working towards the goal.")
        agent_messages.append({"role": "user", "content": feedback})

    else:
        # Exhausted all iterations
        task.status = "done"
        task.result = (f"Reached iteration limit ({max_iterations}). "
                       f"Shell commands used: {ctx.shell_count}. "
                       f"Files written: {', '.join(ctx.files_written[-5:]) or 'none'}. "
                       f"Last action: {ctx.last_action_summary}")
        task.completed_at = time.time()
        mission.log_event("AUTO_EXHAUSTED",
                          f"task={task.task_id} agent={agent.name} iterations={max_iterations}",
                          task_id=task.task_id, agent=agent.name)

    # Auto-commit agent work to git
    if mission.container_id and ctx.files_written:
        summary_line = (done_summary or ctx.last_action_summary or "work")[:80]
        _container_exec(
            mission.container_id,
            f"cd /home/mission && git add -A && "
            f"git diff --cached --quiet || "
            f"git commit -q -m 'task-{task.task_id}: {summary_line}' --allow-empty 2>/dev/null",
            timeout=15,
        )

    # Finalize — save conversation history and update perf stats.
    # Note: agent status reset + task → history move is handled by the
    # crash-safe finally block in agent_autonomous_loop().
    with _lock:
        max_history = max(20, min(int((agent_ctx or 4096) / 1024), 80))
        agent.conversation_history = agent_messages[-max_history:]

        _aperf = mission._agent_perf.setdefault(agent.name, {
            "task_failures": 0, "task_successes": 0,
            "total_iterations": 0, "total_tasks": 0,
        })
        _aperf["total_tasks"] += 1
        _aperf["total_iterations"] += iteration
        if task.status in ("failed", "timed_out"):
            _aperf["task_failures"] += 1
        elif task.status == "cancelled":
            pass
        else:
            _aperf["task_successes"] += 1
