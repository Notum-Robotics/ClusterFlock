"""LM Studio interface: server control, model management, inference."""

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

API = "http://localhost:1234"
HF_API = "https://huggingface.co/api/models"

# Persistent benchmark cache: model_id → {tokens_per_sec, completion_tokens, elapsed_sec, timestamp}
_BENCH_FILE = Path(__file__).parent / "benchmarks.json"
_bench_cache: dict = {}


def _load_bench_cache():
    global _bench_cache
    try:
        _bench_cache = json.loads(_BENCH_FILE.read_text())
    except Exception:
        _bench_cache = {}


def _save_bench_cache():
    try:
        _BENCH_FILE.write_text(json.dumps(_bench_cache, indent=2))
    except Exception:
        pass


def get_bench(model_id):
    """Return cached tokens_per_sec for a model, or 0."""
    return _bench_cache.get(model_id, {}).get("tokens_per_sec", 0)


# Load on import
_load_bench_cache()

# ── Built-in model catalog ────────────────────────────────────────────────────
# Loaded from model_catalog.json in the project root.
_CATALOG_JSON = Path(__file__).resolve().parent.parent.parent / "model_catalog.json"


# ── Internals ────────────────────────────────────────────────────────────────

def _lms_path():
    p = shutil.which("lms")
    if p:
        return p
    for candidate in [
        Path.home() / ".lmstudio" / "bin" / "lms",
        Path.home() / ".cache" / "lm-studio" / "bin" / "lms",
    ]:
        if candidate.exists():
            return str(candidate)
    return None


def _run(args, timeout=300, quiet=True):
    lms = _lms_path()
    if not lms:
        raise RuntimeError("lms CLI not found")
    kw = {"timeout": timeout, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
          "text": True, "encoding": "utf-8", "errors": "replace"}
    r = subprocess.run([lms] + args, **kw)
    if not quiet and r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if not err:
            err = (r.stdout or "").strip()
        raise RuntimeError(f"lms {args[0]} failed" + (f": {err}" if err else ""))
    return (r.stdout or "").strip() if quiet else ""


