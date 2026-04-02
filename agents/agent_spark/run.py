#!/usr/bin/env python3
"""ClusterFlock Spark Agent — DGX Spark native entry point.

Uses llama.cpp directly (no LM Studio / Ollama dependency).
Optimized for GB10 with NVFP4 KV cache and Flash Attention.
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
    """Run the Spark agent loop with the given config."""
    import os
    from hardware import profile, live_metrics, is_dgx_spark
    from server import server_running as _server_running, loaded_models, DEFAULT_PORT
    from commands import execute, current_model, get_activity
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
    # In local mode, nCore provides the node_id via env
    node_id = os.environ.get("CLUSTERFLOCK_NODE_ID", config.get("node_id", ""))
    hostname = config.get("hostname", "")

    if is_dgx_spark():
        print(f"[spark] ✓ DGX Spark detected (GB10 Blackwell)")
    else:
        print(f"[spark] ⚠ Not running on DGX Spark — some optimizations may not apply")

    from server import benchmark as _benchmark
    _bench_failed = set()

    def payload():
        models = []
        model_id = current_model()
        if _server_running():
            loaded = loaded_models()
            # Detect model even if agent didn't start the server itself
            if not model_id and loaded:
                model_id = loaded[0].get("id", "")
            if model_id:
                # Auto-benchmark on first detection
                if get_bench(model_id) == 0 and model_id not in _bench_failed:
                    try:
                        perf = _benchmark()
                        from models_hf import save_bench
                        save_bench(model_id, perf)
                        print(f"  [payload] Benchmark: {perf['tokens_per_sec']} tok/s")
                    except Exception as e:
                        _bench_failed.add(model_id)
                        print(f"  [payload] Benchmark failed: {e}")
                for m in loaded:
                    models.append({
                        "id": model_id,
                        "model": model_id,
                        "status": "ready",
                        "context_length": config.get("context_length", 131072),
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
            "agent_type": "spark",
            "downloaded": dl,
            "hardware": hw,
            "metrics": live_metrics(),
            "context_tokens": ctx_total,
            "endpoints": models,
            "activity": get_activity(),
        }

    start({**config, "agent_version": agent_version},
          payload_fn=payload, command_fn=execute, port=port)


def main():
    parser = argparse.ArgumentParser(
        description="ClusterFlock Spark Agent — DGX Spark native (llama.cpp)")
    parser.add_argument("command", nargs="?", choices=["run", "build"], default=None)
    parser.add_argument("--port", type=int, default=1903, help="Agent listen port")
    parser.add_argument("--jobs", type=int, default=None, help="Parallel build jobs")
    args = parser.parse_args()

    if args.command == "build":
        from server import build
        build(jobs=args.jobs)
        return

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
        config = json.loads(CONFIG.read_text())

    run_agent(config, port=args.port)


if __name__ == "__main__":
    main()
