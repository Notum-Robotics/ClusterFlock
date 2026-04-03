#!/usr/bin/env python3
"""ClusterFlock Agent — unified entry point for all platforms.

Auto-detects platform (Apple Silicon / DGX Spark / Linux CUDA) and
runs a multi-device agent loop.  Each device gets its own llama-server
instance.  On macOS, a single "gpu0" covers the whole SoC.

Uses llama.cpp directly — no LM Studio or Ollama dependency.
"""

import argparse
import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path

CONFIG = Path(__file__).parent / "cluster.json"
_PIDFILE = Path("/tmp/clusterflock_agent.pid")


def _acquire_pidlock():
    """Ensure only one agent runs per machine.  Exit if another is alive."""
    if _PIDFILE.exists():
        try:
            old_pid = int(_PIDFILE.read_text().strip())
            # Check if that process is still alive
            os.kill(old_pid, 0)
            # Still alive — bail out
            print(f"ERROR: Another agent is already running (pid {old_pid}).  "
                  f"Kill it first or remove {_PIDFILE}")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale pidfile — previous process died without cleanup
            pass
    _PIDFILE.write_text(str(os.getpid()))
    atexit.register(_release_pidlock)


def _release_pidlock():
    """Remove PID file on normal exit."""
    try:
        if _PIDFILE.exists() and _PIDFILE.read_text().strip() == str(os.getpid()):
            _PIDFILE.unlink()
    except OSError:
        pass


def _sigterm_handler(signum, _frame):
    """Clean up pidfile on SIGTERM then exit."""
    _release_pidlock()
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)


def _find_link_dir():
    """Locate link.py — bundled copy or fallback search."""
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
    """Run the agent loop with platform-aware payload."""
    _acquire_pidlock()
    from hardware import profile, live_metrics, detect_platform, _mem_info
    from server import (active_devices, loaded_models as _loaded_models,
                        benchmark as _benchmark, _port_for_device,
                        get_server_context)
    from commands import (execute, all_loaded_models, cpu_ram_enabled,
                          init_settings, get_activity, check_crashed_servers)
    from models_hf import get_bench, save_bench, local_models
    from version import __version__ as agent_version

    _bench_failed = set()

    # Load saved settings (cpu_ram_enabled, etc.)
    init_settings()

    # Import link (shared transport layer)
    _link_dir = _find_link_dir()
    if _link_dir and _link_dir != str(Path(__file__).resolve().parent):
        sys.path.insert(0, _link_dir)
    try:
        from link import start
    except ModuleNotFoundError:
        print("ERROR: link.py not found. Copy it into this directory.")
        sys.exit(1)

    hw = profile()
    node_id = os.environ.get("CLUSTERFLOCK_NODE_ID", config.get("node_id", ""))
    hostname = config.get("hostname", "")

    plat = detect_platform()
    agent_type = config.get("agent_type", plat)
    if plat == "mac":
        print(f"[agent] ✓ Apple Silicon detected (Metal GPU)")
    elif plat == "spark":
        print(f"[agent] ✓ DGX Spark detected (GB10 Blackwell)")
    else:
        n_gpus = len(hw.get("gpu", []))
        print(f"[agent] Linux — {n_gpus} GPU(s) detected")
    if cpu_ram_enabled():
        print(f"[agent] CPU/RAM device enabled")

    def payload():
        # Auto-restart any crashed llama-server instances
        check_crashed_servers()

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
                # Auto-benchmark first time
                if (get_bench(model_id, device=dev_id) == 0
                        and model_id not in _bench_failed):
                    try:
                        perf = _benchmark(port=dev_port)
                        save_bench(model_id, perf, device=dev_id)
                        print(f"  [payload] Benchmark {dev_id}: "
                              f"{perf['tokens_per_sec']} tok/s")
                    except Exception as e:
                        _bench_failed.add(model_id)
                        print(f"  [payload] Benchmark failed: {e}")

                # Context: try live value, fall back to config
                ctx = get_server_context(device=dev_id)
                if not ctx:
                    ctx = config.get("context_length", 131072)

                gpu_idx = ("cpu" if dev_id == "cpu"
                           else int(dev_id.replace("gpu", "")))

                endpoints.append({
                    "id": model_id,
                    "model": model_id,
                    "status": "ready",
                    "gpu": gpu_idx,
                    "device": dev_id,
                    "context_length": ctx,
                    "tokens_per_sec": get_bench(model_id, device=dev_id),
                })

        # Build hardware profile, including CPU device if enabled
        hw_live = profile()
        gpu_list = list(hw_live.get("gpu", []))

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
            "agent_type": agent_type,
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
        description="ClusterFlock Agent — unified (llama.cpp)")
    parser.add_argument("command", nargs="?", choices=["run", "build"],
                        default=None)
    parser.add_argument("--port", type=int, default=1903,
                        help="Agent listen port")
    parser.add_argument("--jobs", type=int, default=None,
                        help="Parallel build jobs")
    args = parser.parse_args()

    if args.command == "build":
        from server import build
        build(jobs=args.jobs)
        return

    if os.environ.get("CLUSTERFLOCK_LOCAL") == "1":
        import socket
        config = {
            "node_id": os.environ.get("CLUSTERFLOCK_NODE_ID",
                                      f"local-{socket.gethostname()}"),
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
