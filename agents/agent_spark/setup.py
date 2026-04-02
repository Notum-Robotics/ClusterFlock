"""First-time setup for DGX Spark agent.

Builds llama.cpp, profiles hardware, picks models, registers with nCore.
No LM Studio or Ollama dependency — uses llama.cpp directly.
"""

import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

CONFIG = Path(__file__).parent / "cluster.json"


def run_setup():
    print("=== ClusterFlock Spark Agent Setup ===\n")

    # 1. Clean GPU: kill LM Studio / Ollama if present
    from gpu_cleanup import cleanup_gpu
    cleanup_gpu()

    # 2. Check / build llama.cpp
    from server import is_built, build, LLAMA_CPP_DIR
    if not LLAMA_CPP_DIR.exists():
        print("✗ llama.cpp source not found.")
        print(f"  Expected at: {LLAMA_CPP_DIR}")
        print("  Clone it:  git clone --depth 1 https://github.com/ggerganov/llama.cpp.git llama_cpp")
        sys.exit(1)

    if is_built():
        print("✓ llama-server already built")
    else:
        print("Building llama.cpp with CUDA (Spark-optimized)...")
        build()

    # 3. Check memlock (critical for mlock on large models)
    if platform.system() == "Linux":
        _check_memlock()

    # 4. Check huggingface_hub
    try:
        import huggingface_hub
        print(f"✓ huggingface_hub {huggingface_hub.__version__}")
    except ImportError:
        print("⚠ huggingface_hub not installed — downloads will use curl fallback")
        if input("  Install now? (pip install huggingface_hub) [Y/n] ").strip().lower() != "n":
            subprocess.run([sys.executable, "-m", "pip", "install", "huggingface_hub"],
                           check=False)

    # Load existing config for reuse
    existing = {}
    if CONFIG.exists():
        try:
            existing = json.loads(CONFIG.read_text()) or {}
        except (json.JSONDecodeError, ValueError):
            pass

    # 5. Hardware profile (GPU is clean after cleanup)
    from hardware import snapshot, is_dgx_spark
    hw = snapshot()
    _print_hw(hw)
    gpus = hw["gpu"]

    if is_dgx_spark():
        print("\n  ✓ DGX Spark detected — NVFP4 + FlashAttention optimizations enabled")
    else:
        print("\n  ⚠ Not a DGX Spark — some optimizations may not apply")

    # 6. Model selection skipped — models are managed via nCore commands
    tight_pack = existing.get("tight_pack", False)
    models = []
    benchmarks = []
    print("\n✓ Skipping model selection — models are loaded on-demand via nCore")

    # 7. Connection mode
    print("\nConnection mode:")
    print("  1) Pull — agent connects out to nCore (nCore address known)")
    print("  2) Push — agent listens, nCore connects in (behind NAT)")
    mode_choice = input("Choose [1]: ").strip()
    use_push = mode_choice == "2"

    node_id = existing.get("node_id") or uuid.uuid4().hex[:12]

    config = {
        "node_id": node_id,
        "hostname": platform.node(),
        "agent_type": "spark",
        "hardware": hw,
        "models": models,
        "benchmarks": benchmarks,
        "tight_pack": tight_pack,
    }

    if use_push:
        port = input("\nListen port [1903]: ").strip() or "1903"
        config["mode"] = "push"
        config["listen_port"] = int(port)
        print(f"  → Push mode on port {port}")
    else:
        address = input("\nnCore address [http://localhost:1903]: ").strip() or "http://localhost:1903"
        if not address.startswith("http"):
            address = f"http://{address}"
        config["address"] = address
        config["mode"] = "pull"
        reg = _register(address, config)
        if reg:
            config["node_id"] = reg.get("node_id", "") or config["node_id"]
            if reg.get("token"):
                config["token"] = reg["token"]
                print(f"  → Registered as {config['node_id']}")
            elif reg.get("status") == "pending":
                print(f"  → Pending approval on nCore (node: {config['node_id']})")
            else:
                print(f"  → Registered as {config['node_id']}")
        else:
            print("  → Could not reach nCore — agent will keep retrying.")

    CONFIG.write_text(json.dumps(config, indent=2))
    print(f"\nSaved: {CONFIG}")

    # 8. Service install
    if input("\nInstall as system service? [y/N] ").strip().lower() == "y":
        _install_service()
        print("\n✓ Setup complete.")
    else:
        print("\n✓ Setup complete. Run the agent with:")
        print(f"  python3 {Path(__file__).parent / 'run.py'} run")


