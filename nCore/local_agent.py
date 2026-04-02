"""Local agent manager — discover, start, stop co-located agents.

When nCore and an agent run on the same machine, this module handles:
  • Discovery: scan ../agents/ for available agent types
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

_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"

_lock = threading.Lock()
_proc = None          # subprocess.Popen
_agent_type = None    # e.g. "agent_spark"
_local_token = None   # bearer token for the local agent
_node_id = None       # node_id of the running local agent


def available_agents():
    """Return list of discovered agent types with metadata."""
    agents = []
    if not _AGENTS_DIR.is_dir():
        return agents
    for d in sorted(_AGENTS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        run_py = d / "run.py"
        if not run_py.exists():
            continue
        # Try to read version
        version = None
        ver_py = d / "version.py"
        if ver_py.exists():
            try:
                for line in ver_py.read_text().splitlines():
                    if line.startswith("__version__"):
                        version = line.split("=", 1)[1].strip().strip("\"'")
                        break
            except Exception:
                pass
        agents.append({
            "agent_type": d.name,
            "path": str(d),
            "version": version,
        })
    return agents


def recommended_agent():
    """Auto-detect the best agent flavour for this machine."""
    import platform
    system = platform.system()
    machine = platform.machine().lower()
    avail = {a["agent_type"] for a in available_agents()}

    if system == "Darwin" and machine in ("arm64", "aarch64"):
        for pick in ("agent_mac", "agent_lms"):
            if pick in avail:
                return pick
    elif system == "Linux":
        # Check for DGX Spark (GB10 Blackwell) first
        if "agent_spark" in avail:
            try:
                with open("/sys/firmware/devicetree/base/model", "r") as f:
                    if "DGX" in f.read():
                        return "agent_spark"
            except Exception:
                pass
            try:
                import subprocess as _sp
                out = _sp.check_output(["nvidia-smi", "-L"], timeout=5,
                                       stderr=_sp.DEVNULL).decode()
                if "GB10" in out or "GB20" in out:
                    return "agent_spark"
            except Exception:
                pass
        if "agent_linux" in avail:
            return "agent_linux"
        if "agent_lms" in avail:
            return "agent_lms"
    # Fallback: first available
    return next(iter(avail), None)


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


def start_agent(agent_type, ncore_port=1903):
    """Start a local agent subprocess. Returns (ok, error_msg)."""
    global _proc, _agent_type, _local_token, _node_id

    with _lock:
        # Enforce single agent
        if _proc is not None and _proc.poll() is None:
            return False, f"agent already running: {_agent_type} (pid {_proc.pid})"

    # Validate agent_type
    agent_dir = _AGENTS_DIR / agent_type
    run_py = agent_dir / "run.py"
    if not run_py.exists():
        return False, f"agent not found: {agent_type}"

    hostname = socket.gethostname()
    nid = f"local-{hostname}"
    token = gen_token(nid, label=f"local-{agent_type}")
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
        _agent_type = agent_type
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
