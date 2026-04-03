"""First-time setup — unified for all platforms.

Profiles hardware, checks/builds llama.cpp, registers with nCore.
Auto-detects platform to configure correct build flags and service type.
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

_IS_DARWIN = platform.system() == "Darwin"
CONFIG = Path(__file__).parent / "cluster.json"


def run_setup():
    print("=== ClusterFlock Agent Setup ===\n")

    from hardware import detect_platform
    plat = detect_platform()

    # 1. Clean up competing inference servers
    from gpu_cleanup import cleanup_gpu
    cleanup_gpu()

    # 2. Check / build llama.cpp
    from server import is_built, server_binary, build, LLAMA_CPP_DIR, PREBUILT_DIR

    if not _IS_DARWIN:
        from server import _detect_cuda_version
        cuda_ver = _detect_cuda_version()
        if cuda_ver:
            print(f"✓ CUDA {cuda_ver} driver detected")
        else:
            print("⚠ No NVIDIA driver found — GPU inference unavailable")

    if is_built():
        binary = server_binary()
        print(f"✓ llama-server: {binary}")
    elif LLAMA_CPP_DIR.exists():
        print("Building llama.cpp...")
        build()
    else:
        if _IS_DARWIN:
            prebuilt = PREBUILT_DIR / "llama-server"
            if prebuilt.exists():
                print("✓ llama-server prebuilt binary found")
            else:
                print("✗ llama-server binary not found.")
                print(f"  Expected prebuilt at: {PREBUILT_DIR}")
                print(f"  Or source at: {LLAMA_CPP_DIR}")
                print("  To build from source:")
                print("    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git llama_cpp")
                sys.exit(1)
        else:
            print("✗ llama-server not found")
            print("  No prebuilt binaries and no source tree.")
            print("  Options:")
            print("    1. Run build.sh on a build machine, copy build/ here")
            print("    2. git clone --depth 1 https://github.com/ggerganov/llama.cpp.git llama_cpp")
            print("       python3 run.py build")
            sys.exit(1)

    # 3. Check memlock (Linux only)
    if not _IS_DARWIN:
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

    # Load existing config
    existing = {}
    if CONFIG.exists():
        try:
            existing = json.loads(CONFIG.read_text()) or {}
        except (json.JSONDecodeError, ValueError):
            pass

    # 5. Hardware profile
    from hardware import snapshot, is_apple_silicon, is_dgx_spark
    hw = snapshot()
    _print_hw(hw)

    if plat == "mac":
        print("\n  ✓ Apple Silicon detected — Metal GPU acceleration enabled")
    elif plat == "spark":
        print("\n  ✓ DGX Spark detected — NVFP4 + FlashAttention optimizations enabled")
    else:
        n_gpus = len(hw.get("gpu", []))
        print(f"\n  {n_gpus} NVIDIA GPU(s) available for inference")

    # 6. Models managed via nCore
    tight_pack = existing.get("tight_pack", False)
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
        "agent_type": plat,
        "hardware": hw,
        "models": [],
        "benchmarks": [],
        "tight_pack": tight_pack,
        "cpu_ram_enabled": existing.get("cpu_ram_enabled", False),
    }

    if use_push:
        port = input("\nListen port [1903]: ").strip() or "1903"
        config["mode"] = "push"
        config["listen_port"] = int(port)
        print(f"  → Push mode on port {port}")
    else:
        address = (input("\nnCore address [http://localhost:1903]: ").strip()
                   or "http://localhost:1903")
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
    print(f"⚠ memlock limit is {soft_mb} MB — models may fail to lock memory")
    limits_file = Path("/etc/security/limits.d/99-memlock.conf")
    user = os.environ.get("USER", "notum")
    line = f"{user} - memlock unlimited\n"
    print(f"  Setting memlock unlimited via {limits_file} (requires sudo)")
    try:
        subprocess.run(["sudo", "tee", str(limits_file)],
                       input=line.encode(), capture_output=True, check=True)
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
    for i, g in enumerate(hw["gpu"]):
        tag = " (unified)" if g.get("unified") else ""
        vfree = g.get("vram_free_mb", 0)
        vtotal = g.get("vram_total_mb", 0)
        print(f"  GPU{i}: {g['name']}{tag} — {vfree:,}/{vtotal:,} MB VRAM")


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

    if _IS_DARWIN:
        _launchd(py, agent_dir)
    else:
        _systemd(py, agent_dir)


def _systemd(py, agent_dir):
    port = 1903
    try:
        cfg = json.loads((agent_dir / "cluster.json").read_text())
        port = cfg.get("listen_port", 1903)
    except Exception:
        pass
    unit = f"""[Unit]
Description=ClusterFlock Agent
After=network.target

[Service]
Type=simple
WorkingDirectory={agent_dir}
ExecStart={py} -u {agent_dir / "watchdog.py"} --port {port}
Restart=always
RestartSec=5
LimitMEMLOCK=infinity
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target"""
    path = "/etc/systemd/system/clusterflock-agent.service"
    print(f"  Writing {path} (requires sudo)")
    subprocess.run(["sudo", "tee", path], input=unit.encode(),
                   capture_output=True)
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    subprocess.run(["sudo", "systemctl", "enable", "--now",
                    "clusterflock-agent"])
    print("  ✓ systemd service enabled")


def _launchd(py, agent_dir):
    label = "com.notum.clusterflock.agent"
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
        <string>-u</string>
        <string>{agent_dir / "watchdog.py"}</string>
    </array>
    <key>WorkingDirectory</key><string>{agent_dir}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/clusterflock-agent.log</string>
    <key>StandardErrorPath</key><string>/tmp/clusterflock-agent.log</string>
</dict></plist>""")
    subprocess.run(["launchctl", "load", str(plist)])
    print(f"  ✓ launchd: {plist}")
