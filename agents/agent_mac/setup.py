"""First-time setup for Mac agent.

Profiles hardware, picks models for available unified memory, registers with nCore.
No LM Studio or Ollama dependency — uses llama.cpp with Metal directly.
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
    print("=== ClusterFlock Mac Agent Setup ===\n")

    # 1. Clean up competing inference servers
    from gpu_cleanup import cleanup_gpu
    cleanup_gpu()

    # 2. Check / build llama.cpp
    from server import is_built, build, LLAMA_CPP_DIR
    if is_built():
        print("✓ llama-server already built")
    elif LLAMA_CPP_DIR.exists():
        print("Building llama.cpp with Metal (Apple Silicon)...")
        build()
    else:
        # No source, check prebuilt
        from server import PREBUILT_DIR
        if not (PREBUILT_DIR / "llama-server").exists():
            print("✗ llama-server binary not found.")
            print(f"  Expected prebuilt at: {PREBUILT_DIR}")
            print(f"  Or source at: {LLAMA_CPP_DIR}")
            print("  To build from source, clone llama.cpp:")
            print("    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git llama_cpp")
            sys.exit(1)
        print("✓ llama-server prebuilt binary found")

    # 3. Check huggingface_hub
    try:
        import huggingface_hub
        print(f"✓ huggingface_hub {huggingface_hub.__version__}")
    except ImportError:
        print("⚠ huggingface_hub not installed — downloads will use curl fallback")
        if input("  Install now? (pip install huggingface_hub) [Y/n] ").strip().lower() != "n":
            subprocess.run([sys.executable, "-m", "pip", "install", "huggingface_hub"],
                           check=False)

    # Load existing config
    existing = {}
    if CONFIG.exists():
        try:
            existing = json.loads(CONFIG.read_text()) or {}
        except (json.JSONDecodeError, ValueError):
            pass

    # 4. Hardware profile
    from hardware import snapshot, is_apple_silicon
    hw = snapshot()
    _print_hw(hw)
    gpus = hw["gpu"]

    if is_apple_silicon():
        print("\n  ✓ Apple Silicon detected — Metal GPU acceleration enabled")
    else:
        print("\n  ⚠ Not Apple Silicon — Metal may not be available")

    # 5. Model selection skipped — models are managed via nCore commands
    tight_pack = existing.get("tight_pack", False)
    models = []
    benchmarks = []
    print("\n✓ Skipping model selection — models are loaded on-demand via nCore")

    # 6. Connection mode
    print("\nConnection mode:")
    print("  1) Pull — agent connects out to nCore (nCore address known)")
    print("  2) Push — agent listens, nCore connects in (behind NAT)")
    mode_choice = input("Choose [1]: ").strip()
    use_push = mode_choice == "2"

    node_id = existing.get("node_id") or uuid.uuid4().hex[:12]

    config = {
        "node_id": node_id,
        "hostname": platform.node(),
        "agent_type": "mac",
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

    # 7. Service install
    if input("\nInstall as system service? [y/N] ").strip().lower() == "y":
        _install_service()
        print("\n✓ Setup complete.")
    else:
        print("\n✓ Setup complete. Run the agent with:")
        print(f"  python3 {Path(__file__).parent / 'run.py'} run")


# ── Internals ────────────────────────────────────────────────────────────────

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
    label = "com.notum.clusterflock.mac"
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
    <key>StandardOutPath</key><string>/tmp/clusterflock-mac.log</string>
    <key>StandardErrorPath</key><string>/tmp/clusterflock-mac.log</string>
</dict></plist>""")
    subprocess.run(["launchctl", "load", str(plist)])
    print(f"  ✓ launchd: {plist}")
