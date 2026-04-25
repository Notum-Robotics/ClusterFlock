"""Command dispatcher — unified for all platforms.

Manages llama-server instances across devices (GPUs, CPU/RAM).
Handles load/unload/prompt/configure commands from nCore orchestrator.

Multi-device model (from agent_linux architecture):
  Each GPU gets its own server. On macOS (unified memory), a single
  "gpu0" device covers the entire SoC. CPU/RAM is an optional extra
  device on Linux for system-RAM-only inference.

IMPORTANT: No model splitting. Ever. Each model runs entirely on one device.
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

from server import (start_server, stop_server, server_running, complete,
                    benchmark, loaded_models, active_devices, _port_for_device,
                    get_server_context, inference_liveness_check)
from models_hf import (local_models, download_model,
                        get_bench, save_bench, MODELS_DIR,
                        auto_select_quant, _resolve_gguf_repo,
                        download_progress)

_MODEL_RE = re.compile(r'^[\w./@:\-]+$')
_SHARDED_GGUF_RE = re.compile(r'^(?P<base>.+\.gguf)-\d{5}-of-\d{5}\.gguf$')
_CONFIG = Path(__file__).parent / "cluster.json"

# Loaded models per device: device_id → {"model_id", "model_path", "port"}
_devices = {}

# Activity state for heartbeat reporting
_activity = {"state": "idle", "model": None, "detail": None, "started_at": None}

# CPU/RAM inference — controlled from nCore UI, persisted in cluster.json
_cpu_ram_enabled = False

# Aggressive VRAM — reduces safeguards, fills up to ~99% VRAM with context
_aggressive_vram_enabled = False

# RAM offload — ignores VRAM budget, loads max native context, KV spills to RAM
_ram_offload_enabled = False

# Speculative decoding — draft model path per device, persisted in cluster.json
_spec_decode = {}  # device → draft_model_path

# Auto-unload: unload models after idle timeout, keep primed
_auto_unload_enabled = False
_AUTO_UNLOAD_SEC = 15 * 60  # 15 minutes
_last_command_time = 0.0     # updated on every execute()
_primed = {}                 # device → {model_id, model_path}

# Per-device lock — prevents recovery loop from racing with job threads.
# Job threads acquire before load/download_and_load.
# check_crashed_servers() does a non-blocking acquire; if the device is
# already locked (job in progress), recovery is skipped for that cycle.
_device_locks: dict = {}
_device_locks_mutex = threading.Lock()


def _get_device_lock(device_id: str) -> threading.Lock:
    """Return (creating if needed) the per-device operation lock."""
    with _device_locks_mutex:
        if device_id not in _device_locks:
            _device_locks[device_id] = threading.Lock()
        return _device_locks[device_id]


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
    """Whether CPU/RAM device is enabled."""
    return _cpu_ram_enabled


def current_model(device=None):
    """Return model ID for a device, or first loaded model."""
    if device:
        return _devices.get(device, {}).get("model_id")
    # Fallback: detect externally-started server
    if not _devices:
        _detect_running_model()
    for info in _devices.values():
        if info.get("model_id"):
            return info["model_id"]
    return None


def all_loaded_models():
    """Return list of (device_id, model_id, port) for every loaded model."""
    return [(d, info["model_id"], info["port"])
            for d, info in _devices.items()
            if info.get("model_id")]


# ── Command dispatch ─────────────────────────────────────────────────────

def execute(cmd):
    """Dispatch a command dict. Returns result dict or None."""
    global _last_command_time
    _last_command_time = time.time()
    action = cmd.get("action")

    if action == "unload_all":
        _primed.clear()
        _unload_all()

    elif action == "unload":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        if not _unload(mid):
            raise ValueError(f"model not loaded: {mid}")

    elif action == "load":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        device = _resolve_device(cmd)
        _set_activity("loading", mid)
        try:
            with _get_device_lock(device):
                _load_model(mid, device=device,
                            context_length=cmd.get("context_length"))
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
            with _get_device_lock(device):
                _download_and_load(mid, device=device,
                                   context_length=cmd.get("context_length"),
                                   filename=cmd.get("filename"))
        finally:
            _set_activity("idle")
        loaded_mid = _devices.get(device, {}).get("model_id", mid)
        _auto_bench(loaded_mid, device)
        return {"ok": True}

    elif action == "benchmark":
        device, port = _find_model_device(cmd.get("model"))
        if not device:
            device, port = _wake_primed(cmd.get("model"))
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
            # Check if model is primed (sleeping) — wake it
            device, port = _wake_primed(cmd.get("model"))
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
                            generation_timeout=cmd.get("generation_timeout", 1800),
                            **kwargs)
        finally:
            _set_activity("idle")

    elif action == "delete_model":
        mid = cmd.get("model_id", "")
        if not mid:
            raise ValueError("model_id required")
        return _delete_model(mid)

    elif action == "configure":
        _handle_configure(cmd)

    else:
        raise ValueError(f"unknown action: {action}")
    return None


# ── Device resolution ────────────────────────────────────────────────────

def _resolve_device(cmd):
    """Determine target device from a command.

    - device="cpu" or gpu_idx="cpu" → "cpu"
    - gpu_idx=N → "gpuN" (validated against physical GPUs)
    - No hint → first available GPU slot, or "gpu0" if all occupied
    """
    if cmd.get("device") == "cpu":
        return "cpu"
    gpu_idx = cmd.get("gpu_idx")
    if gpu_idx is not None:
        if str(gpu_idx) == "cpu":
            return "cpu"
        # Validate numeric gpu_idx against physical GPU count
        try:
            idx = int(gpu_idx)
        except (ValueError, TypeError):
            raise ValueError(f"invalid gpu_idx: {gpu_idx}")
        from hardware import gpu
        n_physical = len(gpu())
        if idx >= n_physical:
            if cpu_ram_enabled():
                log.info(f"[device] gpu_idx={idx} beyond {n_physical} "
                         f"physical GPU(s) → remapping to CPU")
                return "cpu"
            raise ValueError(f"gpu_idx={idx} but only {n_physical} "
                             f"physical GPU(s) available")
        return f"gpu{gpu_idx}"
    # Default: first GPU not currently loaded
    from hardware import gpu
    gpus = gpu()
    for i in range(max(len(gpus), 1)):
        dev = f"gpu{i}"
        if dev not in _devices:
            return dev
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
    for dev, info in _devices.items():
        if info.get("model_id"):
            return dev, info["port"]
    return None, None


def _canonical_model_id(model_id):
    """Collapse sharded GGUF IDs to their logical base ID.

    Example:
      foo.gguf-00001-of-00002.gguf -> foo.gguf
    """
    if not model_id:
        return ""
    mid = str(model_id).replace("\\", "/")
    m = _SHARDED_GGUF_RE.match(mid)
    return m.group("base") if m else mid


def _model_id_matches(target_id, loaded_id):
    """Return True when target and loaded IDs refer to the same model."""
    if not target_id or not loaded_id:
        return False
    if target_id == loaded_id:
        return True
    if target_id in loaded_id or loaded_id in target_id:
        return True
    return _canonical_model_id(target_id) == _canonical_model_id(loaded_id)


# ── Load / Unload ────────────────────────────────────────────────────────

def _load_model(model_id, *, device="gpu0", context_length=None,
                model_path=None):
    """Load a model onto a specific device."""
    if not model_path:
        model_path = _resolve_model_path(model_id)
    if not model_path:
        raise FileNotFoundError(f"Model not found: {model_id}. Download it first.")

    tag = "CPU/RAM" if device == "cpu" else device.upper()
    kwargs = {"device": device}
    if context_length:
        kwargs["ctx_size"] = context_length

    # Apply load-mode flags (aggressive VRAM / RAM offload) unless a
    # specific context_length was requested (explicit overrides auto).
    if not context_length:
        if _aggressive_vram_enabled:
            kwargs["aggressive_vram"] = True
        if _ram_offload_enabled:
            kwargs["ram_offload"] = True

    # Apply speculative decoding if configured for this device
    draft_path = _spec_decode.get(device)
    if draft_path and os.path.isfile(draft_path):
        kwargs["draft_model_path"] = draft_path

    log.info(f"[load] Loading {model_id} on {tag}...")

    start_server(model_path, **kwargs)

    port = _port_for_device(device)
    _devices[device] = {
        "model_id": model_id,
        "model_path": model_path,
        "port": port,
    }
    _liveness_interval[device] = time.time()  # prevent immediate liveness fire
    log.info(f"[load] ✓ {model_id} ready on {tag}")


def _unload(model_id):
    """Unload a specific model (finds its device automatically)."""
    # After agent restarts, a model may already be running but _devices can be
    # empty until we detect it from llama-server.
    if not _devices:
        _detect_running_model()

    target_path = _resolve_model_path(model_id)

    for dev, info in list(_devices.items()):
        loaded_id = info.get("model_id", "")
        loaded_path = info.get("model_path", "")

        if _model_id_matches(model_id, loaded_id):
            tag = "CPU/RAM" if dev == "cpu" else dev.upper()
            log.info(f"[unload] Stopping {loaded_id} on {tag}...")
            stop_server(dev)
            del _devices[dev]
            return True

        if target_path and loaded_path:
            try:
                if Path(target_path).resolve() == Path(loaded_path).resolve():
                    tag = "CPU/RAM" if dev == "cpu" else dev.upper()
                    log.info(f"[unload] Stopping {loaded_id} on {tag}...")
                    stop_server(dev)
                    del _devices[dev]
                    return True
            except Exception:
                pass

        if target_path and loaded_path:
            tname = Path(target_path).name
            lname = Path(loaded_path).name
            if _model_id_matches(tname, lname):
                tag = "CPU/RAM" if dev == "cpu" else dev.upper()
                log.info(f"[unload] Stopping {loaded_id} on {tag}...")
                stop_server(dev)
                del _devices[dev]
                return True

    # Fallback partial match for compatibility
    for dev, info in list(_devices.items()):
        if info.get("model_id") and model_id in info["model_id"]:
            tag = "CPU/RAM" if dev == "cpu" else dev.upper()
            log.info(f"[unload] Stopping {model_id} on {tag}...")
            stop_server(dev)
            del _devices[dev]
            return True

    # Live-process fallback: if bookkeeping is stale after restart, ask the
    # running llama-server instance what model it has loaded and match that.
    target_canon = _canonical_model_id(model_id)
    target_path = target_path or _resolve_model_path(model_id)
    target_name = Path(target_path).name if target_path else ""

    for dev in active_devices():
        mids = loaded_models(device=dev)
        live_id = (mids[0].get("id", "") if mids else "")
        live_canon = _canonical_model_id(live_id)
        if _model_id_matches(model_id, live_id) or (target_name and _model_id_matches(target_name, live_id)):
            tag = "CPU/RAM" if dev == "cpu" else dev.upper()
            log.info(f"[unload] Stopping {live_id or model_id} on {tag}...")
            stop_server(dev)
            _devices.pop(dev, None)
            return True
        # Single-model pragmatic fallback when IDs are unavailable but server is alive.
        if not live_id and target_canon and len(active_devices()) == 1:
            tag = "CPU/RAM" if dev == "cpu" else dev.upper()
            log.info(f"[unload] Stopping running server on {tag} (target={model_id})")
            stop_server(dev)
            _devices.pop(dev, None)
            return True

    # Sleeping model fallback: unload should also clear auto-unloaded primed models.
    for dev, pinfo in list(_primed.items()):
        primed_id = pinfo.get("model_id", "")
        primed_path = pinfo.get("model_path", "")

        if _model_id_matches(model_id, primed_id):
            tag = "CPU/RAM" if dev == "cpu" else dev.upper()
            log.info(f"[unload] Removing sleeping model {primed_id} on {tag}...")
            del _primed[dev]
            return True

        if target_path and primed_path:
            try:
                if Path(target_path).resolve() == Path(primed_path).resolve():
                    tag = "CPU/RAM" if dev == "cpu" else dev.upper()
                    log.info(f"[unload] Removing sleeping model {primed_id} on {tag}...")
                    del _primed[dev]
                    return True
            except Exception:
                pass

        if target_path and primed_path:
            tname = Path(target_path).name
            pname = Path(primed_path).name
            if _model_id_matches(tname, pname):
                tag = "CPU/RAM" if dev == "cpu" else dev.upper()
                log.info(f"[unload] Removing sleeping model {primed_id} on {tag}...")
                del _primed[dev]
                return True

    log.info(f"[unload] {model_id} not found on any device")
    return False


def _unload_all():
    """Unload all models on all devices."""
    count = len(_devices)
    log.info(f"[unload] Stopping all servers ({count} device(s))...")
    stop_server()  # stops all
    _devices.clear()


def _download_and_load(model_id, *, device="gpu0", context_length=None,
                       filename=None):
    """Download from HuggingFace and load onto a device."""
    parts = model_id.split("/")
    if len(parts) >= 2:
        hf_repo = "/".join(parts[:2])
        quant = parts[2] if len(parts) > 2 else "q4_k_m"
    else:
        hf_repo = model_id
        quant = "q4_k_m"

    # Direct filename download (from HF blob URL) — skip quant selection
    if filename:
        path = download_model(hf_repo, quant=None, filename=filename)
        _set_activity("loading", model_id)
        _load_model(model_id, device=device, context_length=context_length,
                    model_path=path)
        return

    if quant == "auto":
        from hardware import snapshot
        hw = snapshot()
        if device == "cpu":
            vram_free = hw.get("system", {}).get("ram_free_mb", 0)
        else:
            gpus = hw.get("gpu", [])
            idx = int(device.replace("gpu", ""))
            vram_free = gpus[idx].get("vram_free_mb", 0) if idx < len(gpus) else 0
            if not vram_free:
                vram_free = hw.get("system", {}).get("ram_total_mb", 0)
        gguf_repo = _resolve_gguf_repo(hf_repo)
        quant = auto_select_quant(gguf_repo, vram_free)
        log.info(f"[dl+load] Auto-selected quant: {quant} "
                 f"(VRAM free: {vram_free/1024:.1f} GB)")

    path = download_model(hf_repo, quant=quant)
    _set_activity("loading", model_id)
    _load_model(model_id, device=device, context_length=context_length,
                model_path=path)


def _delete_model(model_id):
    """Delete a downloaded model from disk."""
    # Unload if currently loaded on any device
    for dev, info in list(_devices.items()):
        if info.get("model_id") and model_id in info["model_id"]:
            stop_server(dev)
            del _devices[dev]
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
        log.info(f"[delete] Removed {f.relative_to(MODELS_DIR)} ({sz/(1024**3):.1f} GB)")

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


def _detect_running_model():
    """Detect a model already loaded in an externally-started llama-server."""
    if _devices:
        return
    if not server_running(device="gpu0"):
        return
    models = loaded_models(device="gpu0")
    if models:
        mid = models[0].get("id", "")
        if mid:
            port = _port_for_device("gpu0")
            _devices["gpu0"] = {
                "model_id": mid, "model_path": "", "port": port,
            }
            log.info(f"[detect] Found running model: {mid}")


# ── Remote configuration ────────────────────────────────────────────────

def _handle_configure(cmd):
    """Handle configuration commands from nCore.

    Supported:
      cpu_ram_enabled (bool) — enable CPU/RAM as inference device.
      auto_unload (bool) — auto-unload models after idle timeout.
      aggressive_vram (bool) — fill up to ~99% VRAM with context (no safeguards).
      ram_offload (bool) — load max native context; KV cache spills to system RAM.
    """
    global _cpu_ram_enabled, _auto_unload_enabled
    global _aggressive_vram_enabled, _ram_offload_enabled

    if "cpu_ram_enabled" in cmd:
        new_val = bool(cmd["cpu_ram_enabled"])
        old_val = _cpu_ram_enabled
        _cpu_ram_enabled = new_val
        _save_config({"cpu_ram_enabled": new_val})

        if new_val and not old_val:
            log.info("[configure] ✓ CPU/RAM device ENABLED")
        elif not new_val and old_val:
            log.info("[configure] CPU/RAM device DISABLED")
            if "cpu" in _devices:
                stop_server("cpu")
                del _devices["cpu"]
                log.info("[configure]   Stopped CPU server")

    if "auto_unload" in cmd:
        new_val = bool(cmd["auto_unload"])
        old_val = _auto_unload_enabled
        _auto_unload_enabled = new_val
        _save_config({"auto_unload": new_val})

        if new_val and not old_val:
            log.info("[configure] ✓ Auto-unload ENABLED (15 min idle)")
        elif not new_val and old_val:
            log.info("[configure] Auto-unload DISABLED")
            # Wake any primed models back
            for dev, pinfo in list(_primed.items()):
                try:
                    _load_model(pinfo["model_id"], device=dev,
                                model_path=pinfo["model_path"])
                    log.info(f"[configure]   Woke {pinfo['model_id']} on {dev}")
                except Exception as e:
                    log.error(f"[configure]   Failed to wake {pinfo['model_id']}: {e}")
            _primed.clear()

    if "aggressive_vram" in cmd:
        new_val = bool(cmd["aggressive_vram"])
        old_val = _aggressive_vram_enabled
        _aggressive_vram_enabled = new_val
        _save_config({"aggressive_vram": new_val})
        if new_val and not old_val:
            log.info("[configure] ✓ Aggressive VRAM ENABLED (99% fill, reduced safeguards)")
        elif not new_val and old_val:
            log.info("[configure] Aggressive VRAM DISABLED")

    if "ram_offload" in cmd:
        new_val = bool(cmd["ram_offload"])
        old_val = _ram_offload_enabled
        _ram_offload_enabled = new_val
        _save_config({"ram_offload": new_val})
        if new_val and not old_val:
            log.info("[configure] ✓ RAM offload ENABLED (max context, KV spills to RAM)")
        elif not new_val and old_val:
            log.info("[configure] RAM offload DISABLED")

    if "spec_decode" in cmd:
        device = cmd.get("device", "gpu0")
        draft_path = cmd["spec_decode"]  # path string or null/False to disable
        if draft_path:
            _spec_decode[device] = draft_path
            # Persist per-device spec_decode map
            cfg = _read_config()
            sd = cfg.get("spec_decode", {})
            sd[device] = draft_path
            _save_config({"spec_decode": sd})
            log.info(f"[configure] ✓ Spec-decode ENABLED on {device}: {Path(draft_path).name}")
            # Reload the server immediately if a model is running on this device
            info = _devices.get(device)
            if info:
                log.info(f"[configure]   Reloading {info['model_id']} with draft model")
                _load_model(info["model_id"], device=device,
                            model_path=info["model_path"])
                _force_bench(info["model_id"], device)
        else:
            _spec_decode.pop(device, None)
            cfg = _read_config()
            sd = cfg.get("spec_decode", {})
            sd.pop(device, None)
            _save_config({"spec_decode": sd})
            log.info(f"[configure] Spec-decode DISABLED on {device}")
            # Reload without draft model
            info = _devices.get(device)
            if info:
                log.info(f"[configure]   Reloading {info['model_id']} without draft model")
                _load_model(info["model_id"], device=device,
                            model_path=info["model_path"])
                _force_bench(info["model_id"], device)

    settings = []
    if "cpu_ram_enabled" in cmd:
        settings.append(f"cpu_ram_enabled={_cpu_ram_enabled}")
    if "auto_unload" in cmd:
        settings.append(f"auto_unload={_auto_unload_enabled}")
    if "aggressive_vram" in cmd:
        settings.append(f"aggressive_vram={_aggressive_vram_enabled}")
    if "ram_offload" in cmd:
        settings.append(f"ram_offload={_ram_offload_enabled}")
    if settings:
        log.info(f"[configure] {', '.join(settings)}")


def spec_decode_status():
    """Return {device: draft_model_path} for all devices with spec-decode active."""
    return dict(_spec_decode)


def find_compatible_draft_models(model_path, device="gpu0"):
    """Find downloaded models that could serve as a speculative draft for model_path.

    Compatibility rules:
      - Model family must match (same base architecture/name prefix).
      - Draft must be strictly smaller than target.
      - Draft + target must fit in available VRAM.

    Returns list of {"id", "path", "size_gb"} dicts, smallest-first.
    """
    if not model_path:
        return []

    target_size_mb = os.path.getsize(model_path) / (1024 * 1024)

    # Get free VRAM for the device
    try:
        from hardware import gpu as _hw_gpu, _mem_info
        if device == "cpu":
            _, vram_free_mb = _mem_info()
        else:
            gpus = _hw_gpu()
            idx = int(device.replace("gpu", ""))
            vram_free_mb = gpus[idx].get("vram_free_mb", 0) if idx < len(gpus) else 0
    except Exception:
        vram_free_mb = 0

    # Infer model family from filename: take the first 2-3 meaningful tokens
    target_stem = Path(model_path).stem.lower()
    # Strip quant suffix patterns like -q4_k_m, -iq2_m, -q8_0, etc.
    family_name = re.sub(r'[-_](q\d|iq\d|bf16|f16|f32|fp\d|int\d|gguf).*$', '', target_stem)
    # Keep first 2 dash-separated segments as family key
    parts = [p for p in re.split(r'[-_]', family_name) if p]
    family_key = '-'.join(parts[:3]).lower() if parts else ''

    candidates = []
    for m in local_models():
        candidate_path = m["path"]
        if candidate_path == model_path:
            continue
        candidate_size_mb = os.path.getsize(candidate_path) / (1024 * 1024)
        if candidate_size_mb >= target_size_mb * 0.5:
            # Draft must be meaningfully smaller (less than 50% of target size)
            continue
        # Family match: candidate filename must share family_key prefix
        cand_stem = Path(candidate_path).stem.lower()
        cand_family = re.sub(r'[-_](q\d|iq\d|bf16|f16|f32|fp\d|int\d|gguf).*$', '', cand_stem)
        cand_parts = [p for p in re.split(r'[-_]', cand_family) if p]
        cand_key = '-'.join(cand_parts[:3]).lower() if cand_parts else ''
        if not family_key or not cand_key:
            continue
        # At least first 2 tokens must match
        fk_parts = family_key.split('-')[:2]
        ck_parts = cand_key.split('-')[:2]
        if fk_parts != ck_parts:
            continue
        # VRAM fit check — skip if spec decode is already active on this device
        # (we're swapping the draft, not adding on top of a clean load)
        already_active = bool(_spec_decode.get(device))
        if not already_active and vram_free_mb > 0 and candidate_size_mb * 1.15 > vram_free_mb:
            continue
        candidates.append({"id": m["id"], "path": candidate_path,
                           "size_gb": round(candidate_size_mb / 1024, 2)})

    candidates.sort(key=lambda c: c["size_gb"])
    return candidates


def init_settings():
    """Load saved settings from cluster.json on startup."""
    global _cpu_ram_enabled, _auto_unload_enabled, _last_command_time, _spec_decode
    global _aggressive_vram_enabled, _ram_offload_enabled
    cfg = _read_config()
    _cpu_ram_enabled = cfg.get("cpu_ram_enabled", False)
    _auto_unload_enabled = cfg.get("auto_unload", False)
    _aggressive_vram_enabled = cfg.get("aggressive_vram", False)
    _ram_offload_enabled = cfg.get("ram_offload", False)
    _spec_decode = cfg.get("spec_decode", {})
    _last_command_time = time.time()  # reset on startup
    if _cpu_ram_enabled:
        log.info("[config] CPU/RAM device enabled (from saved config)")
    if _auto_unload_enabled:
        log.info("[config] Auto-unload enabled (15 min idle)")
    if _aggressive_vram_enabled:
        log.info("[config] Aggressive VRAM enabled (from saved config)")
    if _ram_offload_enabled:
        log.info("[config] RAM offload enabled (from saved config)")
    for dev, dp in _spec_decode.items():
        log.info(f"[config] Spec-decode enabled on {dev}: {Path(dp).name}")


# ── Crashed-server auto-restart ──────────────────────────────────────────

_restart_cooldown = {}  # device → last_attempt_time
_liveness_interval = {}  # device → last_liveness_check_time
_LIVENESS_CHECK_SEC = 120  # run inference liveness check every 2 min


def _touch_watchdog_health():
    """Touch the watchdog health file to prevent watchdog kill during long ops."""
    hf = os.environ.get("CLUSTERFLOCK_HEALTH_FILE")
    if hf:
        try:
            Path(hf).touch()
        except OSError:
            pass

def check_crashed_servers():
    """Detect and auto-restart servers that crashed or are stuck.

    Called from the heartbeat loop.  Handles two failure modes:
      1. Process died — _devices has an entry but the process exited.
      2. Inference stuck — process alive, /health ok, but actual inference
         hangs (the "zombie server" scenario).

    Liveness probes run every _LIVENESS_CHECK_SEC to avoid overloading.
    """
    alive = active_devices()
    now = time.time()

    for dev, info in list(_devices.items()):
        model_id = info.get("model_id")
        model_path = info.get("model_path")
        if not model_id:
            if dev not in alive:
                del _devices[dev]
            continue

        need_restart = False
        reason = ""

        if dev not in alive:
            # ── Mode 1: process crashed ──────────────────────────────
            need_restart = True
            reason = "crashed"
        else:
            # ── Mode 2: inference liveness probe ─────────────────────
            last_check = _liveness_interval.get(dev, 0)
            if now - last_check >= _LIVENESS_CHECK_SEC:
                _liveness_interval[dev] = now
                port = info.get("port") or _port_for_device(dev)
                if not inference_liveness_check(port, timeout=30):
                    need_restart = True
                    reason = "stuck (inference unresponsive)"

        if not need_restart:
            continue

        # Cooldown: don't retry more than once every 30 seconds
        last = _restart_cooldown.get(dev, 0)
        if now - last < 30:
            continue

        # Skip if a job thread is currently loading/downloading on this device
        lock = _get_device_lock(dev)
        if not lock.acquire(blocking=False):
            log.debug(f"[recovery] {dev} busy (job in progress) — deferring restart")
            continue
        _restart_cooldown[dev] = now

        log.info(f"[recovery] Server {dev} {reason} — reloading {model_id}...")
        try:
            _touch_watchdog_health()  # prevent watchdog kill during reload
            # Force-kill the stuck process before reloading
            stop_server(dev)
            _set_activity("loading", model_id)
            _load_model(model_id, device=dev, model_path=model_path)
            log.info(f"[recovery] ✓ {model_id} reloaded on {dev}")
            _liveness_interval[dev] = time.time()  # reset liveness timer
            _auto_bench(model_id, dev)
        except Exception as e:
            log.error(f"[recovery] ✗ Failed to reload {model_id}: {e}")
            # Remove stale entry so we don't keep retrying forever
            _devices.pop(dev, None)
        finally:
            _set_activity("idle")
            lock.release()


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
        log.info(f"[benchmark] {perf['tokens_per_sec']} tok/s")
    except Exception as e:
        log.error(f"[benchmark] failed: {e}")
    finally:
        _set_activity("idle")


def _force_bench(model_id, device):
    """Benchmark unconditionally (e.g. after spec decode change)."""
    if not model_id:
        return
    _set_activity("benchmarking", model_id)
    try:
        port = _port_for_device(device)
        perf = benchmark(port=port)
        save_bench(model_id, perf, device=device)
        log.info(f"[benchmark] {perf['tokens_per_sec']} tok/s (post spec-decode)")
    except Exception as e:
        log.error(f"[benchmark] failed: {e}")
    finally:
        _set_activity("idle")


# ── Model path resolution ───────────────────────────────────────────────

def _resolve_model_path(model_id):
    """Find the GGUF file for a model ID.

    model_id for "load" commands is always the exact relative path from
    local_models() (e.g. "Org/Repo/file.gguf").  We match exactly first,
    then fall back to progressively looser strategies.
    """
    # Absolute path already on disk
    if model_id.endswith(".gguf") and Path(model_id).exists():
        return model_id

    models = local_models()

    # 1. Exact match — the normal case (UI sends the id it got from us)
    for m in models:
        if model_id == m["id"]:
            return m["path"]

    # 2. model_id is a substring of the local id (e.g. shorter catalog key)
    for m in models:
        if model_id in m["id"]:
            return m["path"]

    # 3. Filename-only match — model_id's last component matches a local filename
    target_file = model_id.rsplit("/", 1)[-1].lower()
    if target_file:
        for m in models:
            local_file = m["id"].rsplit("/", 1)[-1].lower()
            if target_file == local_file:
                return m["path"]

    return None


# ── Auto-unload (sleeping models) ───────────────────────────────────────

def auto_unload_enabled():
    """Whether auto-unload is currently enabled."""
    return _auto_unload_enabled


def aggressive_vram_enabled():
    """Whether aggressive VRAM mode is currently enabled."""
    return _aggressive_vram_enabled


def ram_offload_enabled():
    """Whether RAM offload mode is currently enabled."""
    return _ram_offload_enabled


def primed_models():
    """Return dict of device → {model_id, model_path} for sleeping models."""
    return dict(_primed)


def check_auto_unload():
    """If auto-unload enabled and idle too long, unload models but keep primed.

    Called every heartbeat. If no commands received for _AUTO_UNLOAD_SEC
    and there are loaded models, unload them and store in _primed so they
    can be transparently reloaded on next command.
    """
    if not _auto_unload_enabled:
        return
    if not _devices:
        return
    if time.time() - _last_command_time < _AUTO_UNLOAD_SEC:
        return

    for dev, info in list(_devices.items()):
        model_id = info.get("model_id")
        model_path = info.get("model_path")
        if not model_id:
            continue
        # Store primed state before unloading
        _primed[dev] = {"model_id": model_id, "model_path": model_path}
        tag = "CPU/RAM" if dev == "cpu" else dev.upper()
        log.info(f"[auto-unload] {model_id} on {tag} → sleeping "
                 f"(idle {_AUTO_UNLOAD_SEC // 60}min)")
        stop_server(dev)

    _devices.clear()


def _wake_primed(model_hint=None):
    """Reload a primed (sleeping) model. Returns (device, port) or (None, None).

    If model_hint is given, tries to match it. Otherwise wakes the first
    available primed model.
    """
    if not _primed:
        return None, None

    target_dev = None
    if model_hint:
        for dev, pinfo in _primed.items():
            mid = pinfo["model_id"]
            if model_hint == mid or model_hint in mid:
                target_dev = dev
                break
    if not target_dev:
        target_dev = next(iter(_primed))

    pinfo = _primed.pop(target_dev)
    model_id = pinfo["model_id"]
    model_path = pinfo["model_path"]
    tag = "CPU/RAM" if target_dev == "cpu" else target_dev.upper()
    log.info(f"[wake] Reloading {model_id} on {tag} (was sleeping)...")
    _set_activity("loading", model_id)
    try:
        _load_model(model_id, device=target_dev, model_path=model_path)
        log.info(f"[wake] ✓ {model_id} ready on {tag}")
        port = _port_for_device(target_dev)
        return target_dev, port
    except Exception as e:
        log.error(f"[wake] ✗ Failed to reload {model_id}: {e}")
        return None, None
    finally:
        _set_activity("idle")