def _api(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ── Public API ───────────────────────────────────────────────────────────────

def lms_installed():
    return _lms_path() is not None


def server_running():
    try:
        _api("GET", "/v1/models")
        return True
    except Exception:
        return False


def disable_lmlink():
    """Disable LM Link so models load locally, not on remote peers."""
    try:
        out = _run(["link", "status"], timeout=10)
        if "disabled" in out.lower():
            return  # already off
        _run(["link", "disable"], timeout=10)
        print("✓ LM Link disabled")
    except Exception:
        pass  # older lms versions may not have 'link' command


def ensure_server():
    disable_lmlink()
    if server_running():
        return
    print("Starting LM Studio server...")
    try:
        _run(["server", "start"], timeout=30)
    except Exception:
        pass
    for attempt in range(60):
        time.sleep(1)
        if server_running():
            print("✓ Server running")
            return
        # Retry the start command halfway through — llmster may need
        # time to boot before it can accept CLI commands (e.g. Jetson).
        if attempt == 20:
            try:
                _run(["server", "start"], timeout=30)
            except Exception:
                pass
    raise RuntimeError("LM Studio server failed to start")


def _restart_server():
    """Force-kill the llmster backend and restart LM Studio server.

    Recovers from stuck/corrupt llmster states (e.g. every load returns
    "Operation canceled") by killing the process and spawning a fresh one.
    """
    print("  Restarting LM Studio (killing llmster)…")
    try:
        subprocess.run(["pkill", "-9", "-f", "llmster"],
                       timeout=10, capture_output=True)
    except Exception:
        pass
    time.sleep(3)
    ensure_server()


def lms_ps():
    """Return list of currently loaded models via `lms ps --json`."""
    try:
        out = _run(["ps", "--json"], timeout=15)
        models = json.loads(out)
        if isinstance(models, list):
            return models
        return []
    except Exception:
        return []


def loaded_models():
    """Return actually-loaded models via lms ps, with HTTP API fallback."""
    # Primary: lms ps --json (ground truth from CLI)
    ps = lms_ps()
    if ps:
        result = []
        for m in ps:
            mid = m.get("identifier") or m.get("modelKey") or m.get("path", "")
            if not mid:
                continue
            result.append({
                "id": mid,
                "context_length": m.get("contextLength", m.get("maxContextLength", 0)),
                "max_context_length": m.get("maxContextLength", 0),
            })
        return result

    # Fallback: HTTP API
    try:
        data = _api("GET", "/api/v1/models")
        result = []
        for m in data.get("models", []):
            instances = m.get("loaded_instances", [])
            if not instances:
                continue
            ctx = instances[0].get("config", {}).get("context_length", 0)
            max_ctx = m.get("max_context_length", 0)
            result.append({
                "id": m.get("key", ""),
                "context_length": ctx,
                "max_context_length": max_ctx,
            })
        return result
    except Exception:
        return []


def unload_all():
    """Unload every model currently in memory."""
    try:
        _run(["unload", "--all"], timeout=120)
    except Exception:
        pass


def local_models():
    """Set of locally downloaded model keys via lms ls --json."""
    try:
        out = _run(["ls", "--json"], timeout=15)
        models = json.loads(out)
        return {m.get("modelKey") for m in models if m.get("type") == "llm"}
    except Exception:
        return set()


_dl_cache = []
_dl_cache_ts = 0
_DL_CACHE_TTL = 60  # seconds


def downloaded_for_heartbeat():
    """Cached list of downloaded model dicts for heartbeat payload.
    Refreshed at most once per minute to avoid subprocess overhead."""
    global _dl_cache, _dl_cache_ts
    now = time.time()
    if now - _dl_cache_ts < _DL_CACHE_TTL and _dl_cache:
        return list(_dl_cache)
    try:
        out = _run(["ls", "--json"], timeout=15)
        models = json.loads(out)
    except Exception:
        return list(_dl_cache)
    result = []
    for m in models:
        if m.get("type") != "llm":
            continue
        key = m.get("modelKey", "")
        size = m.get("sizeBytes", 0)
        if not key or not size:
            continue
        entry = {"id": key, "name": m.get("displayName", key),
                 "file_size": size,
                 "max_context_length": m.get("maxContextLength", 0)}
        pb = _parse_params(m.get("paramsString"))
        if pb > 0:
            entry["params_b"] = pb
        result.append(entry)
    _dl_cache = result
    _dl_cache_ts = now
    return list(result)


# ── Model catalog ─────────────────────────────────────────────────────────────

def _parse_params(s):
    """Parse paramsString like '3B', '70B', '0.6B' into float billions."""
    m = re.match(r"([\d.]+)\s*[Bb]", s or "")
    return float(m.group(1)) if m else 0.0


def _estimate_memory(params_b, file_size_bytes, max_ctx):
    """Total memory: model file + KV cache at full context.
    KV cache ≈ params_B * 600 bytes/token * max_context."""
    return file_size_bytes + int(params_b * 600 * max_ctx)


def _entry(id_, name, size_gb, params_b, max_ctx, downloaded=False, quant="Q4_K_M"):
    file_bytes = int(size_gb * (1024 ** 3))
    return {
        "id": id_, "name": name, "params_b": float(params_b),
        "file_size": file_bytes, "max_ctx": max_ctx, "quant": quant,
        "total_mem": _estimate_memory(float(params_b), file_bytes, max_ctx),
        "downloaded": downloaded,
    }


def _builtin_catalog():
    """Return built-in curated model catalog from model_catalog.json."""
    try:
        raw = json.loads(_CATALOG_JSON.read_text())
        return [_entry(m["id"], m["name"], m["size_gb"], m["params_b"], m["max_ctx"])
                for m in raw]
    except Exception as e:
        print(f"[catalog] Failed to load {_CATALOG_JSON}: {e}")
        return []


def _local_catalog():
    """Build catalog from locally-downloaded models via lms ls --json."""
    try:
        out = _run(["ls", "--json"], timeout=15)
        models = json.loads(out)
    except Exception:
        return []

    catalog = []
    for m in models:
        if m.get("type") != "llm":
            continue
        key = m.get("modelKey", "")
        size = m.get("sizeBytes", 0)
        if not key or not size:
            continue
        q = m.get("quantization", "?")
        quant = q.get("name", str(q)) if isinstance(q, dict) else str(q)
        catalog.append(_entry(
            key, m.get("displayName", key), size / (1024 ** 3),
            _parse_params(m.get("paramsString")),
            m.get("maxContextLength", 4096), downloaded=True, quant=quant,
        ))
    return catalog


def _parse_params_from_name(name):
    """Extract param count in billions from model name like 'Qwen3.5-9B'."""
    m = re.search(r'(?:^|[-_ ])(\d+\.?\d*)\s*B(?:[-_ ]|$)', name, re.I)
    return float(m.group(1)) if m else 0.0


def hf_catalog(limit=50):
    """Fetch top GGUF models from Hugging Face (lmstudio-community)."""
    print("  Checking Hugging Face for GGUF models...")
    url = (f"{HF_API}?author=lmstudio-community"
           f"&sort=downloads&direction=-1&limit={limit}&filter=gguf")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ClusterFlock-nNode/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            models = json.loads(resp.read())
    except Exception as e:
        print(f"  Could not reach Hugging Face: {e}")
        return []

    catalog = []
    for m in models:
        model_id = m.get("id", "")
        name = model_id.split("/")[-1].replace("-GGUF", "")

        # Skip non-text models (vision projectors, MLX, embeddings)
        if any(skip in name.lower() for skip in ("mmproj", "embed", "mlx")):
            continue

        params_b = _parse_params_from_name(name)
        if not params_b:
            continue

        size_gb = round(params_b * 0.6, 1)
        hf_url = f"https://huggingface.co/{model_id}"
        catalog.append(_entry(hf_url, name, size_gb, params_b, 131072))

    if catalog:
        print(f"  Found {len(catalog)} models on Hugging Face")
    return catalog


def fetch_catalog(include_hf=False):
    """Build model catalog from local + builtin + optional HuggingFace sources.
    Returns list of model dicts sorted by file_size desc."""
    print("  Building model catalog...")

    local = _local_catalog()
    builtin = _builtin_catalog()
    hf = hf_catalog() if include_hf else []

    # Merge: local > builtin > HF (local has richest metadata)
    seen = set()
    catalog = []
    for source in [local, builtin, hf]:
        for m in source:
            if m["id"] not in seen:
                seen.add(m["id"])
                catalog.append(m)

    catalog.sort(key=lambda c: c["file_size"], reverse=True)
    dl = sum(1 for m in catalog if m.get("downloaded"))
    print(f"  Catalog: {len(catalog)} models ({dl} downloaded, {len(catalog) - dl} remote)")
    return catalog


def pick_best_models(gpus, catalog, tight_pack=False):
    """Pick models for GPUs. Returns list of (gpu_index, gpu_dict, model_entry_dict).

    tight_pack=True  → bin-pack multiple models per GPU.
    tight_pack=False → one best (largest fitting) model per GPU (default).

    Models are sized to fit within a single GPU — no splitting across GPUs.
    """
    assignments = []
    used_mem = 0  # cumulative for unified memory systems
    assigned_global = set()

    for i, g in enumerate(gpus):
        vram_mb = g.get("vram_free_mb", 0)
        budget = vram_mb * (1024 ** 2) - used_mem
        name = g.get("name", f"GPU {i}")
        unified = g.get("unified", False)
        assigned_here = set()

        print(f"\n  GPU {i}: {name} ({budget / (1024**3):.1f}GB available)")

        while True:
            picked = None
            for m in catalog:
                mid = m["id"]
                if mid in assigned_here or mid in assigned_global:
                    continue
                if m["total_mem"] <= budget:
                    picked = m
                    break  # catalog is sorted largest-first, take first that fits
            if not picked:
                break
            # Commit
            total_gb = picked["total_mem"] / (1024 ** 3)
            file_gb = picked["file_size"] / (1024 ** 3)
            kv_gb = total_gb - file_gb
            dl_tag = "" if picked.get("downloaded") else " [download]"
            print(f"    ✓ {picked['id']} ({picked['params_b']:.0f}B {picked['quant']}): "
                  f"{file_gb:.1f}GB + {kv_gb:.1f}GB KV@{picked['max_ctx']:,} = "
                  f"{total_gb:.1f}GB{dl_tag}")
            assignments.append((i, g, picked))
            assigned_here.add(picked["id"])
            assigned_global.add(picked["id"])
            budget -= picked["total_mem"]
            if unified:
                used_mem += picked["total_mem"]
            if not tight_pack:
                break  # one model per GPU

        remaining_gb = budget / (1024 ** 3)
        if assigned_here:
            print(f"    → {len(assigned_here)} model(s), {remaining_gb:.1f}GB remaining")
        else:
            print("    No model fits.")

    return assignments


def download_model(search_id, quantization=None):
    """Download via LM Studio REST API (POST /api/v1/models/download),
    falling back to lms CLI if the endpoint is unavailable."""
    print(f"  Downloading {search_id}...")
    body = {"model": search_id}
    if quantization:
        body["quantization"] = quantization
    try:
        resp = _api("POST", "/api/v1/models/download", body=body)
        status = resp.get("status")
        if status == "already_downloaded":
            print("  Already downloaded")
            return
        job_id = resp.get("job_id")
        if job_id:
            _poll_download(job_id, resp.get("total_size_bytes", 0))
            return
    except Exception:
        pass
    # Fallback: lms CLI
    _run(["get", search_id, "--yes", "--gguf"], timeout=3600, quiet=False)


def _poll_download(job_id, total_bytes=0):
    """Poll GET /api/v1/models/download/status/:job_id until done."""
    while True:
        time.sleep(5)
        try:
            resp = _api("GET", f"/api/v1/models/download/status/{job_id}")
        except Exception:
            print(f"\n  Lost contact with download job {job_id}")
            return
        status = resp.get("status", "")
        downloaded = resp.get("downloaded_bytes", 0)
        total = resp.get("total_size_bytes", total_bytes) or total_bytes
        if total and downloaded:
            pct = downloaded / total * 100
            speed = resp.get("bytes_per_second", 0)
            speed_mb = speed / (1024 ** 2) if speed else 0
            print(f"\r  {pct:5.1f}% ({downloaded / (1024 ** 3):.1f}/"
                  f"{total / (1024 ** 3):.1f} GB) {speed_mb:.1f} MB/s",
                  end="", flush=True)
        if status == "completed":
            print("\n  \u2713 Download complete")
            return
        if status == "failed":
            print()
            raise RuntimeError(f"Download failed (job {job_id})")
        if status not in ("downloading", "paused"):
            print()
            raise RuntimeError(f"Unexpected status: {status}")


def download_and_identify(search_id):
    """Download a model and return its local model_key for loading."""
    before = local_models()
    quant = "Q4_K_M" if search_id.startswith("https://") else None
    download_model(search_id, quantization=quant)
    after = local_models()

    new = after - before
    if new:
        return new.pop()

    # Already downloaded or detection failed — fuzzy match
    search_name = search_id.split("/")[-1] if "/" in search_id else search_id
    search_norm = search_name.lower().replace("-", "").replace("gguf", "")
    for key in after:
        if search_norm in key.lower().replace("-", ""):
            return key

    return search_id


# Minimum free VRAM (MB) to keep after loading a model
_VRAM_RESERVE_MB = 512

# Generation timeout (seconds) — cut off inference after this long.
# Configurable remotely via "generation_timeout" in commands.
_GENERATION_TIMEOUT = 300  # 5 minutes default


def _model_size_mb(model_id):
    """Lookup file size of a local model in MB, or 0 if unknown."""
    try:
        out = _run(["ls", "--json"], timeout=15)
        for m in json.loads(out):
            if m.get("type") == "llm" and m.get("modelKey") == model_id:
                return m.get("sizeBytes", 0) // (1024 * 1024)
    except Exception:
        pass
    return 0


def _model_max_ctx(model_id):
    """Lookup maxContextLength for a local model, or 0 if unknown."""
    try:
        out = _run(["ls", "--json"], timeout=15)
        for m in json.loads(out):
            if m.get("type") == "llm" and m.get("modelKey") == model_id:
                return m.get("maxContextLength", 0)
    except Exception:
        pass
    return 0


_VRAM_SETTLE_MAX = 45          # max seconds to wait for VRAM reclaim
_VRAM_SETTLE_INTERVAL = 5     # seconds between polls (matches hardware cache TTL)
_VRAM_STABLE_THRESHOLD = 64   # MB — readings within this are "stable"


def _fresh_free_mb(gpu_idx=None):
    """Get a live free-VRAM reading, bypassing the hardware cache.

    If gpu_idx is given, return free VRAM for that specific GPU.
    Otherwise return total free across all GPUs.
    """
    import hardware
    hardware._cache.pop("gpu", None)          # flush stale entry
    gpus = hardware.gpu()
    if not gpus:
        return None
    if gpu_idx is not None and 0 <= gpu_idx < len(gpus):
        return gpus[gpu_idx].get("vram_free_mb", 0)
    return sum(g.get("vram_free_mb", 0) for g in gpus)


def _wait_for_vram(needed_mb, gpu_idx=None):
    """Poll VRAM until enough is free or it stops climbing.

    Returns (free_mb, waited_sec).  Gives up once readings stabilise
    below the target for two consecutive polls.
    If gpu_idx is set, checks that specific GPU only.
    """
    prev = _fresh_free_mb(gpu_idx)
    if prev is None:
        return None, 0
    if prev >= needed_mb:
        return prev, 0

    waited = 0
    stable_count = 0
    print(f"  VRAM wait: {prev}MB free, need {needed_mb}MB — waiting for reclaim…")

    while waited < _VRAM_SETTLE_MAX:
        time.sleep(_VRAM_SETTLE_INTERVAL)
        waited += _VRAM_SETTLE_INTERVAL
        now = _fresh_free_mb(gpu_idx)
        if now is None:
            return None, waited

        delta = now - prev
        print(f"  VRAM wait: {now}MB free (+{delta}MB) after {waited}s")

        if now >= needed_mb:
            return now, waited                     # enough room

        if abs(delta) <= _VRAM_STABLE_THRESHOLD:
            stable_count += 1
            if stable_count >= 2:                  # two flat readings in a row
                print(f"  VRAM stabilised at {now}MB — not enough")
                return now, waited
        else:
            stable_count = 0                       # still moving, keep waiting

        prev = now

    return _fresh_free_mb(gpu_idx) or prev, waited


def load_model(model_id, gpu=None, context_length=None):
    """Load a model onto a specific GPU, with VRAM safety check.

    gpu: GPU index (int) to pin to, or None for auto.
    context_length: context window size, or None to auto-detect from model metadata.
    Uses --gpu 1 (100% offload) instead of --gpu max because LM Studio's
    'max' flag silently fails on multi-GPU systems (reports success but
    the model immediately unloads).
    """
    # Check what's actually loaded via lms ps (ground truth)
    already = lms_ps()
    for m in already:
        mid = m.get("identifier") or m.get("modelKey") or ""
        if mid == model_id:
            print(f"  {model_id} already loaded — skipping")
            return

    from hardware import gpu as _gpu
    gpus = _gpu()
    gpu_tag = f" (gpu {gpu})" if gpu is not None else ""
    if gpus:
        # LM Studio spreads across all GPUs — always check total VRAM
        model_mb = _model_size_mb(model_id)
        if model_mb:
            needed = model_mb + _VRAM_RESERVE_MB
            free_mb, waited = _wait_for_vram(needed, gpu_idx=None)
            if free_mb is not None and free_mb < needed:
                raise RuntimeError(
                    f"VRAM guard{gpu_tag}: {free_mb}MB total free, model ~{model_mb}MB + "
                    f"{_VRAM_RESERVE_MB}MB reserve = {needed}MB needed "
                    f"— refusing load (waited {waited}s)"
                )
            if free_mb is not None:
                print(f"  VRAM{gpu_tag}: {model_mb}MB model + {_VRAM_RESERVE_MB}MB reserve "
                      f"= {needed}MB needed, {free_mb}MB total free"
                      + (f" (after {waited}s wait)" if waited else ""))
        else:
            free_mb = sum(g.get("vram_free_mb", 0) for g in gpus)
            if free_mb < _VRAM_RESERVE_MB:
                raise RuntimeError(
                    f"VRAM guard{gpu_tag}: only {free_mb}MB total free (reserve={_VRAM_RESERVE_MB}MB) — refusing load"
                )
    ensure_server()
    # Resolve context length: explicit > model metadata > default
    if context_length is None:
        context_length = _model_max_ctx(model_id)
    if not context_length or context_length <= 0:
        context_length = 4096  # LM Studio default fallback
    # Cap context to fit in available VRAM (model weights + KV cache + reserve)
    model_mb = _model_size_mb(model_id) or 0
    if model_mb and gpus:
        free_mb = sum(g.get("vram_free_mb", 0) for g in gpus)
        # Estimate params_b from file size.  Use 1.5 GB/B as a middle ground
        # between Q4_K_M (0.6) and FP16 (2.0) — avoids catastrophic
        # underestimation for higher quantisations / FP16 weights.
        params_b = max((model_mb / 1024) / 1.5, 0.5)
        # KV cache per token: modern models universally use GQA (grouped-query
        # attention), so KV heads are much fewer than query heads.  Empirical
        # fit for GQA models (Llama 3, Qwen 2/3, Gemma 2, Nemotron, …):
        # ~2500 bytes per billion params per token.
        kv_per_token_mb = params_b * 2500 / (1024 * 1024)  # MB per context token
        budget_for_kv_mb = free_mb - model_mb - _VRAM_RESERVE_MB
        if budget_for_kv_mb > 0 and kv_per_token_mb > 0:
            max_ctx_fits = int(budget_for_kv_mb / kv_per_token_mb)
            if max_ctx_fits < context_length:
                old_ctx = context_length
                context_length = max(max_ctx_fits, 4096)
                print(f"  VRAM cap: ctx {old_ctx:,} → {context_length:,} "
                      f"(~{params_b:.0f}B params, {budget_for_kv_mb:.0f}MB for KV)")
    # Hard cap: 262144 (256K) — VRAM cap above already guards OOM
    context_length = min(context_length, 262144)
    ctx_tag = f" ctx={context_length:,}" if context_length > 4096 else ""
    print(f"  Loading {model_id}{gpu_tag}{ctx_tag} --gpu 1...")
    load_args = ["load", model_id, "--gpu", "1", "--context-length", str(context_length), "-y"]
    try:
        _run(load_args, timeout=300, quiet=False)
    except RuntimeError as first_err:
        err_str = str(first_err).lower()
        # Don't restart on permanent errors — restarting kills all loaded
        # models and won't fix "not found" or similar issues.
        if "not found" in err_str or "no model" in err_str:
            raise
        print(f"  Load failed ({first_err}) — restarting LM Studio and retrying…")
        _restart_server()
        try:
            _run(load_args, timeout=300, quiet=False)
        except RuntimeError:
            # If high context failed twice, halve it and try once more
            if context_length > 131072:
                fallback_ctx = context_length // 2
                print(f"  Retry also failed — falling back to ctx {fallback_ctx:,}")
                load_args = ["load", model_id, "--gpu", "1",
                             "--context-length", str(fallback_ctx), "-y"]
                _run(load_args, timeout=300, quiet=False)
            else:
                raise
    # Verify model is actually loaded (LM Studio can report success but
    # silently drop the model on multi-GPU systems)
    time.sleep(1)
    loaded = lms_ps()
    found = any(
        (m.get("identifier") or m.get("modelKey") or "") == model_id
        for m in loaded
    )
    if not found:
        raise RuntimeError(
            f"lms reported success but model is not loaded "
            f"(phantom load — check LM Studio logs on this node)"
        )


def unload_model(model_id):
    _run(["unload", model_id], timeout=60)


def benchmark(model=None):
    start = time.time()
    data = _api("POST", "/v1/chat/completions", body={
        "model": model or "",
        "messages": [{"role": "user", "content": "Briefly explain distributed computing in one paragraph."}],
        "max_tokens": 200,
    })
    elapsed = time.time() - start
    tokens = data.get("usage", {}).get("completion_tokens", 0)
    result = {
        "tokens_per_sec": round(tokens / elapsed, 1) if elapsed > 0 else 0,
        "completion_tokens": tokens,
        "elapsed_sec": round(elapsed, 2),
    }
    # Persist benchmark to cache
    mid = model or ""
    if not mid:
        models = loaded_models()
        if models:
            mid = models[0]["id"]
    if mid and result["tokens_per_sec"] > 0:
        _bench_cache[mid] = {**result, "timestamp": time.time()}
        _save_bench_cache()
    return result


def complete(messages, model=None, max_tokens=None, generation_timeout=None,
             temperature=None, top_p=None, frequency_penalty=None,
             presence_penalty=None, stop=None):
    """Run chat completion with a generation-time safeguard.

    Streams tokens from LM Studio. If generation exceeds
    generation_timeout (default _GENERATION_TIMEOUT seconds),
    the connection is closed and whatever was generated so far
    is returned.
    """
    if not model:
        models = loaded_models()
        if models:
            model = models[0]["id"]
    if not model:
        raise RuntimeError("No models loaded — cannot complete prompt")
    timeout_sec = generation_timeout or _GENERATION_TIMEOUT
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if frequency_penalty is not None:
        body["frequency_penalty"] = frequency_penalty
    if presence_penalty is not None:
        body["presence_penalty"] = presence_penalty
    if stop is not None:
        body["stop"] = stop
    # Stream tokens with a wall-clock timeout safeguard
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    req = urllib.request.Request(f"{API}/v1/chat/completions", data=data,
                                 headers=headers, method="POST")
    collected_content = []
    collected_reasoning = []
    resp_model = model
    resp_id = ""
    finish_reason = "timeout"
    prompt_tokens = 0
    completion_tokens = 0
    timed_out = False
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec + 30) as resp:
            for raw_line in resp:
                if time.time() - t0 > timeout_sec:
                    timed_out = True
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                resp_id = chunk.get("id", resp_id)
                resp_model = chunk.get("model", resp_model)
                for ch in chunk.get("choices", []):
                    delta = ch.get("delta", {})
                    if delta.get("content"):
                        collected_content.append(delta["content"])
                    if delta.get("reasoning_content"):
                        collected_reasoning.append(delta["reasoning_content"])
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
                usage = chunk.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
    except Exception as e:
        if not collected_content and not collected_reasoning:
            raise
        # Partial result — return what we have
        finish_reason = "error"
        print(f"  [complete] stream interrupted: {e}")

    if timed_out:
        print(f"  [complete] generation timeout after {timeout_sec}s — returning partial")

    content = "".join(collected_content)
    reasoning = "".join(collected_reasoning)
    if not completion_tokens:
        completion_tokens = max(len(content) // 4, 1)
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {
        "id": resp_id or "chatcmpl-timeout",
        "object": "chat.completion",
        "created": int(t0),
        "model": resp_model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


# ── Background download-and-load jobs ────────────────────────────────────────

_jobs = {}
_jobs_lock = threading.Lock()


def start_download_and_load(search_id, gpu=None, context_length=None):
    """Start a background download+load job.  Returns job_id immediately."""
    job_id = "dl-" + uuid.uuid4().hex[:8]
    job = {
        "job_id": job_id,
        "model_id": search_id,
        "gpu": gpu,
        "context_length": context_length,
        "status": "running",
        "phase": "starting",
        "progress_pct": 0,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "speed_bps": 0,
        "error": None,
        "log": [],
    }
    with _jobs_lock:
        _jobs[job_id] = job
    threading.Thread(target=_run_download_and_load, args=(job,), daemon=True).start()
    return job_id


def get_job_status(job_id):
    """Return current state of a background job, or None."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return None
    return {
        "job_id": job["job_id"],
        "model_id": job["model_id"],
        "gpu": job["gpu"],
        "status": job["status"],
        "phase": job["phase"],
        "progress_pct": job["progress_pct"],
        "downloaded_bytes": job["downloaded_bytes"],
        "total_bytes": job["total_bytes"],
        "speed_bps": job["speed_bps"],
        "error": job["error"],
        "log": list(job["log"]),
    }


def _parse_model_id(search_id):
    """Parse org/repo/quant → (org/repo, quant) or (search_id, None)."""
    parts = search_id.split("/")
    if len(parts) == 3:
        return f"{parts[0]}/{parts[1]}", parts[2]
    return search_id, None


def _cli_download(search_id):
    """Download a model via lms CLI.  Tries full org/repo path first."""
    repo, quant = _parse_model_id(search_id)
    # Try 1: full org/repo path (exact HuggingFace identifier)
    if "/" in repo:
        try:
            _run(["get", repo, "--yes", "--gguf"], timeout=3600, quiet=False)
            return
        except Exception:
            pass
    # Try 2: repo name only as search term
    search_term = repo.split("/")[-1] if "/" in repo else repo
    if quant:
        search_term = f"{search_term} {quant}"
    try:
        _run(["get", search_term, "--yes", "--gguf"], timeout=3600, quiet=False)
        return
    except Exception:
        pass
    # Try 3: repo name without quant suffix
    if quant:
        bare = repo.split("/")[-1] if "/" in repo else repo
        try:
            _run(["get", bare, "--yes", "--gguf"], timeout=3600, quiet=False)
            return
        except Exception:
            pass
    raise RuntimeError(
        f"lms get failed: Searching for models with the term {search_id}"
    )


def _job_log(job, msg):
    job["log"].append({"t": time.time(), "msg": msg})
    print(f"  [job:{job['job_id']}] {msg}")


def _run_download_and_load(job):
    """Background thread: download if needed, then load."""
    search_id = job["model_id"]
    gpu = job["gpu"]

    try:
        # ── Phase 1: check local models ──────────────────────────────
        job["phase"] = "checking"
        _job_log(job, f"Checking if {search_id} is downloaded…")

        existing = local_models()
        model_key = _match_local(search_id, existing)

        if model_key:
            job["phase"] = "already_downloaded"
            job["progress_pct"] = 100
            _job_log(job, f"Already downloaded: {model_key}")
        else:
            # ── Phase 2: download ────────────────────────────────────
            job["phase"] = "downloading"
            job["progress_pct"] = 0
            _job_log(job, f"Downloading {search_id}…")

            # For 3-segment IDs (org/repo/quant), use org/repo for LM Studio
            dl_id = _parse_model_id(search_id)[0]
            try:
                resp = _api("POST", "/api/v1/models/download",
                            {"model": dl_id})
                status = resp.get("status")
                if status == "already_downloaded":
                    job["progress_pct"] = 100
                    _job_log(job, "Already downloaded")
                else:
                    dl_job = resp.get("job_id")
                    if dl_job:
                        job["total_bytes"] = resp.get("total_size_bytes", 0)
                        _poll_download_job(dl_job, job)
                    else:
                        _job_log(job, "No job_id — trying CLI fallback")
                        _cli_download(search_id)
            except Exception:
                _job_log(job, "API download unavailable, using CLI…")
                _cli_download(search_id)

            # Identify the local key of the newly-downloaded model
            after = local_models()
            model_key = _match_local(search_id, after) or search_id
            _job_log(job, f"Local model key: {model_key}")

        # ── Phase 3: load ────────────────────────────────────────────
        job["phase"] = "loading"
        job["progress_pct"] = 0
        gpu_tag = f" onto GPU {gpu}" if gpu is not None else ""
        _job_log(job, f"Loading {model_key}{gpu_tag}…")

        load_model(model_key, gpu=gpu, context_length=job.get("context_length"))

        job["phase"] = "done"
        job["status"] = "done"
        job["progress_pct"] = 100
        _job_log(job, "Model loaded successfully")

    except Exception as e:
        job["phase"] = "failed"
        job["status"] = "failed"
        job["error"] = str(e)
        _job_log(job, f"Failed: {e}")


def _match_local(search_id, model_set):
    """Return the local model key matching search_id, or None."""
    if search_id in model_set:
        return search_id
    repo, quant = _parse_model_id(search_id)
    # For 3-segment IDs, also try org/repo directly
    if repo != search_id and repo in model_set:
        return repo
    # Use repo name (last segment of org/repo) as search token
    token = repo.split("/")[-1] if "/" in repo else repo
    # Strip common GGUF-related suffixes that appear in catalog IDs
    # but not in local model keys (e.g. "Qwen3.5-9B-GGUF" -> "Qwen3.5-9B")
    token = re.sub(r'-(?:gguf|GGUF)(?:-[A-Za-z0-9_]+)?$', '', token)
    norm = token.lower().replace("-", "").replace("_", "")
    # If we have a quant, prefer keys containing both model name and quant
    if quant:
        quant_norm = quant.lower().replace("-", "").replace("_", "")
        for key in model_set:
            kn = key.lower().replace("-", "").replace("_", "")
            if norm in kn and quant_norm in kn:
                return key
    for key in model_set:
        if norm in key.lower().replace("-", "").replace("_", ""):
            return key
    return None


def _poll_download_job(dl_job_id, job):
    """Poll LM Studio download status, updating our job dict."""
    total_bytes = job.get("total_bytes", 0)
    while True:
        time.sleep(3)
        try:
            resp = _api("GET",
                        f"/api/v1/models/download/status/{dl_job_id}")
        except Exception:
            _job_log(job, f"Lost contact with download {dl_job_id}")
            return
        status = resp.get("status", "")
        downloaded = resp.get("downloaded_bytes", 0)
        total = resp.get("total_size_bytes", total_bytes) or total_bytes
        speed = resp.get("bytes_per_second", 0)
        job["downloaded_bytes"] = downloaded
        job["total_bytes"] = total
        job["speed_bps"] = speed
        if total and downloaded:
            job["progress_pct"] = round(downloaded / total * 100, 1)
        if status == "completed":
            job["progress_pct"] = 100
            _job_log(job, "Download complete")
            return
        if status == "failed":
            raise RuntimeError(f"Download failed (job {dl_job_id})")
        if status not in ("downloading", "paused"):
            raise RuntimeError(f"Unexpected download status: {status}")
