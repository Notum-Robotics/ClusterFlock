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
)
from .scoring import (
    _get_endpoint_ctx,
    _is_context_overflow,
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
from .parsing import (
    _parse_showrunner_response,
    _diagnose_parse_failure,
)
from .showrunner import (
    _send_prompt_to_endpoint,
    _wait_for_result,
)
from .flock import _build_agent_system_prompt


def _agent_autonomous_loop(mission, task, agent):
    """Run an autonomous agent loop: prompt → parse actions → execute → repeat."""
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

    # Per-request generation overrides from showrunner dispatch constraints
    gen_overrides = {}
    if constraints.get("max_tokens"):
        gen_overrides["max_tokens"] = constraints["max_tokens"]
    if constraints.get("generation_timeout"):
        gen_overrides["generation_timeout"] = constraints["generation_timeout"]
    if constraints.get("no_gen_limit"):
        gen_overrides["no_gen_limit"] = True

    start_time = time.time()
    shell_count = 0
    last_checkpoint_time = start_time
    consecutive_failures = 0
    files_written = []
    last_action_summary = ""

    mission.log_event("AUTO_START",
                      f"task={task.task_id} agent={agent.name} max_iter={max_iterations} timeout={timeout}s",
                      task_id=task.task_id, agent=agent.name)
    task.status = "running"

    # Load conversation history from prior dispatches
    agent_messages = []
    if agent.conversation_history:
        agent_messages = list(agent.conversation_history)
        # Strip trailing user messages — they're unreplied feedback from a
        # prior task and would create consecutive user messages once we
        # append the new task prompt (breaks strict-alternation models like gemma-3n).
        while agent_messages and agent_messages[-1].get("role") == "user":
            agent_messages.pop()

    # Add iteration budget awareness to initial prompt
    iter_intro = f"\n\n📋 Iteration 1/{max_iterations}."
    if max_iterations == 1:
        iter_intro += (" This is your ONLY iteration — deliver complete results now. "
                      "Emit {\"type\": \"done\", \"summary\": \"...\"} with your final output.")
    elif max_iterations <= 3:
        iter_intro += " Budget is tight — be efficient. Emit {\"type\": \"done\"} when finished to end early."
    else:
        iter_intro += " Emit {\"type\": \"done\", \"summary\": \"...\"} when finished to end early and save iterations."
    agent_messages.append({"role": "user", "content": task.prompt + iter_intro})

    iteration = 0
    for iteration in range(1, max_iterations + 1):
        # ── Check cancellation ──
        if task._cancel_event.is_set():
            mission.log_event("AUTO_CANCELLED",
                              f"task={task.task_id} agent={agent.name} at iteration {iteration}",
                              task_id=task.task_id, agent=agent.name)
            task.status = "cancelled"
            task.result = f"Cancelled at iteration {iteration}. Last action: {last_action_summary}"
            break

        # ── Check wall-clock timeout ──
        elapsed = time.time() - start_time
        if elapsed > timeout:
            mission.log_event("AUTO_TIMEOUT",
                              f"task={task.task_id} agent={agent.name} elapsed={elapsed:.0f}s",
                              task_id=task.task_id, agent=agent.name)
            task.status = "timed_out"
            task.error = f"Autonomous timeout after {elapsed:.0f}s, {iteration-1} iterations"
            break

        # ── Emit checkpoint ──
        now = time.time()
        if (iteration % _AUTO_CHECKPOINT_INTERVAL == 0 or
                (now - last_checkpoint_time) > _AUTO_CHECKPOINT_SECONDS):
            task.checkpoint = {
                "task_id": task.task_id,
                "agent": agent.name,
                "iteration": iteration,
                "max_iterations": max_iterations,
                "elapsed": now - start_time,
                "shell_commands": shell_count,
                "last_action": last_action_summary,
                "files_written": files_written[-5:],  # last 5
                "status": "stuck" if consecutive_failures >= 2 else "working",
            }
            last_checkpoint_time = now
            mission.log_event("AUTO_CHECKPOINT",
                              f"task={task.task_id} iter={iteration}/{max_iterations} "
                              f"elapsed={now - start_time:.0f}s shells={shell_count} "
                              f"status={'stuck' if consecutive_failures >= 2 else 'working'}",
                              task_id=task.task_id, agent=agent.name)

        # ── Bail-out on repeated failures ──
        if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            mission.log_event("AUTO_BAIL",
                f"task={task.task_id} agent={agent.name} — "
                f"{consecutive_failures} consecutive failures, bailing out",
                task_id=task.task_id, agent=agent.name)
            task.status = "failed"
            task.error = (
                f"Bailed out after {consecutive_failures} consecutive failures "
                f"at iteration {iteration}. Last: {last_action_summary}")
            task.completed_at = time.time()
            break

        # ── Prompt the agent ──
        system_prompt = _build_agent_system_prompt(agent, mission=mission)

        # Inject agent scratchpad into system prompt if it has notes
        if agent.scratchpad:
            pad_lines = [f"- {k}: {v}" for k, v in agent.scratchpad.items()]
            system_prompt += "\n\nYOUR SCRATCHPAD (persistent notes):\n" + "\n".join(pad_lines)

        # Inject available tools from mission manifest
        if mission.tools:
            tool_lines = [f"- /home/mission/tools/{t['name']}: {t.get('description', '')}"
                          for t in mission.tools]
            system_prompt += ("\n\nAVAILABLE TOOLS (use run_tool or shell to invoke):\n"
                              + "\n".join(tool_lines))

        # Inject shared mission knowledge + notes for cross-agent context
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

        # Scale rolling window to agent's context — larger context sees more history
        agent_ctx = agent.context_length or _get_endpoint_ctx(agent.node_id, agent.model)
        agent_window = max(6, min(int((agent_ctx or 4096) / 2048), 40))
        messages = [{"role": "system", "content": system_prompt}] + agent_messages[-agent_window:]

        # ── Preflight: estimate tokens & trim if likely to overflow ──
        est_tokens = sum(len(m.get("content", "")) // _CHARS_PER_TOKEN + 4 for m in messages)
        ctx_budget = int((agent_ctx or 4096) * _PREFLIGHT_HEADROOM)
        while est_tokens > ctx_budget and len(messages) > 3:
            # Remove the oldest non-system message
            messages.pop(1)
            est_tokens = sum(len(m.get("content", "")) // _CHARS_PER_TOKEN + 4 for m in messages)

        # Last resort: if remaining messages still exceed budget, truncate the longest ones
        if est_tokens > ctx_budget:
            char_budget = ctx_budget * _CHARS_PER_TOKEN
            for m in sorted(messages, key=lambda x: len(x.get("content", "")), reverse=True):
                if m.get("role") == "system":
                    continue  # don't truncate system prompt
                content = m.get("content", "")
                excess_chars = (est_tokens - ctx_budget) * _CHARS_PER_TOKEN
                if excess_chars <= 0:
                    break
                if len(content) > 2000:
                    cut = min(int(excess_chars), len(content) - 1000)
                    m["content"] = content[:500] + f"\n... [{cut} chars truncated for context fit] ...\n" + content[-500:]
                    est_tokens = sum(len(x.get("content", "")) // _CHARS_PER_TOKEN + 4 for x in messages)

        orch_task_id, wait_timeout = _send_prompt_to_endpoint(
            agent.node_id, agent.model, messages, mission.mission_id, task.task_id,
            role="worker", overrides=gen_overrides or None,
        )
        result = _wait_for_result(orch_task_id, timeout=wait_timeout)

        if not result:
            consecutive_failures += 1
            last_action_summary = "inference timeout"
            agent_messages.append({"role": "assistant", "content": '{"thinking":"timeout","actions":[]}'})
            agent_messages.append({"role": "user", "content": "Your last response timed out. Try a simpler approach."})
            continue

        # Agent-side error — check for context overflow first
        if result.get("_agent_error"):
            is_overflow, n_prompt, n_ctx_real = _is_context_overflow(result)
            if is_overflow:
                # ── Context overflow recovery: trim conversation & correct ctx ──
                if n_ctx_real and n_ctx_real < (agent.context_length or 999999):
                    mission.log_event("WARN",
                        f"agent={agent.name} context corrected: "
                        f"was={agent.context_length} actual={n_ctx_real}",
                        task_id=task.task_id, agent=agent.name)
                    agent.context_length = n_ctx_real
                    agent_ctx = n_ctx_real
                    # Also correct the registry so future dispatches use the real ctx
                    correct_endpoint_ctx(agent.node_id, agent.model, n_ctx_real)

                # Aggressively trim: keep only the initial task prompt + last assistant+user pair
                keep_first = 1  # the task prompt message
                keep_last = 2   # last assistant + user pair (if any)
                if len(agent_messages) > keep_first + keep_last:
                    agent_messages = agent_messages[:keep_first] + agent_messages[-keep_last:]
                # Recalculate window with corrected ctx
                agent_window = max(6, min(int((agent_ctx or 4096) / 2048), 40))

                # Force-truncate remaining messages if they're still too large
                effective_ctx = n_ctx_real or agent_ctx or 4096
                char_budget = int(effective_ctx * _PREFLIGHT_HEADROOM * _CHARS_PER_TOKEN)
                total_chars = sum(len(m.get("content", "")) for m in agent_messages)
                if total_chars > char_budget:
                    # Truncate the longest message (usually tool output or task prompt)
                    for m in sorted(agent_messages, key=lambda x: len(x.get("content", "")), reverse=True):
                        excess = total_chars - char_budget
                        if excess <= 0:
                            break
                        content = m.get("content", "")
                        if len(content) > 2000:
                            cut = min(excess, len(content) - 1000)
                            m["content"] = content[:500] + f"\n... [{cut} chars truncated due to context limits] ...\n" + content[-500:]
                            total_chars -= cut

                mission.log_event("CONTEXT",
                    f"agent={agent.name} overflow recovery: "
                    f"prompt={n_prompt} n_ctx={n_ctx_real or agent_ctx} — "
                    f"trimmed to {len(agent_messages)} msgs, {total_chars} chars",
                    task_id=task.task_id, agent=agent.name)
                consecutive_failures += 1
                last_action_summary = "context overflow (trimmed)"
                continue

            consecutive_failures += 1
            last_action_summary = f"agent error: {result.get('error', 'unknown')}"
            mission.log_event("AGENT_ERROR", f"agent={agent.name} error={result.get('error', '')}",
                              task_id=task.task_id, agent=agent.name)
            # Synthetic assistant message to maintain strict role alternation (gemma-3n)
            agent_messages.append({"role": "assistant", "content": '{"thinking":"error","actions":[]}'})
            agent_messages.append({"role": "user", "content": "Agent error occurred. Try again."})
            continue

        # Extract text — handle both thinking and non-thinking models
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
            consecutive_failures += 1
            last_action_summary = "empty response"
            # Synthetic assistant message to maintain strict role alternation (gemma-3n)
            agent_messages.append({"role": "assistant", "content": '{"thinking":"empty","actions":[]}'})
            agent_messages.append({"role": "user", "content": "Empty response. Try again."})
            continue

        agent_messages.append({"role": "assistant", "content": text})
        task.task_context = agent_messages[-agent_window:]  # keep rolling context available

        # ── Parse agent response ──
        parsed = _parse_showrunner_response(text)
        if not parsed or not parsed.get("actions"):
            consecutive_failures += 1
            last_action_summary = "unparseable response"
            diag = _diagnose_parse_failure(text)
            agent_messages.append({"role": "user", "content":
                f"Could not parse your response as JSON. {diag}\n"
                "Respond with a JSON object containing 'actions' array. Raw JSON only, no markdown."})
            continue

        # ── Execute actions ──
        action_results = []
        done = False
        done_summary = ""
        # Scale autonomous agent output limits to agent's context
        agent_read_limit = max(2000, int((agent_ctx or 8192) * 0.25))
        # Small-context agents get aggressively capped output
        if (agent_ctx or 8192) < 8192:
            agent_read_limit = min(agent_read_limit, 2000)

        # ── Read-only first iteration enforcement ──
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

            # Read-only enforcement: reject mutating actions on first iteration
            if is_readonly_iter and atype not in _READONLY_ACTIONS:
                if atype == "shell":
                    # Allow read-only shell commands
                    cmd_word = act.get("command", "").strip().split()[0] if act.get("command") else ""
                    if not any(cmd_word.startswith(p) for p in _READONLY_SHELL_PREFIXES):
                        action_results.append(
                            f"shell: BLOCKED — first iteration is read-only. Inspect the workspace "
                            f"first (read_file, workspace_tree, search, ls), then write in iteration 2.")
                        continue
                else:
                    action_results.append(
                        f"{atype}: BLOCKED — first iteration is read-only. "
                        f"Inspect the workspace first, then write/modify in iteration 2.")
                    continue

            # ── save_note (agent scratchpad) ──
            if atype == "save_note":
                key = act.get("key", "").strip()[:100]
                value = act.get("value", "").strip()[:2000]
                if key and value:
                    agent.scratchpad[key] = value
                    # Cap scratchpad at 20 entries
                    if len(agent.scratchpad) > 20:
                        oldest = next(iter(agent.scratchpad))
                        del agent.scratchpad[oldest]
                    action_results.append(f"save_note: saved '{key}'")
                else:
                    action_results.append("save_note: key and value required")
                consecutive_failures = 0
                last_action_summary = f"save_note: {key}"
                continue

            if atype == "search":
                pattern = act.get("pattern", "")
                spath = act.get("path", working_dir)
                if not pattern:
                    action_results.append("search: empty pattern")
                    continue
                is_regex = act.get("regex", False)
                grep_flag = "-rn" if is_regex else "-rnF"
                search_lines = max(60, agent_read_limit // 100)
                cmd = f"grep {grep_flag} --include='*' {shlex.quote(pattern)} {shlex.quote(spath)} 2>/dev/null | head -{search_lines}"
                out, err, rc = _container_exec(mission.container_id, cmd, timeout=30)
                if rc == 1 and not out:
                    action_results.append("search: no matches")
                else:
                    action_results.append(f"search: {out.count(chr(10))} matches\n{out[:agent_read_limit]}")
                consecutive_failures = 0
                last_action_summary = f"search: {pattern}"
                continue

            if atype == "find_files":
                pattern = act.get("pattern", "")
                fpath = act.get("path", working_dir)
                if not pattern:
                    action_results.append("find_files: pattern required (e.g. '*.py')")
                    continue
                found = _find_files(mission.container_id, pattern, fpath)
                action_results.append(f"find_files: {len(found)} matches\n" + "\n".join(found[:100]))
                consecutive_failures = 0
                last_action_summary = f"find_files: {pattern}"
                continue

            if atype == "file_info":
                fpath = act.get("path", "")
                if not fpath:
                    action_results.append("file_info: path required")
                    continue
                info = _file_info(mission.container_id, fpath)
                if info:
                    action_results.append(
                        f"file_info: {fpath} — {info.get('lines', '?')} lines, "
                        f"{info.get('size', 0)}B, type={info.get('type', '?')}")
                else:
                    action_results.append(f"file_info: {fpath} NOT FOUND")
                consecutive_failures = 0
                last_action_summary = f"file_info: {fpath}"
                continue

            if atype == "run_tool":
                tool_name = act.get("name", "")
                tool_args = act.get("args", [])
                if not tool_name:
                    action_results.append("run_tool: name required")
                    continue
                # Look up tool
                tool_entry = None
                for t in mission.tools:
                    if t["name"] == tool_name:
                        tool_entry = t
                        break
                if not tool_entry:
                    available = [t["name"] for t in mission.tools]
                    action_results.append(f"run_tool: '{tool_name}' not found. Available: {', '.join(available) or 'none'}")
                    continue
                tool_path = f"/home/mission/tools/{tool_name}"
                if isinstance(tool_args, list):
                    arg_str = " ".join(shlex.quote(str(a)) for a in tool_args)
                else:
                    arg_str = str(tool_args)
                tool_cmd = f"cd {shlex.quote(working_dir)} && {shlex.quote(tool_path)} {arg_str}"
                tool_timeout = min(int(act.get("timeout", 120)), _SHELL_TIMEOUT_DEFAULT)
                out, err, rc = _container_exec(mission.container_id, tool_cmd, timeout=tool_timeout)
                result_str = f"run_tool({tool_name}): rc={rc}"
                if out:
                    result_str += f" stdout={_smart_truncate(out, agent_read_limit, is_own_content=True)}"
                if err:
                    result_str += f" stderr={err[:500]}"
                action_results.append(result_str)
                if rc == 0:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                last_action_summary = f"run_tool: {tool_name} → rc={rc}"
                continue

            if atype == "shell":
                if shell_count >= max_shell:
                    action_results.append("shell: LIMIT REACHED (max shell commands exceeded)")
                    continue
                command = act.get("command", "")
                if not command:
                    action_results.append("shell: empty command")
                    continue
                # Allow agent to specify timeout (capped)
                shell_timeout = min(int(act.get("timeout", _SHELL_TIMEOUT_DEFAULT)), _SHELL_TIMEOUT_INSTALL)
                # Confine to working_dir by prepending cd
                full_cmd = f"cd {shlex.quote(working_dir)} && {command}"
                out, err, rc = _container_exec(mission.container_id, full_cmd, timeout=shell_timeout)
                shell_count += 1
                result_str = f"shell: rc={rc}"
                if out:
                    truncated_out = _smart_truncate(out, agent_read_limit, is_own_content=True)
                    result_str += f" stdout={truncated_out}"
                    if len(out) > agent_read_limit:
                        result_str += " [OUTPUT TRUNCATED — pipe through head/tail/grep to narrow results]"
                if err:
                    result_str += f" stderr={err[:agent_read_limit // 3]}"
                action_results.append(result_str)
                mission.log_event("SHELL", f"{command[:120]} → rc={rc}",
                                  task_id=task.task_id, agent=agent.name)
                last_action_summary = f"shell: {command[:80]} → rc={rc}"
                if rc != 0:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

            elif atype == "write_file":
                path = act.get("path", "")
                content = act.get("content", "")
                append = act.get("append", False)
                if not path:
                    action_results.append("write_file: no path")
                    continue
                # Enforce working_dir prefix
                if not path.startswith(working_dir) and not path.startswith("/home/mission/"):
                    path = working_dir.rstrip("/") + "/" + path.lstrip("/")
                # Ensure parent directory exists
                parent = "/".join(path.split("/")[:-1])
                if parent:
                    _container_exec(mission.container_id, f"mkdir -p {shlex.quote(parent)}", timeout=10)
                if append:
                    existing = _container_read_file(mission.container_id, path) or ""
                    full_content = existing + content
                    ok = _container_write_file(mission.container_id, path, full_content)
                    action_results.append(f"write_file(append): {path} ok={ok} (+{len(content)}B total={len(full_content)}B)")
                else:
                    ok = _container_write_file(mission.container_id, path, content)
                    result_str = f"write_file: {path} ok={ok} ({len(content)}B)"
                    # Auto syntax check
                    if ok:
                        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                        if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
                            check_ok, errors = _syntax_check(mission.container_id, path)
                            if not check_ok:
                                result_str += f" ⚠ SYNTAX ERROR: {errors}"
                    action_results.append(result_str)
                if ok:
                    mission.log_event("WRITE_FILE", f"{path} ({len(content)}B)",
                                      task_id=task.task_id, agent=agent.name)
                last_action_summary = f"write_file: {path} ({len(content)}B)"
                if ok:
                    files_written.append(path)
                    consecutive_failures = 0
                    # Auto-test: if test tool exists and this is a source file, run lint
                    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                    if ext in ("py", "js", "mjs", "ts") and len(files_written) % 3 == 0:
                        lint_out, _, lint_rc = _container_exec(
                            mission.container_id,
                            f"test -x /home/mission/tools/lint && /home/mission/tools/lint {shlex.quote(path)} 2>&1 || true",
                            timeout=15)
                        if lint_rc != 0 and lint_out:
                            action_results.append(f"auto-lint: ⚠ {lint_out[:500]}")
                else:
                    consecutive_failures += 1

            elif atype == "read_file":
                path = act.get("path", "")
                if not path:
                    action_results.append("read_file: no path")
                    continue
                start_line = act.get("start_line")
                end_line = act.get("end_line")
                if start_line is not None and end_line is not None:
                    # Line-range read
                    start_line = max(1, int(start_line))
                    end_line = max(start_line, int(end_line))
                    cmd = f"sed -n '{start_line},{end_line}p' {shlex.quote(path)}"
                    fcontent, err, rc = _container_exec(mission.container_id, cmd, timeout=30)
                    if rc != 0 or fcontent is None:
                        action_results.append(f"read_file: {path} NOT FOUND")
                        consecutive_failures += 1
                    else:
                        display = _smart_truncate(fcontent, agent_read_limit, is_own_content=True)
                        action_results.append(
                            f"read_file: {path} lines {start_line}-{end_line} ({len(fcontent)}B)\n{display}")
                        consecutive_failures = 0
                else:
                    fcontent = _container_read_file(mission.container_id, path)
                    if fcontent is not None:
                        total_lines = fcontent.count('\n') + (1 if fcontent and not fcontent.endswith('\n') else 0)
                        truncated = len(fcontent) > agent_read_limit
                        if truncated:
                            # For large files: show structured preview instead of raw truncation
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
                        action_results.append(
                            f"read_file: {path} ({len(fcontent)}B, {total_lines} lines)\n{display}")
                        consecutive_failures = 0
                    else:
                        action_results.append(f"read_file: {path} NOT FOUND")
                        consecutive_failures += 1
                last_action_summary = f"read_file: {path}"

            elif atype == "batch_read":
                paths = act.get("paths", [])
                if not paths or not isinstance(paths, list):
                    action_results.append("batch_read: paths must be a non-empty array")
                    continue
                per_file_limit = agent_read_limit // max(len(paths), 1)
                per_file_limit = max(per_file_limit, 1500)
                batch_parts = []
                for p in paths[:15]:  # cap
                    fcontent = _container_read_file(mission.container_id, p)
                    if fcontent is None:
                        batch_parts.append(f"--- {p}: NOT FOUND ---")
                    else:
                        display = _smart_truncate(fcontent, per_file_limit, is_own_content=True)
                        batch_parts.append(f"--- {p} ({len(fcontent)}B) ---\n{display}")
                action_results.append(f"batch_read: {len(paths)} files\n" + "\n".join(batch_parts))
                consecutive_failures = 0
                last_action_summary = f"batch_read: {len(paths)} files"

            elif atype == "workspace_tree":
                tree_path = act.get("path", "/home/mission/")
                tree = _build_workspace_tree(mission.container_id, tree_path)
                if tree:
                    action_results.append(f"workspace_tree: {tree_path}\n{tree}")
                    consecutive_failures = 0
                else:
                    action_results.append("workspace_tree: empty or failed")
                last_action_summary = f"workspace_tree: {tree_path}"

            elif atype == "patch_file":
                path = act.get("path", "")
                old_text = act.get("old", "")
                new_text = act.get("new", "")
                if not path or not old_text:
                    action_results.append("patch_file: path and old text required")
                    continue
                fcontent = _container_read_file(mission.container_id, path)
                if fcontent is None:
                    action_results.append(f"patch_file: {path} NOT FOUND")
                    consecutive_failures += 1
                    continue
                cnt = fcontent.count(old_text)
                if cnt == 0:
                    action_results.append("patch_file: old text not found — read_file first to see exact content")
                    consecutive_failures += 1
                elif cnt > 1:
                    action_results.append(f"patch_file: old text matches {cnt} locations — include more context lines to be specific")
                    consecutive_failures += 1
                else:
                    new_content = fcontent.replace(old_text, new_text, 1)
                    ok = _container_write_file(mission.container_id, path, new_content)
                    result_str = f"patch_file: {path} ok={ok} (-{len(old_text)}B +{len(new_text)}B)"
                    # Auto syntax check
                    if ok:
                        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                        if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
                            check_ok, errors = _syntax_check(mission.container_id, path)
                            if not check_ok:
                                result_str += f" ⚠ SYNTAX ERROR: {errors}"
                    action_results.append(result_str)
                    if ok:
                        mission.log_event("PATCH_FILE", f"{path} (-{len(old_text)}B +{len(new_text)}B)",
                                          task_id=task.task_id, agent=agent.name)
                        files_written.append(path)
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                last_action_summary = f"patch_file: {path}"

            elif atype == "replace_lines":
                path = act.get("path", "")
                sl = act.get("start_line")
                el = act.get("end_line")
                new_text = act.get("content", "")
                if not path or sl is None or el is None:
                    action_results.append("replace_lines: path, start_line, end_line, and content required")
                    continue
                sl = max(1, int(sl))
                el = max(sl, int(el))
                ok, total = _replace_lines(mission.container_id, path, sl, el, new_text)
                if not ok:
                    action_results.append(f"replace_lines: {path} FAILED (file not found or write error)")
                    consecutive_failures += 1
                else:
                    new_line_count = len(new_text.split("\n")) if new_text else 0
                    result_str = f"replace_lines: {path} lines {sl}-{el} replaced with {new_line_count} lines (total: {total})"
                    # Auto syntax check
                    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                    if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
                        check_ok, errors = _syntax_check(mission.container_id, path)
                        if not check_ok:
                            result_str += f" ⚠ SYNTAX ERROR: {errors}"
                    action_results.append(result_str)
                    mission.log_event("REPLACE_LINES", f"{path} lines {sl}-{el}",
                                      task_id=task.task_id, agent=agent.name)
                    files_written.append(path)
                    consecutive_failures = 0
                last_action_summary = f"replace_lines: {path} {sl}-{el}"

            elif atype == "apply_diff":
                diff_text = act.get("diff", "")
                diff_path = act.get("path", "")
                if not diff_text:
                    action_results.append("apply_diff: diff content required")
                    continue
                ok, output = _apply_diff(mission.container_id, diff_path, diff_text)
                result_str = f"apply_diff: ok={ok} {output}"
                if ok and diff_path:
                    ext = diff_path.rsplit(".", 1)[-1].lower() if "." in diff_path else ""
                    if ext in ("py", "js", "mjs", "ts", "json", "sh", "bash"):
                        check_ok, errors = _syntax_check(mission.container_id, diff_path)
                        if not check_ok:
                            result_str += f" ⚠ SYNTAX ERROR: {errors}"
                action_results.append(result_str)
                if ok:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                last_action_summary = f"apply_diff: {diff_path or 'multi'}"

        if done:
            # Quality gate: reject placeholder/trivial task results (unless last iteration)
            _PLACEHOLDER_MARKERS = ("placeholder", "todo", "lorem ipsum", "sample output",
                                    "will be", "to be completed", "tbd", "example")
            is_placeholder = (
                len(done_summary.strip()) < 50
                or any(m in done_summary.lower() for m in _PLACEHOLDER_MARKERS)
            )
            if is_placeholder and iteration < max_iterations:
                mission.log_event("QUALITY_REJECT",
                    f"task={task.task_id} agent={agent.name} — rejected placeholder result "
                    f"({len(done_summary)} chars) at iter {iteration}",
                    task_id=task.task_id, agent=agent.name)
                agent_messages.append({"role": "user", "content":
                    "❌ REJECTED: Your 'done' summary is too short or appears to be a placeholder. "
                    "A task result must contain substantive completed work (>50 chars, no placeholders). "
                    "Continue working and deliver real results."})
                done = False
                consecutive_failures += 1
                continue

            task.status = "done"
            task.result = done_summary
            task.completed_at = time.time()
            latency = task.completed_at - task.created_at
            mission.log_event("AUTO_DONE",
                              f"task={task.task_id} agent={agent.name} iterations={iteration} "
                              f"shells={shell_count} latency={latency:.0f}s summary={done_summary[:200]}",
                              task_id=task.task_id, agent=agent.name)
            break

        # Feed action results back to agent — with compressed summaries for prior results
        # Planning enforcement: after first read-only iteration, nudge for plan if missing
        plan_nudge = ""
        if is_readonly_iter and not agent.scratchpad.get("plan"):
            plan_nudge = (
                "\n💡 TIP: Use save_note with key='plan' to record your approach before writing. "
                "Planning first prevents wasted iterations."
            )
        elapsed_agent = time.time() - start_time
        remaining_agent = max(0, timeout - elapsed_agent)
        time_note = f"\n⏱ Time: {elapsed_agent:.0f}s elapsed, ~{remaining_agent:.0f}s remaining."
        if remaining_agent < timeout * 0.2:
            time_note += " ⚠ TIME CRITICAL — wrap up now, emit 'done' with what you have."
        elif remaining_agent < timeout * 0.4:
            time_note += " Finish up — make sure your output is complete, then emit 'done'."

        # Compress old tool results in conversation to save context
        # Keep only outcome summaries for messages older than the last 4
        if len(agent_messages) > 8:
            for i, msg in enumerate(agent_messages):
                if i >= len(agent_messages) - 4:
                    break  # keep recent messages intact
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                if not content.startswith("Action results:"):
                    continue
                # Compress: extract just the action type and outcome
                lines = content.split("\n")
                compressed = []
                for line in lines:
                    if line.startswith("Action results:"):
                        continue
                    # Keep action summary lines, drop content bodies
                    for prefix in ("shell:", "write_file:", "read_file:", "patch_file:",
                                   "search:", "batch_read:", "workspace_tree:", "replace_lines:",
                                   "find_files:", "file_info:", "run_tool:", "apply_diff:", "save_note:"):
                        if line.strip().startswith(prefix):
                            # Keep only the first line of each result
                            compressed.append(line.strip()[:200])
                            break
                    if line.startswith("📋") or line.startswith("⏱"):
                        compressed.append(line)
                if compressed:
                    msg["content"] = "[Prior results summary]\n" + "\n".join(compressed)

        # Iteration budget awareness for next round
        next_iter = iteration + 1
        iters_left = max_iterations - iteration  # iterations remaining after this one
        time_is_critical = remaining_agent < timeout * 0.2
        if time_is_critical:
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}."
        elif iters_left == 1:
            iter_note = (f"\n📋 Iteration {next_iter}/{max_iterations} — ⚠ THIS IS YOUR LAST ITERATION. "
                        "Deliver your final result now. "
                        'Emit {"type": "done", "summary": "..."} with your completed work. '
                        "If the task is incomplete, summarize progress and what remains.")
        elif iters_left == 2:
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}. Next iteration is your LAST — plan to wrap up."
        elif iters_left <= max(3, int(max_iterations * 0.15)):
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}. {iters_left} iterations remaining — start planning to finish."
        else:
            iter_note = f"\n📋 Iteration {next_iter}/{max_iterations}."

        feedback = "Action results:\n" + "\n".join(action_results) + plan_nudge + time_note + iter_note + "\nContinue working towards the goal."
        agent_messages.append({"role": "user", "content": feedback})

    else:
        # Exhausted all iterations without 'done'
        task.status = "done"
        task.result = (f"Reached iteration limit ({max_iterations}). "
                       f"Shell commands used: {shell_count}. "
                       f"Files written: {', '.join(files_written[-5:]) or 'none'}. "
                       f"Last action: {last_action_summary}")
        task.completed_at = time.time()
        mission.log_event("AUTO_EXHAUSTED",
                          f"task={task.task_id} agent={agent.name} iterations={max_iterations}",
                          task_id=task.task_id, agent=agent.name)

    # ── Auto-commit agent work to git ──
    if mission.container_id and files_written:
        summary_line = (done_summary or last_action_summary or "work")[:80]
        _container_exec(
            mission.container_id,
            f"cd /home/mission && git add -A && "
            f"git diff --cached --quiet || "
            f"git commit -q -m 'task-{task.task_id}: {summary_line}' --allow-empty 2>/dev/null",
            timeout=15,
        )

    # ── Finalize ──
    with _lock:
        # Save conversation history for future dispatches
        agent_ctx = agent.context_length or _get_endpoint_ctx(agent.node_id, agent.model)
        max_history = max(20, min(int((agent_ctx or 4096) / 1024), 80))
        agent.conversation_history = agent_messages[-max_history:]

        agent.assigned_task = None
        agent.status = "available"

        # Task results are reported to Showrunner via task_history/prompt_parts
        mission.task_history.append(task.to_dict())
        mission.tasks.pop(task.task_id, None)
