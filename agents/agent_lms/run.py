#!/usr/bin/env python3
"""ClusterFlock nNode Agent — entry point.

Wires hardware profiling, model endpoints, and command dispatch
into the nCore transport layer (link.py).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

CONFIG = Path(__file__).parent / "cluster.json"


def run_agent(config, port=1903):
    """Run the agent loop with the given config."""
    import os
    from hardware import profile, live_metrics
    from studio import loaded_models, downloaded_for_heartbeat, get_bench, benchmark as _benchmark
    from commands import execute
    from link import start
    from version import __version__ as agent_version

    # Unload any lingering models so VRAM profile reflects true capacity
    from studio import lms_ps, unload_all as _unload_all, ensure_server
    try:
        ensure_server()
        stale = lms_ps()
        if stale:
            names = [m.get("identifier") or m.get("modelKey") or "?" for m in stale]
            print(f"[startup] Unloading {len(stale)} stale model(s): {', '.join(names)}")
            _unload_all()
            time.sleep(2)  # let VRAM settle
        else:
            print("[startup] No models loaded — VRAM is clean")
    except Exception as e:
        print(f"[startup] LM Studio not available ({e}) — continuing without it")

    # Benchmark any loaded models missing cached benchmarks
    try:
        for m in loaded_models():
            mid = m.get("id", "")
            if mid and get_bench(mid) == 0:
                print(f"[startup] Benchmarking {mid}...")
                try:
                    perf = _benchmark(mid)
                    print(f"[startup]   {perf['tokens_per_sec']} tok/s")
                except Exception as e:
                    print(f"[startup]   Benchmark failed: {e}")
    except Exception:
        pass

    hw = profile()
    # In local mode, nCore provides the node_id via env
    node_id = os.environ.get("CLUSTERFLOCK_NODE_ID", config.get("node_id", ""))
    hostname = config.get("hostname", "")

    def payload():
        models = loaded_models()
        ctx_total = sum(m.get("context_length", 0) for m in models)
        return {
            "node_id": node_id,
            "hostname": hostname,
            "timestamp": time.time(),
            "agent_version": agent_version,
            "agent_type": "lms",
            "hardware": hw,
            "metrics": live_metrics(),
            "context_tokens": ctx_total,
            "downloaded": downloaded_for_heartbeat(),
            "endpoints": [
                {"model": m["id"], "status": "ready",
                 "context_length": m.get("context_length", 0),
                 "tokens_per_sec": get_bench(m["id"])}
                for m in models if m.get("id")
            ],
        }

    start({**config, "agent_version": agent_version}, payload_fn=payload, command_fn=execute, port=port)


def main():
    p = argparse.ArgumentParser(prog="nnode", description="ClusterFlock nNode Agent")
    p.add_argument("command", nargs="?", choices=["run"], default=None)
    p.add_argument("--port", type=int, default=1903, help="Listener port (push mode)")
    args = p.parse_args()

    if os.environ.get("CLUSTERFLOCK_LOCAL") == "1":
        config = {"node_id": os.environ.get("CLUSTERFLOCK_NODE_ID", ""),
                  "hostname": __import__("socket").gethostname()}
    elif not CONFIG.exists():
        from setup import run_setup
        run_setup()
        if not CONFIG.exists():
            print("Setup did not create cluster.json.")
            sys.exit(1)
        config = json.loads(CONFIG.read_text())
    else:
        try:
            config = json.loads(CONFIG.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error: cluster.json is malformed: {e}")
            sys.exit(1)

    run_agent(config, port=args.port)


if __name__ == "__main__":
    main()
