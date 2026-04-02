"""First-time setup: detect LM Studio, profile hardware, pick model, register."""

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

CONFIG = Path(__file__).parent / "cluster.json"


def run_setup():
    print("=== ClusterFlock nNode Setup ===\n")

    # 1. LM Studio CLI
    _ensure_lmstudio()

    # 1b. Ensure memlock limit is sufficient (LM Studio evicts models otherwise)
    if platform.system() == "Linux":
        _check_memlock()

    # 2. Ensure server running, unload all models for clean VRAM profiling
    from studio import ensure_server, lms_ps, unload_all
    from hardware import snapshot

    ensure_server()  # also disables LM Link
    stale = lms_ps()
    if stale:
        names = [m.get("identifier") or m.get("modelKey") or "?" for m in stale]
        print(f"Unloading {len(stale)} model(s) for clean profiling: {', '.join(names)}")
        unload_all()
        time.sleep(2)  # let VRAM settle
        print("✓ Models unloaded")
    else:
        print("✓ No models loaded — VRAM is clean")

    # Load existing config for reuse (node_id, tight_pack, etc.)
    existing = {}
    if CONFIG.exists():
        try:
            existing = json.loads(CONFIG.read_text()) or {}
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Hardware profile (all models unloaded = accurate free VRAM)
    hw = snapshot()
    _print_hw(hw)
    gpus = hw["gpu"]

    # 4. Model selection skipped — models are managed via nCore commands
    tight_pack = existing.get("tight_pack", False)
    models = []
    benchmarks = []
    print("\n✓ Skipping model selection — models are loaded on-demand via nCore")

    # 4. Connection mode
    print("\nConnection mode:")
    print("  1) Pull — agent connects out to nCore (use when nCore address is known)")
    print("  2) Push — agent listens, nCore connects in (use behind NAT / no route to nCore)")
    mode_choice = input("Choose [1]: ").strip()
    use_push = mode_choice == "2"

    # Stable node_id: reuse existing or generate once
    node_id = existing.get("node_id") or uuid.uuid4().hex[:12]

    config = {
        "node_id": node_id,
        "hostname": platform.node(),
        "hardware": hw,
        "models": models,
        "benchmarks": benchmarks,
    }

    if use_push:
        port = input("\nListen port [1903]: ").strip() or "1903"
        config["mode"] = "push"
        config["listen_port"] = int(port)
        print(f"  → Push mode on port {port} — nCore will pair with this node.")
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
    print(f"Saved: {CONFIG}")

    # 5. Service install or run inline
    if input("\nInstall as system service? [y/N] ").strip().lower() == "y":
        _install_service()
        print("\n✓ Setup complete.")
    else:
        print("\n✓ Setup complete. Starting agent...\n")
        from run import run_agent
        run_agent(config)


# ── Internals ────────────────────────────────────────────────────────────────

def _check_memlock():
    """Ensure the memlock ulimit is unlimited.

    LM Studio's llama.cpp backend calls mlock() to pin model weights in RAM.
    If RLIMIT_MEMLOCK is too low the load reports success but the model is
    silently evicted seconds later ("phantom load").  This is critical on
    memory-constrained devices like Jetson Orin.
    """
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    UNLIMITED = resource.RLIM_INFINITY
    if soft == UNLIMITED:
        print("\u2713 memlock unlimited")
        return

    soft_mb = soft // (1024 * 1024)
    print(f"\u2717 memlock limit is {soft_mb} MB — models may silently unload")

    limits_file = Path("/etc/security/limits.d/99-memlock.conf")
    user = os.environ.get("USER", "notum")
    line = f"{user} - memlock unlimited\n"

    print(f"  Setting memlock to unlimited via {limits_file} (requires sudo)")
    try:
        subprocess.run(
            ["sudo", "tee", str(limits_file)],
            input=line.encode(), capture_output=True, check=True,
        )
        print(f"  \u2713 Written {limits_file}")
        print("  NOTE: You must log out and back in (or reboot) for the new")
        print("        limit to take effect, then restart the agent.")
    except subprocess.CalledProcessError:
        print(f"  Could not write {limits_file} — set memlock manually:")
        print(f"    echo '{user} - memlock unlimited' | sudo tee {limits_file}")


def _ensure_lmstudio():
    from studio import lms_installed
    if lms_installed():
        print("✓ LM Studio CLI found")
        return

    print("✗ LM Studio CLI (lms) not found.")
    print("  Install LM Studio from https://lmstudio.ai")
    print("  Then open the app once to bootstrap the CLI.\n")

    if platform.system() == "Darwin" and shutil.which("brew"):
        if input("  Attempt install via Homebrew? [Y/n] ").strip().lower() != "n":
            print("  Running: brew install --cask lm-studio")
            r = subprocess.run(["brew", "install", "--cask", "lm-studio"],
                               capture_output=True, text=True)
            if r.returncode == 0 and lms_installed():
                print("  ✓ Installed")
                return
            print("  CLI still not found. Open LM Studio.app, then re-run setup.")

    sys.exit(1)


def _print_hw(hw):
    s = hw["system"]
    print(f"\n  Host:  {s['hostname']} ({s['os']}/{s['arch']})")
    print(f"  CPU:   {s['cpu_count']} cores @ {s['cpu_pct']}%")
    print(f"  RAM:   {s['ram_free_mb']:,}/{s['ram_total_mb']:,} MB free")
    print(f"  Disk:  {s['disk_free_gb']} GB free")
    for g in hw["gpu"]:
        tag = " (unified)" if g.get("unified") else ""
        vfree = g.get("vram_free_mb", 0)
        vtotal = g.get("vram_total_mb", 0)
        print(f"  GPU:   {g['name']}{tag} — {vfree:,}/{vtotal:,} MB VRAM")


def _register(address, config):
    try:
        body = json.dumps(config).encode()
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

    if sysname == "Darwin":
        _launchd(py, agent_dir)
    elif sysname == "Linux":
        _systemd(py, agent_dir)
    elif sysname == "Windows":
        _win_task(py, agent_dir)
    else:
        print(f"  Unsupported OS: {sysname}")


def _launchd(py, agent_dir):
    label = "com.notum.clusterflock.nnode"
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
    <key>StandardOutPath</key><string>/tmp/clusterflock-nnode.log</string>
    <key>StandardErrorPath</key><string>/tmp/clusterflock-nnode.log</string>
</dict></plist>""")
    subprocess.run(["launchctl", "load", str(plist)])
    print(f"  ✓ launchd: {plist}")


def _systemd(py, agent_dir):
    unit = f"""[Unit]
Description=ClusterFlock nNode Agent
After=network.target

[Service]
Type=simple
WorkingDirectory={agent_dir}
Environment=PATH=/home/notum/.lmstudio/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={py} {agent_dir / "run.py"} run
Restart=always
RestartSec=5
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target"""
    path = "/etc/systemd/system/clusterflock-nnode.service"
    print(f"  Writing {path} (requires sudo)")
    subprocess.run(["sudo", "tee", path], input=unit.encode(), capture_output=True)
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    subprocess.run(["sudo", "systemctl", "enable", "--now", "clusterflock-nnode"])
    print("  ✓ systemd service enabled")


def _win_task(py, agent_dir):
    name = "ClusterFlock-nNode"
    cmd = f'"{py}" "{agent_dir / "run.py"}" run'
    subprocess.run(["schtasks", "/create", "/tn", name,
                     "/tr", cmd, "/sc", "onlogon", "/rl", "highest", "/f"])
