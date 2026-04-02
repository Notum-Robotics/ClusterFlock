"""ClusterFlock OAPI — OpenAI-compatible chat completions API.

UNRELATED TO MISSION RUNNER

Runs on a separate port (default 1919). Fan-outs user prompts to all
available cluster endpoints, uses a Showrunner to synthesize a final
answer, and returns an OpenAI-compatible response.

Endpoints:
  GET  /v1/models                   List available models
  POST /v1/chat/completions         Chat completions (non-streaming)
  GET  /api/oapi/status             Queue & showrunner status
  GET  /api/oapi/conversations      List conversations
  GET  /api/oapi/conversations/:id  Get conversation detail
  DELETE /api/oapi/conversations     Clear all conversations
  DELETE /api/oapi/conversations/:id Delete one conversation
  GET  /                            Web UI
"""

import json
import math
import re
import secrets
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from registry import all_nodes, get_node
import orchestrator as orch_mod
import ranking

# ── Configuration ────────────────────────────────────────────────────────

_DEFAULT_PORT = 1919
_MAX_QUEUE = 5           # max queued requests


def _strip_think_tags(text):
    """Remove <think>...</think> blocks from content (thinking model output cleanup)."""
    if not text:
        return text
    cleaned = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()
    return cleaned if cleaned else text  # fallback to original if stripping removes everything
_FANOUT_TIMEOUT = 60     # seconds to wait for all endpoints
_SYNTHESIS_TIMEOUT = 120 # seconds to wait for showrunner synthesis
_MAX_CONVERSATIONS = 50  # max stored conversations
_MAX_HISTORY_TURNS = 50  # max turns per conversation
_THINKING_POWER_MIN = 10
_THINKING_POWER_MAX = 300
_REUSE_MIN_GAP = 5       # don't re-dispatch if < 5 s left

# Sampling parameters forwarded to endpoints when provided by the caller
_FORWARDED_PARAMS = ("temperature", "top_p", "frequency_penalty", "presence_penalty", "stop")

# Mode options: "fanout" (default), "speed", "manual"
_VALID_MODES = ("fanout", "speed", "manual")

# Showrunner system prompt for OAPI synthesis
_SYNTH_SYSTEM = (
    "You are the Showrunner — a master synthesizer. You receive a user's question "
    "along with answers from multiple AI endpoints in a compute cluster. "
    "Your job is to evaluate all responses, identify the best information, "
    "correct any errors, and produce a single authoritative answer.\n\n"
    "RULES:\n"
    "- Respond ONLY with the final answer for the user. Do NOT mention endpoints, "
    "cluster internals, or that multiple models were consulted.\n"
    "- Do NOT include your reasoning process, evaluation notes, or analysis. "
    "Output ONLY the direct answer the user needs.\n"
    "- If all endpoints agree, be concise. If they disagree, reason carefully "
    "but only output the conclusion.\n"
    "- Preserve any code blocks, formatting, or structure from the best response.\n"
    "- If the user asked for a specific format (JSON, list, etc.), follow it.\n"
    "- Be helpful, accurate, and direct.\n"
)

# ── Thread safety ────────────────────────────────────────────────────────

_lock = threading.Lock()

# ── Request queue ────────────────────────────────────────────────────────

_queue_sem = threading.Semaphore(_MAX_QUEUE)
_active_request = threading.Lock()  # only one request processed at a time
_queue_depth = 0  # track current queue depth

# ── Conversations ────────────────────────────────────────────────────────

# conv_id → {id, created, updated, turns: [{role, content, _meta}]}
_conversations: dict = {}
_conv_order: list = []  # ordered list of conv_ids (newest first)

# ── OAPI mode ────────────────────────────────────────────────────────────

_oapi_mode = "fanout"       # "fanout" | "speed" | "manual"
_oapi_manual_model = None   # model name string when mode == "manual"
_oapi_thinking_power = 60   # seconds — fan-out collection window (10–300)
_oapi_max_tokens = 0        # 0 = no limit; >0 forwarded to all endpoints

# ── Showrunner state ─────────────────────────────────────────────────────

_showrunner = {
    "node_id": None,
    "model": None,
    "score": 0,
    "hostname": None,
    "context_length": 0,
    "elected_at": 0,
}

