"""Command dispatcher for Linux agent.

Manages multiple llama-server instances — one per device (GPU or CPU).
Handles load/unload/prompt/configure commands from nCore orchestrator.

IMPORTANT: No model splitting. Ever. Each model runs entirely on one device.
"""

import json
import os
import re
import time
from pathlib import Path

from server import (start_server, stop_server, server_running, complete,
                    benchmark, loaded_models, active_devices, _port_for_device)
from models_hf import (local_models, download_model,
                        get_bench, save_bench, MODELS_DIR,
                        auto_select_quant, _resolve_gguf_repo,
                        download_progress)

_MODEL_RE = re.compile(r'^[\w./@:\-]+$')
_CONFIG = Path(__file__).parent / "cluster.json"

# Loaded models per device: device_id → {"model_id", "model_path", "port"}
_devices = {}

# Activity state for heartbeat reporting
_activity = {"state": "idle", "model": None, "detail": None, "started_at": None}


def get_activity():
    """Return current activity state for heartbeat reporting."""
    a = dict(_activity)
    if a["state"] == "downloading":
        prog = download_progress()
        if prog:
            a["detail"] = prog
    return a


def _set_activity(state, model=None):
    _activity["state"] = state
    _activity["model"] = model
    _activity["detail"] = None
    _activity["started_at"] = time.time() if state != "idle" else None


# ── LINUX-ONLY: CPU/RAM inference setting ──────────────────────────────
# When enabled, the agent advertises system RAM as an additional device
# capable of running models entirely in CPU + RAM (no GPU).
# This is controlled remotely from the nCore UI toggle and persisted
# in cluster.json. Only available on Linux.
_cpu_ram_enabled = False


def _read_config():
    try:
        return json.loads(_CONFIG.read_text())
    except Exception:
        return {}


def _save_config(updates):
    """Merge updates into cluster.json."""
    cfg = _read_config()
    cfg.update(updates)
    _CONFIG.write_text(json.dumps(cfg, indent=2))


def cpu_ram_enabled():
    """Whether CPU/RAM device is enabled (Linux-only feature)."""
    return _cpu_ram_enabled


def current_model(device=None):
    """Return model ID for a device, or first loaded model."""
    if device:
        return _devices.get(device, {}).get("model_id")
    for info in _devices.values():
        if info.get("model_id"):
            return info["model_id"]
    return None


def all_loaded_models():
    """Return list of (device_id, model_id, port) for every loaded model."""
    return [(d, info["model_id"], info["port"])
            for d, info in _devices.items()
            if info.get("model_id")]


def execute(cmd):
    """Dispatch a command dict. Returns result dict or None."""
    action = cmd.get("action")

    if action == "unload_all":
        _unload_all()

    elif action == "unload":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        _unload(mid)

    elif action == "load":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        device = _resolve_device(cmd)
        _set_activity("loading", mid)
        try:
            _load_model(mid, device=device, context_length=cmd.get("context_length"))
        finally:
            _set_activity("idle")
        _auto_bench(mid, device)

    elif action == "download_and_load":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        device = _resolve_device(cmd)
        _set_activity("downloading", mid)
        try:
            _download_and_load(mid, device=device,
                               context_length=cmd.get("context_length"))
        finally:
            _set_activity("idle")
        loaded_mid = _devices.get(device, {}).get("model_id", mid)
        _auto_bench(loaded_mid, device)
        return {"ok": True}

    elif action == "benchmark":
        device, port = _find_model_device(cmd.get("model"))
        if not device:
            raise ValueError("no model loaded to benchmark")
        mid = _devices.get(device, {}).get("model_id", "")
        _set_activity("benchmarking", mid)
        try:
            perf = benchmark(port=port)
        finally:
            _set_activity("idle")
        if mid:
            save_bench(mid, perf, device=device)
        return {"model_id": mid, **perf}

    elif action == "prompt":
        messages = cmd.get("messages")
        if not messages:
            raise ValueError("prompt requires 'messages'")
        device, port = _find_model_device(cmd.get("model"))
        if not device:
            raise ValueError("no model loaded for prompt")
        kwargs = {}
        if cmd.get("temperature") is not None:
            kwargs["temperature"] = float(cmd["temperature"])
        if cmd.get("top_p") is not None:
            kwargs["top_p"] = float(cmd["top_p"])
        if cmd.get("frequency_penalty") is not None:
            kwargs["frequency_penalty"] = float(cmd["frequency_penalty"])
        if cmd.get("presence_penalty") is not None:
            kwargs["presence_penalty"] = float(cmd["presence_penalty"])
        if cmd.get("stop") is not None:
            kwargs["stop"] = cmd["stop"]
        _set_activity("generating", cmd.get("model"))
        try:
            return complete(messages, max_tokens=cmd.get("max_tokens", -1),
                            port=port,
                            generation_timeout=cmd.get("generation_timeout", 300),
                            **kwargs)
        finally:
            _set_activity("idle")

    elif action == "delete_model":
        mid = cmd.get("model_id", "")
        if not mid:
            raise ValueError("model_id required")
        return _delete_model(mid)

    # ── LINUX-ONLY: remote configuration from nCore ──
    elif action == "configure":
        _handle_configure(cmd)

    else:
        raise ValueError(f"unknown action: {action}")
    return None


