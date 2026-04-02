"""PUSH-mode poller: periodically fetch heartbeats from push-mode agents
and deliver pending orchestrator commands."""

import json
import threading
import time
import urllib.request
import urllib.error

from registry import push_nodes, heartbeat as hb_node, get_node
import orchestrator as orch_mod

_interval = 5  # seconds, remotely configurable


def start(interval=None):
    if interval is not None:
        set_interval(interval)
    threading.Thread(target=_loop, daemon=True).start()


def get_interval():
    return _interval


def set_interval(val):
    global _interval
    _interval = max(2, min(30, int(val)))


def _loop(interval=None):
    while True:
        for nid, addr, tok in push_nodes():
            _poll(nid, addr, tok)
        time.sleep(_interval)


def _poll(node_id, address, token):
    try:
        req = urllib.request.Request(
            f"{address}/api/v1/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        hb_node(
            node_id,
            hostname=data.get("hostname", ""),
            metrics=data.get("metrics"),
            endpoints=data.get("endpoints"),
            hardware=data.get("hardware"),
            agent_version=data.get("agent_version"),
            downloaded=data.get("downloaded"),
            agent_type=data.get("agent_type"),
            cpu_ram_enabled=data.get("cpu_ram_enabled"),
        )
        # Track autoload progress from endpoints
        orch_mod.autoload_check_heartbeat(node_id, data.get("endpoints"))
    except Exception:
        pass  # node goes stale → dead via heartbeat timeout

    # Deliver any queued commands to this push-mode agent
    commands = orch_mod.drain(node_id)
    if not commands:
        return
    # Separate model ops (must run sequentially) from other commands
    model_ops = [c for c in commands if c.get("action") in ("load", "unload", "unload_all")]
    other = [c for c in commands if c.get("action") not in ("load", "unload", "unload_all")]
    for cmd in other:
        threading.Thread(
            target=_send_cmd, args=(node_id, address, token, cmd, None),
            daemon=True,
        ).start()
    if model_ops:
        # Serialize model ops in a single thread to prevent concurrent OOM
        threading.Thread(
            target=_send_model_ops, args=(node_id, address, token, model_ops),
            daemon=True,
        ).start()


def _send_model_ops(node_id, address, token, cmds):
    """Send model ops sequentially to avoid concurrent OOM."""
    node = get_node(node_id)
    host = (node.get("hostname", "") if node else "") or node_id[:6]
    t0 = time.time()
    for i, cmd in enumerate(cmds):
        action = cmd.get('action', '')
        model = cmd.get('model_id', '')
        gpu_tag = f" gpu={cmd['gpu_idx']}" if cmd.get('gpu_idx') is not None else ""
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] push  {host:20} [{i+1}/{len(cmds)}] {action} {model}{gpu_tag}")
        # Mark autoload step as sending
        if cmd.get("_autoload") and action == "load" and model:
            orch_mod._autoload_update_step(node_id, model, "sending")
        step_t = time.time()
        _send_cmd(node_id, address, token, cmd, hostname=host)
        elapsed = time.time() - step_t
        if elapsed > 2:
            print(f"[{time.strftime('%H:%M:%S')}] push  {host:20} [{i+1}/{len(cmds)}] done in {elapsed:.1f}s")
    total = time.time() - t0
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] push  {host:20} all {len(cmds)} model ops done ({total:.1f}s)")


def _send_cmd(node_id, address, token, cmd, hostname=None):
    """POST a command to the push-mode agent and forward the result."""
    host = hostname or node_id[:6]
    action = cmd.get("action", "")
    model = cmd.get("model_id", "")
    task_id = cmd.get("task_id")
    try:
        body = json.dumps(cmd).encode()
        req = urllib.request.Request(
            f"{address}/api/v1/command",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        ttl = cmd.get("ttl", 120)
        with urllib.request.urlopen(req, timeout=ttl + 10) as resp:
            result = json.loads(resp.read())
        if action in ("load", "unload", "unload_all"):
            ok = result.get("ok", False)
            err = result.get("error", "")
            tag = "ok" if ok else f"FAIL {err}" if err else "done"
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] push  {host:20} {action} {model}: {tag}")
            # Report autoload progress
            if cmd.get("_autoload") and action == "load" and model:
                orch_mod.autoload_record_load_result(
                    node_id, model, ok, error=err if not ok else None)
        # Always record prompt results (OpenAI-format has "choices", not "ok")
        if task_id and result:
            node = get_node(node_id)
            hn = node.get("hostname", "") if node else ""
            orch_mod.record_result(task_id, result,
                                   node_id=node_id, hostname=hn)
    except Exception as e:
        body_text = ""
        # HTTPError stores the response body; try multiple ways to extract it
        if hasattr(e, "read"):
            try:
                body_text = e.read().decode(errors="replace")
            except Exception:
                pass
        if not body_text and hasattr(e, "fp") and e.fp is not None:
            try:
                body_text = e.fp.read().decode(errors="replace")
            except Exception:
                pass
        detail = f" — {body_text}" if body_text else ""
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] push  {host:20} {action} {model}: FAIL {e}{detail}")
        # Report autoload failure
        if cmd.get("_autoload") and action == "load" and model:
            orch_mod.autoload_record_load_result(
                node_id, model, False, error=str(e))
        # Post error result so orchestrator unblocks waiting tasks
        if task_id:
            try:
                err_msg = f"push delivery failed: {e}{detail}"
                orch_mod.record_result(task_id,
                    {"error": err_msg, "_agent_error": True},
                    node_id=node_id, hostname=host)
            except Exception:
                pass