# ── Status tracking ─────────────────────────────────────────────────────

_status = {
    "requests_total": 0,
    "requests_completed": 0,
    "requests_failed": 0,
    "last_request_at": 0,
    "queue_depth": 0,
}


# ── OAPI mode getters/setters ────────────────────────────────────────────

def get_oapi_mode():
    with _lock:
        return _oapi_mode


def set_oapi_mode(mode, manual_model=None):
    global _oapi_mode, _oapi_manual_model
    if mode not in _VALID_MODES:
        return False
    with _lock:
        _oapi_mode = mode
        if mode == "manual":
            _oapi_manual_model = manual_model
        return True


def get_oapi_config():
    with _lock:
        return {
            "mode": _oapi_mode,
            "manual_model": _oapi_manual_model,
            "thinking_power": _oapi_thinking_power,
            "max_tokens": _oapi_max_tokens,
        }


def load_oapi_config(cfg):
    """Restore config from persisted state."""
    global _oapi_mode, _oapi_manual_model, _oapi_thinking_power, _oapi_max_tokens
    if not cfg:
        return
    with _lock:
        m = cfg.get("mode", "fanout")
        if m in _VALID_MODES:
            _oapi_mode = m
        _oapi_manual_model = cfg.get("manual_model")
        tp = cfg.get("thinking_power")
        if tp is not None:
            _oapi_thinking_power = max(_THINKING_POWER_MIN, min(int(tp), _THINKING_POWER_MAX))
        mt = cfg.get("max_tokens")
        if mt is not None:
            _oapi_max_tokens = max(0, int(mt))


# Delegate scoring to ranking module
_model_quality_tier = ranking.model_quality_tier
_composite_score = ranking.composite_score


# ── Showrunner election ─────────────────────────────────────────────────

def _elect_showrunner(exclude_node_id=None):
    """Pick the best model as Showrunner. Delegates to ranking module."""
    return ranking.elect_showrunner(exclude_node_id=exclude_node_id)


def _ensure_showrunner():
    """Ensure we have a valid showrunner. Re-elect if needed."""
    global _showrunner
    with _lock:
        # Check if current showrunner is still healthy
        if _showrunner["node_id"]:
            node = get_node(_showrunner["node_id"])
            if node and node.get("status") != "dead":
                # Verify model still loaded
                for ep in node.get("endpoints", []):
                    if ep.get("model") == _showrunner["model"] and ep.get("status") == "ready":
                        return dict(_showrunner)

        # Need to elect a new one
        sr = _elect_showrunner()
        if sr:
            _showrunner.update(sr)
            _showrunner["elected_at"] = time.time()
            _log(f"showrunner  {sr['model']} on {sr['hostname']} (score={sr['score']:.1f})")
            return dict(_showrunner)
        return None


def _failover_showrunner(failed_node_id, knowledge):
    """Showrunner failed — elect a new one, pass on accumulated knowledge.
    Returns new showrunner dict or None."""
    global _showrunner
    _log(f"failover    showrunner on {failed_node_id} failed, re-electing...")

    sr = _elect_showrunner(exclude_node_id=failed_node_id)
    if not sr:
        _log("failover    no eligible showrunner available")
        return None

    with _lock:
        _showrunner.update(sr)
        _showrunner["elected_at"] = time.time()

    _log(f"failover    new showrunner: {sr['model']} on {sr['hostname']}")
    return sr


# ── Prompt dispatch helpers ──────────────────────────────────────────────

def _send_to_endpoint(node_id, model, messages, task_prefix="oapi", max_tokens=0, sampling_params=None):
    """Send a prompt to a single endpoint. Returns orchestrator task_id.
    max_tokens: 0 = no limit (agent default), >0 = forwarded to agent.
    sampling_params: dict of temperature/top_p/etc forwarded to agent."""
    orch_task_id = f"{task_prefix}-" + secrets.token_hex(6)

    cmd = {
        "action": "prompt",
        "task_id": orch_task_id,
        "model": model,
        "messages": messages,
        "ttl": max(_oapi_thinking_power, _FANOUT_TIMEOUT) * 2,
        "locked": True,
        "tight_pack": orch_mod.is_tight_pack(),
    }
    if max_tokens and max_tokens > 0:
        cmd["max_tokens"] = max_tokens
    if sampling_params:
        for k, v in sampling_params.items():
            if k in _FORWARDED_PARAMS:
                cmd[k] = v

    orch_mod.enqueue(node_id, cmd)

    with orch_mod._lock:
        orch_mod._tasks[orch_task_id] = {
            "status": "pending",
            "expected": 1,
            "results": [],
            "created": time.time(),
        }

    return orch_task_id


