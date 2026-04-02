#!/usr/bin/env python3
"""Non-interactive deployment: profile, pick models, download, load, register, install service."""

import json
import platform
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

CONFIG = Path(__file__).parent / "cluster.json"
NCORE_ADDRESS = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:1903"


def main():
    print("=== ClusterFlock nNode Deploy (non-interactive) ===\n")
    print(f"  nCore: {NCORE_ADDRESS}")

    # 1. Verify LM Studio
    from studio import lms_installed, ensure_server, unload_all, fetch_catalog
    from studio import pick_best_models, download_and_identify, load_model, benchmark
    if not lms_installed():
        print("ERROR: lms CLI not found. Install LM Studio first.")
        sys.exit(1)
    print("  ✓ LM Studio CLI found")

    # 2. Start server + unload
    ensure_server()
    print("\n  Unloading all models for clean profiling...")
    unload_all()
    time.sleep(2)

    # 3. Hardware profile
    from hardware import snapshot
    hw = snapshot()
    s = hw["system"]
    print(f"\n  Host:  {s['hostname']} ({s['os']}/{s['arch']})")
    print(f"  CPU:   {s['cpu_count']} cores")
    print(f"  RAM:   {s['ram_free_mb']:,}/{s['ram_total_mb']:,} MB free")
    print(f"  Disk:  {s['disk_free_gb']} GB free")
    for g in hw["gpu"]:
        tag = " (unified)" if g.get("unified") else ""
        print(f"  GPU:   {g['name']}{tag} — {g.get('vram_free_mb',0):,}/{g.get('vram_total_mb',0):,} MB VRAM")

    # 4. Model selection
    catalog = fetch_catalog(include_hf=False)
    assignments = pick_best_models(hw["gpu"], catalog) if catalog else []
    models = []
    benchmarks_list = []

    if assignments:
        print(f"\n  Deploying {len(assignments)} model(s)...")
        for gpu_info, model_entry in assignments:
            model_id = model_entry["id"]
            print(f"\n  → {gpu_info['name']}: {model_entry['name']}")
            try:
                if not model_entry.get("downloaded"):
                    model_id = download_and_identify(model_entry["id"])
                    if not model_id:
                        print(f"    Download failed for {model_entry['id']}")
                        continue
                load_model(model_id)
                perf = benchmark(model_id)
                print(f"    {perf['tokens_per_sec']} tok/s "
                      f"({perf['completion_tokens']} tokens in {perf['elapsed_sec']}s)")
                models.append(model_id)
                benchmarks_list.append({"model": model_id, "gpu": gpu_info["name"], **perf})
            except Exception as e:
                print(f"    Model setup failed: {e}")
    else:
        print("\n  No suitable models found.")

    # 5. Config
    existing = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    node_id = existing.get("node_id") or uuid.uuid4().hex[:12]

    config = {
        "address": NCORE_ADDRESS,
        "node_id": node_id,
        "hostname": platform.node(),
        "hardware": hw,
        "models": models,
        "benchmarks": benchmarks_list,
    }

    # 6. Register with nCore
    print(f"\n  Registering with {NCORE_ADDRESS}...")
    reg = _register(NCORE_ADDRESS, config)
    if reg:
        config["node_id"] = reg.get("node_id", "") or config["node_id"]
        if reg.get("token"):
            config["token"] = reg["token"]
            config["mode"] = "pull"
            print(f"  ✓ Registered as {config['node_id']}")
        elif reg.get("status") == "pending":
            print(f"  ⏳ Pending approval on nCore (node: {config['node_id']})")
            token = _await_approval(NCORE_ADDRESS, config)
            if token:
                config["token"] = token
                config["mode"] = "pull"
                print(f"  ✓ Approved! Registered as {config['node_id']}")
            else:
                print("  ✗ Rejected or timed out.")
        else:
            print(f"  ✓ Registered as {config['node_id']}")
    else:
        print("  ✗ Could not reach nCore. Config saved anyway.")

    CONFIG.write_text(json.dumps(config, indent=2))
    print(f"\n  Saved: {CONFIG}")

    # 7. Install systemd service
    _install_systemd()

    print("\n=== Deploy complete ===")


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


def _await_approval(address, config, timeout=300):
    """Poll for admin approval, up to timeout seconds."""
    body = json.dumps(config).encode()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        try:
            req = urllib.request.Request(
                f"{address}/api/v1/register", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data.get("token"):
                    return data["token"]
        except urllib.error.HTTPError as e:
            if e.code == 202:
                continue
            if e.code == 403:
                return None
        except Exception:
            continue
    return None


def _install_systemd():
    agent_dir = Path(__file__).parent.resolve()
    py = sys.executable
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

[Install]
WantedBy=multi-user.target"""
    path = "/etc/systemd/system/clusterflock-nnode.service"
    print(f"\n  Installing systemd service: {path}")
    r = subprocess.run(["sudo", "tee", path], input=unit.encode(), capture_output=True)
    if r.returncode != 0:
        print(f"  ✗ Failed to write service file: {r.stderr.decode()}")
        return
    subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
    subprocess.run(["sudo", "systemctl", "enable", "clusterflock-nnode"], capture_output=True)
    print("  ✓ systemd service installed and enabled")
    print("  → Start with: sudo systemctl start clusterflock-nnode")


if __name__ == "__main__":
    main()
