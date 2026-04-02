"""Command dispatcher for Mac agent.

Handles orchestrator commands — load/unload/prompt via llama.cpp server.
"""

import json
import os
import re
from pathlib import Path

from server import start_server, stop_server, server_running, complete, benchmark
from models_hf import (local_models, download_model,
                        get_bench, save_bench, MODELS_DIR,
                        auto_select_quant, _resolve_gguf_repo)

_MODEL_RE = re.compile(r'^[\w./@:\-]+$')
_CONFIG = Path(__file__).parent / "cluster.json"

# Track which model is currently loaded
_current_model = None
_current_model_path = None


def _read_config():
    try:
        return json.loads(_CONFIG.read_text())
    except Exception:
        return {}


def current_model():
    """Return the currently loaded model ID, or None."""
    return _current_model


def execute(cmd):
    """Dispatch a command dict. Returns result dict or None."""
    action = cmd.get("action")

    if action == "unload_all":
        _unload()
    elif action == "unload":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        _unload()
    elif action == "load":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        _load_model(mid, context_length=cmd.get("context_length"))
        # Auto-benchmark if no cached result
        if get_bench(mid) == 0:
            try:
                perf = benchmark()
                save_bench(mid, perf)
                print(f"  Benchmark: {perf['tokens_per_sec']} tok/s")
            except Exception as e:
                print(f"  Benchmark failed: {e}")
    elif action == "download_and_load":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        _download_and_load(mid, context_length=cmd.get("context_length"))
        # Auto-benchmark after download+load
        loaded_mid = _current_model or mid
        if get_bench(loaded_mid) == 0:
            try:
                perf = benchmark()
                save_bench(loaded_mid, perf)
                print(f"  Benchmark: {perf['tokens_per_sec']} tok/s")
            except Exception as e:
                print(f"  Benchmark failed: {e}")
        return {"ok": True}
    elif action == "benchmark":
        if not _current_model:
            raise ValueError("no model loaded to benchmark")
        perf = benchmark()
        save_bench(_current_model, perf)
        return {"model_id": _current_model, **perf}
    elif action == "prompt":
        messages = cmd.get("messages")
        if not messages:
            raise ValueError("prompt requires 'messages'")
        if not _current_model:
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
        return complete(messages, max_tokens=cmd.get("max_tokens", -1),
                        generation_timeout=cmd.get("generation_timeout", 300),
                        **kwargs)
    elif action == "delete_model":
        mid = cmd.get("model_id", "")
        if not mid:
            raise ValueError("model_id required")
        return _delete_model(mid)
    else:
        raise ValueError(f"unknown action: {action}")
    return None


def _delete_model(model_id):
    """Delete a downloaded model from disk."""
    global _current_model, _current_model_path
    # Unload if currently loaded
    if _current_model and model_id in _current_model:
        _unload()
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


def _load_model(model_id, context_length=None, model_path=None):
    """Load a model into llama-server."""
    global _current_model, _current_model_path

    if not model_path:
        model_path = _resolve_model_path(model_id)
    if not model_path:
        raise FileNotFoundError(f"Model not found: {model_id}. Download it first.")

    stop_server()

    ctx = context_length  # None = auto-detect in server.py
    print(f"[load] Loading {model_id}...")
    start_server(model_path, ctx_size=ctx)
    _current_model = model_id
    _current_model_path = model_path
    print(f"[load] ✓ {model_id} ready")


def _unload():
    """Unload the current model by stopping the server."""
    global _current_model, _current_model_path
    if _current_model:
        print(f"[unload] Stopping {_current_model}...")
        stop_server()
        _current_model = None
        _current_model_path = None
    else:
        stop_server()


def _download_and_load(model_id, context_length=None):
    """Download a model from HuggingFace and load it."""
    parts = model_id.split("/")
    if len(parts) >= 2:
        hf_repo = "/".join(parts[:2])
        quant = parts[2] if len(parts) > 2 else "q4_k_m"
    else:
        hf_repo = model_id
        quant = "q4_k_m"

    # Auto-select best fitting quantization
    if quant == "auto":
        from hardware import snapshot
        hw = snapshot()
        gpus = hw.get("gpu", [])
        vram_free = gpus[0].get("vram_free_mb", 0) if gpus else 0
        if not vram_free:
            vram_free = hw.get("system", {}).get("ram_total_mb", 0)
        gguf_repo = _resolve_gguf_repo(hf_repo)
        quant = auto_select_quant(gguf_repo, vram_free)
        print(f"[dl+load] Auto-selected quant: {quant} (VRAM free: {vram_free/1024:.1f} GB)")

    path = download_model(hf_repo, quant=quant)
    _load_model(model_id, context_length=context_length, model_path=path)


def _resolve_model_path(model_id):
    """Find the GGUF file for a model ID."""
    if model_id.endswith(".gguf") and Path(model_id).exists():
        return model_id

    for m in local_models():
        if model_id in m["id"] or model_id.replace("/", "_") in m["id"]:
            return m["path"]

    # Broader search — prefer first shard for multi-shard models
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