def _wait_for_result(orch_task_id, timeout=120):
    """Poll orchestrator for task result. Returns result dict or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = orch_mod.get_task(orch_task_id)
        if task and task["status"] == "done" and task["results"]:
            return task["results"][0]
        time.sleep(0.5)
    return None


def _collect_ready_endpoints():
    """Gather all ready endpoints across the cluster."""
    return ranking.collect_ready_endpoints()


def _truncate_messages_for_context(messages, max_context):
    """Truncate conversation history to fit within an endpoint's context window.
    Always preserves the system prompt (first message) and last user message.
    Estimates ~4 chars per token."""
    if not messages:
        return messages

    chars_per_token = 4
    budget = int(max_context * chars_per_token * 0.6)  # 60% of context for input

    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars <= budget:
        return messages

    # Always keep first (system) and last (user) message
    keep = []
    if messages[0].get("role") == "system":
        keep.append(messages[0])
        remaining = messages[1:]
    else:
        remaining = list(messages)

    # Always keep last message
    last = remaining[-1] if remaining else None
    middle = remaining[:-1] if remaining else []

    used = sum(len(m.get("content", "")) for m in keep)
    if last:
        used += len(last.get("content", ""))

    # Add middle messages from most recent backwards
    kept_middle = []
    for m in reversed(middle):
        mc = len(m.get("content", ""))
        if used + mc > budget:
            break
        kept_middle.insert(0, m)
        used += mc

    result = keep + kept_middle
    if last:
        result.append(last)
    return result


# ── Core OAPI logic ─────────────────────────────────────────────────────

def _process_chat_completion(messages, conv_id=None, max_tokens=0, sampling_params=None):
    """Process a chat completion request.  Dispatches based on OAPI mode.
    Returns (response_dict, error_string)."""
    mode = get_oapi_mode()

    # Merge per-request max_tokens with global setting (per-request wins if nonzero)
    effective_max = max_tokens if max_tokens and max_tokens > 0 else _oapi_max_tokens

    if mode == "speed":
        return _process_speed(messages, max_tokens=effective_max, sampling_params=sampling_params)
    if mode == "manual":
        return _process_manual(messages, max_tokens=effective_max, sampling_params=sampling_params)
    return _process_fanout(messages, max_tokens=effective_max, sampling_params=sampling_params)


def _estimate_prompt_tokens(messages):
    """Rough token count for context-fit check (~4 chars/token)."""
    return sum(len(m.get("content", "")) for m in messages) // 4


def _build_response(content, model_label, meta_extra=None):
    """Build an OpenAI-compatible response dict."""
    response = {
        "id": "chatcmpl-" + secrets.token_hex(6),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_label,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(content) // 4,
            "total_tokens": len(content) // 4,
        },
        "_clusterflock": meta_extra or {},
    }
    return response


# ── Speed mode ───────────────────────────────────────────────────────────

def _process_speed(messages, max_tokens=0, sampling_params=None):
    """Single fastest endpoint that fits the prompt context."""
    endpoints = _collect_ready_endpoints()
    if not endpoints:
        return None, "no ready endpoints in cluster"

    required_ctx = _estimate_prompt_tokens(messages)
    ep = ranking.select_fastest(endpoints, required_context=required_ctx)
    if not ep:
        return None, "no endpoint available"

    event_log = []
    _ev = lambda msg: event_log.append({"t": time.time(), "msg": msg})
    _ev(f"mode: speed — selected {ep['model']} on {ep['hostname']} "
        f"({ep['toks_per_sec']:.0f} tok/s, ctx {ep['context_length']})")

    ep_messages = _truncate_messages_for_context(messages, ep["context_length"])
    task_id = _send_to_endpoint(ep["node_id"], ep["model"], ep_messages, "oapi-spd", max_tokens=max_tokens, sampling_params=sampling_params)
    _ev(f"dispatched to {ep['model']} on {ep['hostname']}")

    result = _wait_for_result(task_id, timeout=_FANOUT_TIMEOUT)
    if not result:
        return None, f"endpoint {ep['model']} timed out"

    content = ""
    choices = result.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
    if not content:
        content = result.get("content", "")
    if not content:
        return None, "endpoint returned empty response"

    _ev(f"response from {ep['model']} on {ep['hostname']} ({len(content)} chars)")

    content = _strip_think_tags(content)
    return _build_response(content, ep["model"], {
        "mode": "speed",
        "endpoints_queried": 1,
        "endpoints_responded": 1,
        "showrunner": ep["model"],
        "showrunner_host": ep["hostname"],
        "event_log": event_log,
    }), None


# ── Manual mode ──────────────────────────────────────────────────────────

def _process_manual(messages, max_tokens=0, sampling_params=None):
    """Send to a specific user-selected model."""
    with _lock:
        target_model = _oapi_manual_model

    if not target_model:
        return None, "manual mode: no model selected — choose one in OAPI settings"

    endpoints = _collect_ready_endpoints()
    ep = ranking.select_manual(endpoints, target_model)
    if not ep:
        return None, f"manual mode: model '{target_model}' not loaded or not ready"

    event_log = []
    _ev = lambda msg: event_log.append({"t": time.time(), "msg": msg})
    _ev(f"mode: manual — {ep['model']} on {ep['hostname']}")

    ep_messages = _truncate_messages_for_context(messages, ep["context_length"])
    task_id = _send_to_endpoint(ep["node_id"], ep["model"], ep_messages, "oapi-man", max_tokens=max_tokens, sampling_params=sampling_params)
    _ev(f"dispatched to {ep['model']} on {ep['hostname']}")

    result = _wait_for_result(task_id, timeout=_FANOUT_TIMEOUT)
    if not result:
        return None, f"endpoint {ep['model']} timed out"

    content = ""
    choices = result.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
    if not content:
        content = result.get("content", "")
    if not content:
        return None, "endpoint returned empty response"

    _ev(f"response from {ep['model']} on {ep['hostname']} ({len(content)} chars)")

    content = _strip_think_tags(content)
    return _build_response(content, ep["model"], {
        "mode": "manual",
        "endpoints_queried": 1,
        "endpoints_responded": 1,
        "showrunner": ep["model"],
        "showrunner_host": ep["hostname"],
        "event_log": event_log,
    }), None


# ── Fan-out mode (original behaviour) ────────────────────────────────────

def _process_fanout(messages, max_tokens=0, sampling_params=None):
    """Fan-out → collect → re-use → synthesize.  Returns (response_dict, error_string).

    Thinking Power controls the collection window.  When all endpoints respond
    before the deadline, we re-dispatch to gather additional perspectives until
    time runs out (endpoint re-use).
    """

    sr = _ensure_showrunner()
    if not sr:
        return None, "no showrunner available — no models with 7B+ parameters loaded"

    endpoints = _collect_ready_endpoints()
    if not endpoints:
        return None, "no ready endpoints in cluster"

    event_log = []
    _log_event = lambda msg: event_log.append({"t": time.time(), "msg": msg})

    thinking_power = _oapi_thinking_power  # snapshot
    _log_event(f"showrunner: {sr['model']} on {sr['hostname']}")
    _log_event(f"endpoints: {len(endpoints)} ready, thinking_power: {thinking_power}s")

    collected = []       # [{endpoint, content, received_at, round}]
    sr_thoughts = []     # showrunner intermediate thoughts
    deadline = time.time() + thinking_power
    reuse_round = 0
    total_dispatched = 0

    # ── Collection loop (with endpoint re-use) ───────────────────────
    while True:
        reuse_round += 1

        # Dispatch to ALL endpoints
        fanout_tasks = {}  # orch_task_id → endpoint_info
        for ep in endpoints:
            ep_messages = _truncate_messages_for_context(messages, ep["context_length"])
            task_id = _send_to_endpoint(
                ep["node_id"], ep["model"], ep_messages, "oapi", max_tokens=max_tokens,
                sampling_params=sampling_params,
            )
            fanout_tasks[task_id] = ep
            total_dispatched += 1
            if reuse_round == 1:
                _log_event(f"dispatched to {ep['model']} on {ep['hostname']}")
            else:
                _log_event(f"re-use round {reuse_round}: dispatched to {ep['model']} on {ep['hostname']}")

        # Collect this round's responses
        done_tasks = set()
        while time.time() < deadline and len(done_tasks) < len(fanout_tasks):
            for task_id, ep_info in fanout_tasks.items():
                if task_id in done_tasks:
                    continue
                task = orch_mod.get_task(task_id)
                if not task or task["status"] not in ("done",):
                    continue

                done_tasks.add(task_id)

                if task["results"]:
                    result = task["results"][0]
                    content = ""
                    choices = result.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        content = msg.get("content", "")
                    if not content:
                        content = result.get("content", "")
                    if content:
                        collected.append({
                            "endpoint": ep_info,
                            "content": content,
                            "received_at": time.time(),
                            "round": reuse_round,
                        })
                        _log_event(f"response from {ep_info['model']} on {ep_info['hostname']} ({len(content)} chars)")

                        # Feed to showrunner for intermediate evaluation
                        if (ep_info["node_id"] != sr["node_id"] or ep_info["model"] != sr["model"]):
                            sr_eval = _showrunner_evaluate(sr, content, ep_info, messages)
                            if sr_eval:
                                sr_thoughts.append(sr_eval)
                                _log_event(f"showrunner evaluated response from {ep_info['model']}")

            time.sleep(0.5)

        # All tasks for this round are done. Re-use endpoints if time remains.
        remaining = deadline - time.time()
        if remaining < _REUSE_MIN_GAP:
            break  # not enough time for another round

        _log_event(f"round {reuse_round} complete — {remaining:.0f}s remaining, re-using endpoints")

    if not collected:
        return None, "no endpoint responded within timeout"

    _log_event(f"collected {len(collected)} responses from {total_dispatched} dispatches over {reuse_round} round(s)")

    # Phase 3: Final synthesis (max_tokens also applied to showrunner)
    final_content, synth_err = _showrunner_synthesize(
        sr, messages, collected, sr_thoughts, event_log
    )

    if synth_err:
        # Showrunner failed — try failover
        knowledge = {
            "collected": collected,
            "sr_thoughts": sr_thoughts,
            "event_log": event_log,
        }
        new_sr = _failover_showrunner(sr["node_id"], knowledge)
        if new_sr:
            _log_event(f"failover to {new_sr['model']} on {new_sr['hostname']}")
            final_content, synth_err = _showrunner_synthesize(
                new_sr, messages, collected, sr_thoughts, event_log
            )

    if synth_err:
        # Last resort: return the best single response
        best = max(collected, key=lambda c: _composite_score(
            c["endpoint"]["toks_per_sec"], c["endpoint"]["model"], c["endpoint"]["context_length"]
        ))
        final_content = best["content"]
        _log_event("fallback: returning best single-endpoint response")

    return _build_response(final_content, "clusterflock", {
        "mode": "fanout",
        "endpoints_queried": total_dispatched,
        "endpoints_responded": len(collected),
        "reuse_rounds": reuse_round,
        "thinking_power": thinking_power,
        "showrunner": sr["model"],
        "showrunner_host": sr["hostname"],
        "event_log": event_log,
    }), None


def _showrunner_evaluate(sr, response_content, ep_info, original_messages):
    """Ask showrunner to evaluate an endpoint's response. Returns thought string or None."""
    user_msg = original_messages[-1].get("content", "") if original_messages else ""

    eval_messages = [
        {"role": "system", "content": (
            "You are evaluating an AI endpoint's response. Be brief. "
            "Note any errors, strengths, or missing information. 1-2 sentences max."
        )},
        {"role": "user", "content": (
            f"User asked: {user_msg[:500]}\n\n"
            f"Endpoint ({ep_info['model']}) responded:\n{response_content[:2000]}\n\n"
            "Your quick evaluation:"
        )},
    ]

    task_id = _send_to_endpoint(sr["node_id"], sr["model"], eval_messages, "oapi-eval")
    result = _wait_for_result(task_id, timeout=30)
    if result:
        choices = result.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content", "")
            if content:
                return content
        return result.get("content", "")
    return None