# ── Internals ────────────────────────────────────────────────────────────────

def _check_memlock():
    """Ensure memlock ulimit is unlimited for mlock() on large model weights."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    if soft == resource.RLIM_INFINITY:
        print("✓ memlock unlimited")
        return
    soft_mb = soft // (1024 * 1024)
    print(f"⚠ memlock limit is {soft_mb} MB — models may fail to lock in memory")
    limits_file = Path("/etc/security/limits.d/99-memlock.conf")
    user = os.environ.get("USER", "notum")
    line = f"{user} - memlock unlimited\n"
    print(f"  Setting memlock unlimited via {limits_file} (requires sudo)")
    try:
        subprocess.run(
            ["sudo", "tee", str(limits_file)],
            input=line.encode(), capture_output=True, check=True,
        )
        print(f"  ✓ Written {limits_file} — log out/reboot to apply")
    except subprocess.CalledProcessError:
        print(f"  Could not write — set manually:")
        print(f"    echo '{user} - memlock unlimited' | sudo tee {limits_file}")


def _print_hw(hw):
    s = hw["system"]
    print(f"\n  Host:  {s['hostname']} ({s['os']}/{s['arch']})")
    print(f"  CPU:   {s['cpu_count']} cores")
    print(f"  RAM:   {s['ram_free_mb']:,}/{s['ram_total_mb']:,} MB free")
    print(f"  Disk:  {s['disk_free_gb']} GB free")
    for g in hw["gpu"]:
        tag = " (unified)" if g.get("unified") else ""
        vfree = g.get("vram_free_mb", 0)
        vtotal = g.get("vram_total_mb", 0)
        print(f"  GPU:   {g['name']}{tag} — {vfree:,}/{vtotal:,} MB VRAM")


def _register(address, config):
    try:
        body = json.dumps({
            "node_id": config.get("node_id", ""),
            "hostname": config.get("hostname", ""),
            "hardware": config.get("hardware"),
        }).encode()
        req = urllib.request.Request(
            f"{address}/api/v1/register", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 202:
            return json.loads(e.read())
        print(f"  Registration error: HTTP {e.code}")
        return None
    except Exception as e:
        print(f"  Registration error: {e}")
        return None


# ── Service installation ─────────────────────────────────────────────────────

def _install_service():
    agent_dir = Path(__file__).parent.resolve()
    py = sys.executable
    sysname = platform.system()

    if sysname == "Linux":
        _systemd(py, agent_dir)
    elif sysname == "Darwin":
        _launchd(py, agent_dir)
    else:
        print(f"  Unsupported OS for service install: {sysname}")


def _systemd(py, agent_dir):
    unit = f"""[Unit]
Description=ClusterFlock Spark Agent
After=network.target

[Service]
Type=simple
WorkingDirectory={agent_dir}
ExecStart={py} {agent_dir / "run.py"} run
Restart=always
RestartSec=5
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target"""
    path = "/etc/systemd/system/clusterflock-spark.service"
    print(f"  Writing {path} (requires sudo)")
    subprocess.run(["sudo", "tee", path], input=unit.encode(), capture_output=True)
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    subprocess.run(["sudo", "systemctl", "enable", "--now", "clusterflock-spark"])
    print("  ✓ systemd service enabled")


def _launchd(py, agent_dir):
    label = "com.notum.clusterflock.spark"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{agent_dir / "run.py"}</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key><string>{agent_dir}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/clusterflock-spark.log</string>
    <key>StandardErrorPath</key><string>/tmp/clusterflock-spark.log</string>
</dict></plist>""")
    subprocess.run(["launchctl", "load", str(plist)])
    print(f"  ✓ launchd: {plist}")