def _delete_model(model_id):
    """Delete a downloaded model from disk."""
    # Unload if currently loaded
    for dev, info in list(_devices.items()):
        if info.get("model_id") and model_id in info["model_id"]:
            _unload(info["model_id"])
            break
    # Find model files
    candidates = []
    for f in MODELS_DIR.rglob("*.gguf"):
        rel = str(f.relative_to(MODELS_DIR)).replace(os.sep, "/")
        if model_id == rel or model_id in rel or any(
            part in rel.lower() for part in model_id.lower().split("/") if len(part) > 3
        ):
            candidates.append(f)
    if not candidates:
        raise FileNotFoundError(f"Model not found: {model_id}")
    freed = 0
    deleted = []
    for f in candidates:
        sz = f.stat().st_size
        f.unlink()
        freed += sz
        deleted.append(str(f.relative_to(MODELS_DIR)))
        print(f"[delete] Removed {f.relative_to(MODELS_DIR)} ({sz/(1024**3):.1f} GB)")
    # Clean up empty parent dirs
    for f in candidates:
        d = f.parent
        while d != MODELS_DIR:
            try:
                if not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
                else:
                    break
            except Exception:
                break
    return {"ok": True, "deleted": deleted, "freed_gb": round(freed / (1024**3), 2)}


# ── Device resolution ────────────────────────────────────────────────────

def _resolve_device(cmd):
    """Determine target device from a command.

    - device="cpu" or gpu_idx="cpu" → "cpu"
    - gpu_idx=N → "gpuN"
    - No hint → first available GPU slot, or "cpu" if all GPUs occupied
    """
    if cmd.get("device") == "cpu":
        return "cpu"
    gpu_idx = cmd.get("gpu_idx")
    if gpu_idx is not None:
        if str(gpu_idx) == "cpu":
            return "cpu"
        return f"gpu{gpu_idx}"
    # Default: first GPU not currently loaded
    from hardware import gpu
    gpus = gpu()
    for i in range(len(gpus)):
        dev = f"gpu{i}"
        if dev not in _devices:
            return dev
    # All GPUs occupied; use gpu0 (will unload existing)
    return "gpu0"


def _find_model_device(model_hint=None):
    """Find which device has a specific model loaded.

    Returns (device_id, port) or (None, None).
    """
    if model_hint:
        for dev, info in _devices.items():
            mid = info.get("model_id", "")
            if mid and (model_hint == mid or model_hint in mid):
                return dev, info["port"]
    # No hint or no match — first loaded device
    for dev, info in _devices.items():
        if info.get("model_id"):
            return dev, info["port"]
    return None, None


# ── Load / Unload ────────────────────────────────────────────────────────

def _load_model(model_id, *, device="gpu0", context_length=None,
                model_path=None):
    """Load a model onto a specific device. No model splitting — ever."""
    if not model_path:
        model_path = _resolve_model_path(model_id)
    if not model_path:
        raise FileNotFoundError(f"Model not found: {model_id}. Download it first.")

    ctx = context_length or 131072
    tag = "CPU/RAM" if device == "cpu" else device.upper()
    print(f"[load] Loading {model_id} on {tag} (ctx={ctx})...")

    start_server(model_path, device=device, ctx_size=ctx)

    port = _port_for_device(device)
    _devices[device] = {
        "model_id": model_id,
        "model_path": model_path,
        "port": port,
    }
    print(f"[load] ✓ {model_id} ready on {tag}")