def _showrunner_synthesize(sr, original_messages, collected, sr_thoughts, event_log):
    """Ask showrunner to produce final synthesized answer.
    Returns (content, error_string)."""

    # Build the synthesis prompt
    responses_text = ""
    for i, c in enumerate(collected, 1):
        ep = c["endpoint"]
        responses_text += f"\n--- Endpoint {i}: {ep['model']} ({ep['hostname']}) ---\n"
        # Strip thinking and truncate very long responses to fit in context
        content = _strip_think_tags(c["content"])
        if len(content) > 4000:
            content = content[:4000] + "\n[...truncated...]"
        responses_text += content + "\n"

    thoughts_text = ""
    if sr_thoughts:
        thoughts_text = "\n--- Your earlier evaluations ---\n"
        for i, t in enumerate(sr_thoughts, 1):
            if t:
                thoughts_text += f"{i}. {t[:500]}\n"

    # Build the full message list: original conversation + synthesis request
    synth_messages = [{"role": "system", "content": _SYNTH_SYSTEM}]

    # Include original conversation context (system + history)
    for m in original_messages:
        if m.get("role") == "system":
            synth_messages[0]["content"] += f"\n\nOriginal system context: {m['content']}"
        else:
            synth_messages.append(dict(m))

    # Add synthesis request
    synth_messages.append({
        "role": "user",
        "content": (
            f"I previously asked the question above. Here are {len(collected)} responses "
            f"from different AI endpoints in the cluster:\n"
            f"{responses_text}\n"
            f"{thoughts_text}\n"
            "Now synthesize the best possible final answer to my original question. "
            "Respond directly — do not reference the endpoints or this synthesis process."
        ),
    })

    # Truncate for showrunner's context
    synth_messages = _truncate_messages_for_context(synth_messages, sr["context_length"])

    task_id = _send_to_endpoint(sr["node_id"], sr["model"], synth_messages, "oapi-synth")
    result = _wait_for_result(task_id, timeout=_SYNTHESIS_TIMEOUT)

    if not result:
        return None, f"showrunner synthesis timed out ({sr['model']})"

    content = ""
    choices = result.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
    if not content:
        content = result.get("content", "")

    if not content:
        return None, "showrunner returned empty response"

    content = _strip_think_tags(content)
    return content, None


