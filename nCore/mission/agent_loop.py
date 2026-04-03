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
    allowed_caps = set(task.capabilities or ["shell", "write_file", "read_file", "batch_read", "workspace_tree"])

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

        # ── Prompt the agent ──
        system_prompt = _build_agent_system_prompt(agent)
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

        for act in parsed.get("actions", []):
            atype = act.get("type", "")

            if atype == "done":
                done = True
                done_summary = act.get("summary", "Task completed.")
                break

            if atype not in allowed_caps:
                action_results.append(f"{atype}: NOT ALLOWED (capabilities: {', '.join(allowed_caps)})")
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
                if not path:
                    action_results.append("write_file: no path")
                    continue
                # Enforce working_dir prefix
                if not path.startswith(working_dir) and not path.startswith("/home/mission/"):
                    path = working_dir.rstrip("/") + "/" + path.lstrip("/")
                ok = _container_write_file(mission.container_id, path, content)
                action_results.append(f"write_file: {path} ok={ok} ({len(content)}B)")
                if ok:
                    mission.log_event("WRITE_FILE", f"{path} ({len(content)}B)",
                                      task_id=task.task_id, agent=agent.name)
                last_action_summary = f"write_file: {path} ({len(content)}B)"
                if ok:
                    files_written.append(path)
                    consecutive_failures = 0
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
                        display = _smart_truncate(fcontent, agent_read_limit, is_own_content=True)
                        truncated = len(fcontent) > agent_read_limit
                        hint = " [TRUNCATED — use start_line/end_line]" if truncated else ""
                        action_results.append(
                            f"read_file: {path} ({len(fcontent)}B, {total_lines} lines){hint}\n{display}")
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
                    action_results.append("patch_file: old text not found — read_file first")
                    consecutive_failures += 1
                elif cnt > 1:
                    action_results.append(f"patch_file: old text matches {cnt} locations — be more specific")
                    consecutive_failures += 1
                else:
                    new_content = fcontent.replace(old_text, new_text, 1)
                    ok = _container_write_file(mission.container_id, path, new_content)
                    action_results.append(f"patch_file: {path} ok={ok} (-{len(old_text)}B +{len(new_text)}B)")
                    if ok:
                        mission.log_event("PATCH_FILE", f"{path} (-{len(old_text)}B +{len(new_text)}B)",
                                          task_id=task.task_id, agent=agent.name)
                        files_written.append(path)
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                last_action_summary = f"patch_file: {path}"

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

        # Feed action results back to agent
        elapsed_agent = time.time() - start_time
        remaining_agent = max(0, timeout - elapsed_agent)
        time_note = f"\n⏱ Time: {elapsed_agent:.0f}s elapsed, ~{remaining_agent:.0f}s remaining."
        if remaining_agent < timeout * 0.2:
            time_note += " ⚠ TIME CRITICAL — wrap up now, emit 'done' with what you have."
        elif remaining_agent < timeout * 0.4:
            time_note += " Finish up — make sure your output is complete, then emit 'done'."

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

        feedback = "Action results:\n" + "\n".join(action_results) + time_note + iter_note + "\nContinue working towards the goal."
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
