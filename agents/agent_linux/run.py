#!/usr/bin/env python3
"""ClusterFlock Linux Agent — generic Linux (amd64 + CUDA) entry point.

Uses llama.cpp directly (no LM Studio / Ollama dependency).
Supports 0–N NVIDIA GPUs plus optional CPU/RAM inference device.
Each device runs its own llama-server instance independently.
No model splitting — each model runs entirely on one device.

Host requirements: NVIDIA driver only (no CUDA toolkit needed).
Prebuilt binaries in build/{cuda12,cuda11,cpu}/ bundle all CUDA runtime libs.
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
    """Run the Linux agent loop."""
    from hardware import profile, live_metrics, gpu, _mem_info
    from server import (active_devices, loaded_models as _loaded_models,
                        benchmark as _benchmark, _port_for_device)
    from commands import (execute, all_loaded_models, cpu_ram_enabled,
                          init_settings, get_activity)
    from models_hf import get_bench, local_models
    from version import __version__ as agent_version

    _bench_failed = set()  # Track models where benchmark timed out

    # Load saved settings (cpu_ram_enabled, etc.)
    init_settings()

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

    n_gpus = len(hw.get("gpu", []))
    print(f"[linux] {n_gpus} GPU(s) detected")
    if cpu_ram_enabled():
        print(f"[linux] CPU/RAM device enabled")

    def payload():
        endpoints = []
        devices = active_devices()

        for dev_id, dev_info in devices.items():
            model_id = dev_info.get("model_id")
            dev_port = dev_info.get("port")

            if not model_id:
                models = _loaded_models(port=dev_port)
                if models:
                    model_id = models[0].get("id", "")

            if model_id:
                # Auto-benchmark first time we see a model on this device
                if get_bench(model_id, device=dev_id) == 0 and model_id not in _bench_failed:
                    try:
                        perf = _benchmark(port=dev_port)
                        from models_hf import save_bench
                        save_bench(model_id, perf, device=dev_id)
                        print(f"  [payload] Benchmark {dev_id}: "
                              f"{perf['tokens_per_sec']} tok/s")
                    except Exception as e:
                        _bench_failed.add(model_id)
                        print(f"  [payload] Benchmark failed: {e}")

                gpu_idx = "cpu" if dev_id == "cpu" else int(
                    dev_id.replace("gpu", ""))

                endpoints.append({
                    "id": model_id,
                    "model": model_id,
                    "status": "ready",
                    "gpu": gpu_idx,
                    "device": dev_id,
                    "context_length": config.get("context_length", 131072),
                    "tokens_per_sec": get_bench(model_id, device=dev_id),
                })

        # Build hardware profile, including CPU device if enabled
        hw_live = profile()
        gpu_list = list(hw_live.get("gpu", []))

        # ── LINUX-ONLY: CPU/RAM as additional virtual device ──
        # When cpu_ram_enabled, add free system RAM as a schedulable device.
        # The orchestrator treats it like any other GPU with its own VRAM budget.
        # Assume 4 CPU cores for inference (as per spec).
        if cpu_ram_enabled():
            _, ram_free = _mem_info()
            gpu_list.append({
                "name": "CPU (System RAM)",
                "device": "cpu",
                "vram_total_mb": ram_free,
                "vram_free_mb": ram_free,
                "utilization_pct": 0,
                "cpu_cores": 4,
            })

        hw_payload = {
            "gpu": gpu_list,
            "system": hw_live.get("system", {}),
        }

        ctx_total = sum(ep.get("context_length", 0) for ep in endpoints)
        dl = [{"id": m["id"], "name": m["name"],
               "file_size": int(m["size_gb"] * 1024**3)}
              for m in local_models()]
        return {
            "node_id": node_id,
            "hostname": hostname,
            "timestamp": time.time(),
            "agent_version": agent_version,
            "agent_type": "linux",
            "cpu_ram_enabled": cpu_ram_enabled(),
            "downloaded": dl,
            "hardware": hw_payload,
            "metrics": live_metrics(),
            "context_tokens": ctx_total,
            "endpoints": endpoints,
            "activity": get_activity(),
        }

    start({**config, "agent_version": agent_version},
          payload_fn=payload, command_fn=execute, port=port)


def main():
    parser = argparse.ArgumentParser(
        description="ClusterFlock Linux Agent — amd64 + CUDA (llama.cpp)")
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