# ── Conversation management ──────────────────────────────────────────────

def _get_or_create_conv(conv_id=None):
    """Get or create a conversation. Returns conv dict."""
    with _lock:
        if conv_id and conv_id in _conversations:
            return _conversations[conv_id]

        if not conv_id:
            conv_id = secrets.token_hex(8)

        conv = {
            "id": conv_id,
            "created": time.time(),
            "updated": time.time(),
            "title": "",
            "turns": [],
        }
        _conversations[conv_id] = conv
        _conv_order.insert(0, conv_id)

        # Enforce max conversations
        while len(_conv_order) > _MAX_CONVERSATIONS:
            old_id = _conv_order.pop()
            _conversations.pop(old_id, None)

        return conv


def _add_turn(conv_id, role, content, meta=None):
    """Add a turn to a conversation."""
    with _lock:
        conv = _conversations.get(conv_id)
        if not conv:
            return
        turn = {"role": role, "content": content, "timestamp": time.time()}
        if meta:
            turn["_meta"] = meta
        conv["turns"].append(turn)

        # Auto-title from first user message
        if not conv["title"] and role == "user":
            conv["title"] = content[:80] + ("..." if len(content) > 80 else "")

        conv["updated"] = time.time()

        # Trim old turns
        if len(conv["turns"]) > _MAX_HISTORY_TURNS:
            conv["turns"] = conv["turns"][-_MAX_HISTORY_TURNS:]

        # Move to front of order
        if conv_id in _conv_order:
            _conv_order.remove(conv_id)
        _conv_order.insert(0, conv_id)