def _unload(model_id):
    """Unload a specific model (finds its device automatically)."""
    for dev, info in list(_devices.items()):
        if info.get("model_id") == model_id:
            tag = "CPU/RAM" if dev == "cpu" else dev.upper()
            print(f"[unload] Stopping {model_id} on {tag}...")
            stop_server(dev)
            del _devices[dev]
            return
    # Partial match
    for dev, info in list(_devices.items()):
        if info.get("model_id") and model_id in info["model_id"]:
            stop_server(dev)
            del _devices[dev]
            return
    print(f"[unload] {model_id} not found on any device")


def _unload_all():
    """Unload all models on all devices."""
    count = len(_devices)
    print(f"[unload] Stopping all servers ({count} device(s))...")
    stop_server()  # stops all
    _devices.clear()


def _download_and_load(model_id, *, device="gpu0", context_length=None):
    """Download from HuggingFace and load onto a device."""
    parts = model_id.split("/")
    if len(parts) >= 2:
        hf_repo = "/".join(parts[:2])
        quant = parts[2] if len(parts) > 2 else "q4_k_m"
    else:
        hf_repo = model_id
        quant = "q4_k_m"

    if quant == "auto":
        from hardware import snapshot
        hw = snapshot()
        if device == "cpu":
            vram_free = hw.get("system", {}).get("ram_free_mb", 0)
        else:
            gpus = hw.get("gpu", [])
            idx = int(device.replace("gpu", ""))
            vram_free = gpus[idx].get("vram_free_mb", 0) if idx < len(gpus) else 0
        gguf_repo = _resolve_gguf_repo(hf_repo)
        quant = auto_select_quant(gguf_repo, vram_free)

    path = download_model(hf_repo, quant=quant)
    _set_activity("loading", model_id)
    _load_model(model_id, device=device, context_length=context_length,
                model_path=path)


# ── LINUX-ONLY: Remote configuration ────────────────────────────────────

def _handle_configure(cmd):
    """Handle configuration commands from nCore.

    Supported settings:
      cpu_ram_enabled (bool) — enable/disable CPU/RAM as inference device.
                               Linux-only feature. When enabled, system RAM
                               appears as a schedulable device in nCore.
    """
    global _cpu_ram_enabled

    if "cpu_ram_enabled" in cmd:
        new_val = bool(cmd["cpu_ram_enabled"])
        old_val = _cpu_ram_enabled
        _cpu_ram_enabled = new_val
        _save_config({"cpu_ram_enabled": new_val})

        if new_val and not old_val:
            print("[configure] ✓ CPU/RAM device ENABLED")
        elif not new_val and old_val:
            print("[configure] CPU/RAM device DISABLED")
            if "cpu" in _devices:
                stop_server("cpu")
                del _devices["cpu"]
                print("[configure]   Stopped CPU server")

    print(f"[configure] cpu_ram_enabled={_cpu_ram_enabled}")


def init_settings():
    """Load saved settings from cluster.json on startup."""
    global _cpu_ram_enabled
    cfg = _read_config()
    _cpu_ram_enabled = cfg.get("cpu_ram_enabled", False)
    if _cpu_ram_enabled:
        print(f"[config] CPU/RAM device enabled (from saved config)")


# ── Auto-benchmark ───────────────────────────────────────────────────────

def _auto_bench(model_id, device):
    """Benchmark a model if no cached result exists for this device."""
    if not model_id or get_bench(model_id, device=device) != 0:
        return
    _set_activity("benchmarking", model_id)
    try:
        port = _port_for_device(device)
        perf = benchmark(port=port)
        save_bench(model_id, perf, device=device)
        print(f"  Benchmark: {perf['tokens_per_sec']} tok/s")
    except Exception as e:
        print(f"  Benchmark failed: {e}")
    finally:
        _set_activity("idle")


# ── Model path resolution ───────────────────────────────────────────────

def _resolve_model_path(model_id):
    """Find the GGUF file for a model ID."""
    if model_id.endswith(".gguf") and Path(model_id).exists():
        return model_id

    for m in local_models():
        if model_id in m["id"] or model_id.replace("/", "_") in m["id"]:
            return m["path"]

    matches = sorted(
        (f for f in MODELS_DIR.rglob("*.gguf")
         if any(part in str(f).lower() for part in model_id.lower().split("/"))),
        key=lambda f: f.name
    )
    for f in matches:
        if "00001-of-" in f.name:
            return str(f)
    if matches:
        return str(matches[0])
    return None



