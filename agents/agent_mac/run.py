#!/usr/bin/env python3
"""ClusterFlock Mac Agent — Apple Silicon native entry point.

Uses llama.cpp with Metal directly (no LM Studio / Ollama dependency).
Optimized for M-series chips with unified memory.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

CONFIG = Path(__file__).parent / "cluster.json"


def _find_link_dir():
    """Locate agent_lms/link.py anywhere in the project tree."""
    here = Path(__file__).resolve().parent
    for d in [here,
              here.parent / "agent_lms",
              here.parent.parent / "agents" / "agent_lms",
              here.parent.parent / "agent_lms"]:
        if (d / "link.py").is_file():
            return str(d)
    for p in here.parents:
        for sub in ["agent_lms", "agents/agent_lms"]:
            if (p / sub / "link.py").is_file():
                return str(p / sub)
    return None


def run_agent(config, port=1903):
    """Run the Mac agent loop with the given config."""
    from hardware import profile, live_metrics, is_apple_silicon
    from server import server_running as _server_running, loaded_models, DEFAULT_PORT
    from commands import execute, current_model
    from models_hf import get_bench, local_models
    from version import __version__ as agent_version

    # Import link (shared transport layer) — bundled copy or fallback search
    _link_dir = _find_link_dir()
    if _link_dir and _link_dir != str(Path(__file__).resolve().parent):
        sys.path.insert(0, _link_dir)
    try:
        from link import start
    except ModuleNotFoundError:
        print("ERROR: link.py not found. Copy agents/agent_lms/link.py into this directory.")
        sys.exit(1)

    hw = profile()
    node_id = os.environ.get("CLUSTERFLOCK_NODE_ID", config.get("node_id", ""))
    hostname = config.get("hostname", "")

    if is_apple_silicon():
        print(f"[mac] ✓ Apple Silicon detected (Metal GPU)")
    else:
        print(f"[mac] ⚠ Not Apple Silicon — Metal may not be available")

    def payload():
        models = []
        model_id = current_model()
        if model_id and _server_running():
            for m in loaded_models():
                mid = m.get("id", model_id)
                models.append({
                    "id": mid,
                    "model": mid,
                    "status": "ready",
                    "context_length": config.get("context_length", 65536),
                    "tokens_per_sec": get_bench(model_id),
                })
        ctx_total = sum(m.get("context_length", 0) for m in models)
        dl = [{"id": m["id"], "name": m["name"],
               "file_size": int(m["size_gb"] * 1024**3)}
              for m in local_models()]
        return {
            "node_id": node_id,
            "hostname": hostname,
            "timestamp": time.time(),
            "agent_version": agent_version,
            "agent_type": "mac",
            "downloaded": dl,
            "hardware": hw,
            "metrics": live_metrics(),
            "context_tokens": ctx_total,
            "endpoints": models,
        }

    start({**config, "agent_version": agent_version},
          payload_fn=payload, command_fn=execute, port=port)


def main():
    parser = argparse.ArgumentParser(description="ClusterFlock Mac Agent")
    parser.add_argument("command", nargs="?", choices=["run", "build"], default=None)
    parser.add_argument("--port", type=int, default=1903)
    args = parser.parse_args()

    if args.command == "build":
        from server import build
        build()
        return

    if os.environ.get("CLUSTERFLOCK_LOCAL") == "1":
        import socket
        config = {
            "node_id": os.environ.get("CLUSTERFLOCK_NODE_ID", f"local-{socket.gethostname()}"),
            "hostname": socket.gethostname(),
        }
    elif not CONFIG.exists():
        from setup import run_setup
        run_setup()
        if not CONFIG.exists():
            print("Setup did not create cluster.json.")
            sys.exit(1)
        config = json.loads(CONFIG.read_text())
    else:
        config = json.loads(CONFIG.read_text())

    run_agent(config, port=args.port)


if __name__ == "__main__":
    main()
