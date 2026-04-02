"""Local agent manager — start and stop the co-located unified agent.

When nCore and the agent run on the same machine, this module handles:
  • Lifecycle: start agent as subprocess with local-mode env vars
  • Registration: pre-register in registry with conn_mode="local"
  • Enforcement: only one local agent at a time (PID tracking)

The started agent receives CLUSTERFLOCK_LOCAL=1, CLUSTERFLOCK_NCORE_PORT,
and CLUSTERFLOCK_LOCAL_TOKEN via environment — link.py detects these and
connects directly to localhost, skipping negotiation and auth.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from registry import register as reg_node, remove as rm_node, get_node
from auth import generate as gen_token

_AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"

_lock = threading.Lock()
_proc = None          # subprocess.Popen
_agent_type = None    # "agent"
_local_token = None   # bearer token for the local agent
_node_id = None       # node_id of the running local agent


def available_agents():
    """Return list with the unified agent if present."""
    run_py = _AGENT_DIR / "run.py"
    if not run_py.exists():
        return []
    version = None
    ver_py = _AGENT_DIR / "version.py"
    if ver_py.exists():
        try:
            for line in ver_py.read_text().splitlines():
                if line.startswith("__version__"):
                    version = line.split("=", 1)[1].strip().strip("\"'")
                    break
        except Exception:
            pass
    return [{
        "agent_type": "agent",
        "path": str(_AGENT_DIR),
        "version": version,
    }]


def recommended_agent():
    """Return 'agent' if it exists, else None."""
    if (_AGENT_DIR / "run.py").exists():
        return "agent"
    return None


def status():
    """Return current local agent status."""
    with _lock:
        running = _proc is not None and _proc.poll() is None
        return {
            "running": running,
            "agent_type": _agent_type if running else None,
            "node_id": _node_id if running else None,
            "pid": _proc.pid if running else None,
            "available": [a["agent_type"] for a in available_agents()],
            "recommended": recommended_agent(),
        }


def start_agent(agent_type=None, ncore_port=1903):
    """Start the local agent subprocess. Returns (ok, error_msg).

    agent_type is accepted for API compatibility but ignored — there is
    only one unified agent now.
    """
    global _proc, _agent_type, _local_token, _node_id

    with _lock:
        if _proc is not None and _proc.poll() is None:
            return False, f"agent already running (pid {_proc.pid})"

    agent_dir = _AGENT_DIR
    run_py = agent_dir / "run.py"
    if not run_py.exists():
        return False, f"agent not found at {agent_dir}"

    hostname = socket.gethostname()
    nid = f"local-{hostname}"
    token = gen_token(nid, label="local-agent")
    local_secret = secrets.token_urlsafe(32)

    # Pre-register in the registry
    reg_node(nid, hostname, conn_mode="local", token=token)

    # Build environment for the subprocess
    env = os.environ.copy()
    env["CLUSTERFLOCK_LOCAL"] = "1"
    env["CLUSTERFLOCK_NCORE_PORT"] = str(ncore_port)
    env["CLUSTERFLOCK_LOCAL_TOKEN"] = token
    env["CLUSTERFLOCK_NODE_ID"] = nid

    # Start the agent subprocess
    proc = subprocess.Popen(
        [sys.executable, "-u", str(run_py)],
        cwd=str(agent_dir),
        env=env,
        stdout=None,   # inherit nCore's stdout
        stderr=None,   # inherit nCore's stderr
    )

    with _lock:
        _proc = proc
        _agent_type = "agent"
        _local_token = token
        _node_id = nid

    # Monitor thread — detect unexpected exit
    threading.Thread(target=_monitor, args=(proc, nid), daemon=True).start()

    return True, None


def stop_agent():
    """Stop the running local agent. Returns (ok, error_msg)."""
    global _proc, _agent_type, _local_token, _node_id

    with _lock:
        proc = _proc
        nid = _node_id
        if proc is None or proc.poll() is not None:
            _proc = None
            _agent_type = None
            _local_token = None
            _node_id = None
            return True, None

    # Graceful shutdown
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Remove from registry
    if nid:
        rm_node(nid)

    with _lock:
        _proc = None
        _agent_type = None
        _local_token = None
        _node_id = None

    return True, None


def _monitor(proc, nid):
    """Wait for subprocess exit and clean up registry."""
    proc.wait()
    global _proc, _agent_type, _local_token, _node_id
    with _lock:
        if _proc is proc:
            _proc = None
            _agent_type = None
            _local_token = None
            _node_id = None
    # Don't remove from registry — let it show as dead so operator sees the crash