def _list_conversations():
    with _lock:
        result = []
        for cid in _conv_order:
            conv = _conversations.get(cid)
            if conv:
                result.append({
                    "id": conv["id"],
                    "title": conv["title"],
                    "created": conv["created"],
                    "updated": conv["updated"],
                    "turns": len(conv["turns"]),
                })
        return result


def _get_conversation(conv_id):
    with _lock:
        conv = _conversations.get(conv_id)
        return dict(conv) if conv else None


def _delete_conversation(conv_id):
    with _lock:
        if conv_id in _conversations:
            del _conversations[conv_id]
            if conv_id in _conv_order:
                _conv_order.remove(conv_id)
            return True
        return False


def _clear_conversations():
    with _lock:
        _conversations.clear()
        _conv_order.clear()


# ── HTTP Handler ─────────────────────────────────────────────────────────

class OAPIHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    # ── Routing ──────────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/models":
            self._models()
        elif path == "/api/oapi/status":
            self._oapi_status()
        elif path == "/api/oapi/config":
            self._get_config()
        elif path == "/api/oapi/conversations":
            self._list_convs()
        elif path.startswith("/api/oapi/conversations/"):
            self._get_conv()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            self._chat_completions()
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        path = self.path.split("?")[0]
        if path == "/api/oapi/config":
            self._put_config()
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path == "/api/oapi/conversations":
            _clear_conversations()
            self._json(200, {"ok": True})
        elif path.startswith("/api/oapi/conversations/"):
            cid = path.rsplit("/", 1)[-1]
            if _delete_conversation(cid):
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "conversation not found"})
        else:
            self._json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ── /v1/models ───────────────────────────────────────────────────────

    def _models(self):
        """Return available models in OpenAI format."""
        endpoints = _collect_ready_endpoints()
        models = [{
            "id": "clusterflock",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "clusterflock",
            "permission": [],
            "root": "clusterflock",
            "parent": None,
        }]
        # Also expose individual endpoint models
        seen = set()
        for ep in endpoints:
            if ep["model"] not in seen:
                seen.add(ep["model"])
                models.append({
                    "id": ep["model"],
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "clusterflock",
                    "permission": [],
                    "root": ep["model"],
                    "parent": None,
                })
        self._json(200, {"object": "list", "data": models})

    # ── /v1/chat/completions ─────────────────────────────────────────────

    def _chat_completions(self):
        body = self._body()
        if body is None:
            return

        messages = body.get("messages", [])
        if not messages:
            return self._json(400, {"error": "messages array required"})

        # Validate messages structure
        for m in messages:
            if "role" not in m or "content" not in m:
                return self._json(400, {"error": "each message must have role and content"})

        # Check queue capacity
        global _queue_depth
        with _lock:
            if _queue_depth >= _MAX_QUEUE:
                return self._json(429, {
                    "error": {
                        "message": f"server busy — {_queue_depth} requests queued (max {_MAX_QUEUE})",
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                })
            _queue_depth += 1
            _status["requests_total"] += 1
            _status["queue_depth"] = _queue_depth

        try:
            # Serialize processing
            with _active_request:
                _status["last_request_at"] = time.time()

                # Manage conversation
                conv_id = body.get("_conversation_id")
                conv = _get_or_create_conv(conv_id)
                conv_id = conv["id"]

                # Store user turn
                user_msg = messages[-1].get("content", "")
                _add_turn(conv_id, "user", user_msg)

                # Process — forward max_tokens and sampling params from request body
                req_max_tokens = 0
                try:
                    mt = body.get("max_tokens")
                    if mt is not None:
                        req_max_tokens = int(mt)
                except (ValueError, TypeError):
                    pass

                sampling_params = {}
                for p in _FORWARDED_PARAMS:
                    v = body.get(p)
                    if v is not None:
                        sampling_params[p] = v

                response, err = _process_chat_completion(
                    messages, conv_id, max_tokens=req_max_tokens,
                    sampling_params=sampling_params or None,
                )

                if err:
                    _status["requests_failed"] += 1
                    return self._json(503, {"error": {"message": err, "type": "server_error"}})

                # Store assistant turn
                assistant_content = response["choices"][0]["message"]["content"]
                _add_turn(conv_id, "assistant", assistant_content, meta={
                    "endpoints_queried": response["_clusterflock"]["endpoints_queried"],
                    "endpoints_responded": response["_clusterflock"]["endpoints_responded"],
                    "showrunner": response["_clusterflock"]["showrunner"],
                })

                # Add conversation_id to response for UI tracking
                response["_conversation_id"] = conv_id

                _status["requests_completed"] += 1
                self._json(200, response)

        finally:
            with _lock:
                _queue_depth -= 1
                _status["queue_depth"] = _queue_depth

    # ── Status & config endpoints ───────────────────────────────────────

    def _oapi_status(self):
        sr = dict(_showrunner)
        sr.pop("elected_at", None)
        endpoints = _collect_ready_endpoints()
        cfg = get_oapi_config()
        self._json(200, {
            "showrunner": sr,
            "endpoints_ready": len(endpoints),
            "endpoints": [{"model": e["model"], "hostname": e["hostname"],
                           "toks_per_sec": e["toks_per_sec"],
                           "context_length": e["context_length"],
                           "graylisted": e.get("graylisted", False)}
                          for e in endpoints],
            "queue": dict(_status),
            "oapi_mode": cfg["mode"],
            "oapi_manual_model": cfg["manual_model"],
            "thinking_power": cfg["thinking_power"],
            "max_tokens": cfg["max_tokens"],
            "config": {
                "max_queue": _MAX_QUEUE,
                "fanout_timeout": _FANOUT_TIMEOUT,
                "synthesis_timeout": _SYNTHESIS_TIMEOUT,
                "thinking_power_min": _THINKING_POWER_MIN,
                "thinking_power_max": _THINKING_POWER_MAX,
            },
        })

    def _get_config(self):
        cfg = get_oapi_config()
        endpoints = _collect_ready_endpoints()
        models = []
        for e in endpoints:
            key = e["model"] + "@" + e["hostname"]
            models.append({"key": key, "model": e["model"], "hostname": e["hostname"]})
        self._json(200, {
            "mode": cfg["mode"],
            "manual_model": cfg["manual_model"],
            "thinking_power": cfg["thinking_power"],
            "max_tokens": cfg["max_tokens"],
            "available_models": models,
        })

    def _put_config(self):
        body = self._body()
        if body is None:
            return
        mode = body.get("mode")
        if mode and mode not in _VALID_MODES:
            return self._json(400, {"error": f"invalid mode — must be one of {_VALID_MODES}"})
        manual_model = body.get("manual_model")
        if mode:
            set_oapi_mode(mode, manual_model=manual_model)
        elif manual_model is not None:
            with _lock:
                global _oapi_manual_model
                _oapi_manual_model = manual_model

        # Thinking power slider
        global _oapi_thinking_power, _oapi_max_tokens
        tp = body.get("thinking_power")
        if tp is not None:
            with _lock:
                _oapi_thinking_power = max(_THINKING_POWER_MIN, min(int(tp), _THINKING_POWER_MAX))

        # Global max_tokens
        mt = body.get("max_tokens")
        if mt is not None:
            with _lock:
                _oapi_max_tokens = max(0, int(mt))

        # Trigger persist via server module (best-effort)
        try:
            import server as srv_mod
            srv_mod._persist()
        except Exception:
            pass
        cfg = get_oapi_config()
        _log(f"config      mode={cfg['mode']} thinking_power={cfg['thinking_power']}s max_tokens={cfg['max_tokens']}")
        self._json(200, {"ok": True, **cfg})

    # ── Conversation endpoints ───────────────────────────────────────────

    def _list_convs(self):
        self._json(200, {"conversations": _list_conversations()})

    def _get_conv(self):
        cid = self.path.split("?")[0].rsplit("/", 1)[-1]
        conv = _get_conversation(cid)
        if not conv:
            return self._json(404, {"error": "conversation not found"})
        self._json(200, conv)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "invalid JSON"})
            return None

    def _json(self, code, data):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_error(self, code):
        body = f"<h1>{code}</h1>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Module-level ─────────────────────────────────────────────────────────

def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] oapi: {msg}")


def serve(host="0.0.0.0", port=_DEFAULT_PORT):
    """Start the OAPI server on its own port."""
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), OAPIHandler)
    _log(f"OAPI server listening on {host}:{port}")
    _log(f"OpenAI-compatible API: http://{host}:{port}/v1/chat/completions")
    _log(f"Models endpoint:       http://{host}:{port}/v1/models")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
        httpd.server_close()


def start_background(host="0.0.0.0", port=_DEFAULT_PORT):
    """Start the OAPI server in a background daemon thread."""
    t = threading.Thread(target=serve, args=(host, port), daemon=True)
    t.start()
    return t
