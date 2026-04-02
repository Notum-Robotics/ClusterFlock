"""HuggingFace model manager for DGX Spark agent.

Downloads GGUF models from HuggingFace and manages them in the local
models/ directory. Only GGUF format is supported (llama.cpp native).

Primary download uses huggingface_hub.snapshot_download (pip install huggingface_hub).
Falls back to curl if the library is unavailable.
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

try:
    from huggingface_hub import snapshot_download as _snapshot_download
    _HAS_HF_HUB = True
except ImportError:
    _HAS_HF_HUB = False

AGENT_DIR = Path(__file__).parent
MODELS_DIR = AGENT_DIR / "models"
HF_API = "https://huggingface.co/api/models"
CATALOG_CACHE = AGENT_DIR / "catalog_cache.json"
CATALOG_TTL = 3600  # 1 hour

# Persistent benchmark cache
BENCH_FILE = AGENT_DIR / "benchmarks.json"
_bench_cache: dict = {}

# Download progress tracking (read by heartbeat payload)
_download_state = {"active": False, "model": "", "expected_bytes": 0, "dest_dir": ""}

# ── Curated model catalog for DGX Spark ──────────────────────────────────────
# (hf_repo, gguf_filename_pattern, display_name, size_gb, params_b, max_ctx)
# Prioritized for Spark's 128GB unified memory — focus on large high-quality models
# and NVFP4-quantized GGUFs where available.
_BUILTIN = [
    # Qwen3.5 series
    ("unsloth/Qwen3.5-27B-GGUF",      "q4_k_m",  "Qwen3.5 27B",          16.2,   27, 131072),
    ("unsloth/Qwen3.5-9B-GGUF",       "q4_k_m",  "Qwen3.5 9B",            5.4,    9, 131072),
    ("unsloth/Qwen3.5-4B-GGUF",       "q4_k_m",  "Qwen3.5 4B",            2.3,    4, 131072),
    # Qwen3 series
    ("Qwen/Qwen3-32B-GGUF",           "q4_k_m",  "Qwen3 32B",            19.9,   32, 131072),
    ("Qwen/Qwen3-14B-GGUF",           "q4_k_m",  "Qwen3 14B",             9.0,   14, 131072),
    ("Qwen/Qwen3-8B-GGUF",            "q4_k_m",  "Qwen3 8B",              4.9,    8, 131072),
    ("Qwen/Qwen3-30B-A3B-GGUF",       "q4_k_m",  "Qwen3 30B MoE",        17.4,   30, 131072),
    # Qwen3 Coder
    ("unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", "q4_k_m", "Qwen3 Coder 30B", 17.4, 30, 262144),
    # DeepSeek R1 distills
    ("bartowski/DeepSeek-R1-Distill-Qwen-32B-GGUF", "q4_k_m", "DeepSeek R1 32B", 19.9, 32, 131072),
    ("unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF", "q4_k_m", "DeepSeek R1 14B", 8.5, 14, 131072),
    # Gemma 3
    ("bartowski/google_gemma-3-27b-it-GGUF", "q4_k_m", "Gemma 3 27B",    17.2,   27, 131072),
    ("bartowski/google_gemma-3-12b-it-GGUF", "q4_k_m", "Gemma 3 12B",     7.3,   12, 131072),
    ("bartowski/google_gemma-3-4b-it-GGUF", "q4_k_m", "Gemma 3 4B",       3.0,    4, 131072),
    # Llama 3.3
    ("bartowski/Llama-3.3-70B-Instruct-GGUF", "q4_k_m", "Llama 3.3 70B", 42.5, 70, 131072),
    # Mistral / Devstral
    ("unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF", "q4_k_m", "Mistral Small 24B", 15.0, 24, 131072),
    # Phi-4
    ("microsoft/phi-4-gguf",           "q4_k_m",  "Phi-4 14B",             8.4,   14,  16384),
    # NVIDIA Nemotron
    ("nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF", "q4_k_m", "Nemotron 3 4B",  2.4,    4, 131072),
]


def _load_bench_cache():
    global _bench_cache
    try:
        _bench_cache = json.loads(BENCH_FILE.read_text())
    except Exception:
        _bench_cache = {}


def _save_bench_cache():
    try:
        BENCH_FILE.write_text(json.dumps(_bench_cache, indent=2))
    except Exception:
        pass


def get_bench(model_id):
    """Return cached tokens_per_sec for a model, or 0."""
    return _bench_cache.get(model_id, {}).get("tokens_per_sec", 0)


def save_bench(model_id, perf):
    """Save benchmark result for a model."""
    _bench_cache[model_id] = {**perf, "timestamp": time.time()}
    _save_bench_cache()


_load_bench_cache()


def download_progress():
    """Return current download progress or None if not downloading."""
    if not _download_state["active"]:
        return None
    dest = Path(_download_state["dest_dir"])
    current = 0
    if dest.exists():
        current = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    expected = _download_state["expected_bytes"]
    pct = min(99, int(current / expected * 100)) if expected > 0 else 0
    return {
        "model": _download_state["model"],
        "pct": pct,
        "downloaded_bytes": current,
        "expected_bytes": expected,
    }


# ── Model Discovery ─────────────────────────────────────────────────────────

def local_models():
    """List GGUF models already downloaded in models/ directory.

    Returns list of dicts: {id, name, path, size_gb}.
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    models = []
    for f in sorted(MODELS_DIR.rglob("*.gguf")):
        size_gb = round(f.stat().st_size / (1024**3), 1)
        # Derive a readable ID from path: repo_name/filename
        rel = f.relative_to(MODELS_DIR)
        model_id = str(rel).replace(os.sep, "/")
        name = f.stem.replace("-", " ").replace("_", " ").title()
        models.append({
            "id": model_id,
            "name": name,
            "path": str(f),
            "size_gb": size_gb,
            "downloaded": True,
        })
    return models


def _search_hf_gguf(query="GGUF", limit=50):
    """Search HuggingFace for GGUF model repos."""
    try:
        params = f"?search={query}&filter=gguf&sort=downloads&direction=-1&limit={limit}"
        req = urllib.request.Request(f"{HF_API}{params}")
        req.add_header("User-Agent", "ClusterFlock-Spark/0.1")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[models] HuggingFace search failed: {e}")
        return []


def _hf_repo_files(repo_id):
    """List files in a HuggingFace repo, filtering for GGUF."""
    try:
        url = f"https://huggingface.co/api/models/{repo_id}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "ClusterFlock-Spark/0.1")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            siblings = data.get("siblings", [])
            return [s["rfilename"] for s in siblings
                    if s.get("rfilename", "").endswith(".gguf")]
    except Exception:
        return []


def _pick_gguf_file(files, quant_preference="q4_k_m"):
    """Pick the best GGUF file from a list based on quantization preference."""
    # Exact match first
    for f in files:
        if quant_preference.lower() in f.lower():
            return f
    # Fallback priorities
    for pref in ["q4_k_m", "q4_k_s", "q4_0", "q5_k_m", "q8_0"]:
        for f in files:
            if pref in f.lower():
                return f
    # Last resort: first GGUF file
    return files[0] if files else None


def fetch_catalog(include_hf=False):
    """Build merged catalog of available models.

    Combines built-in curated list with local models and optional HF search.
    Returns list of model dicts sorted by size (largest first).
    """
    catalog = []
    local = {m["id"]: m for m in local_models()}

    # Built-in catalog
    for repo, quant, name, size_gb, params_b, max_ctx in _BUILTIN:
        model_id = f"{repo}/{quant}"
        entry = {
            "id": model_id,
            "hf_repo": repo,
            "quant": quant,
            "name": name,
            "size_gb": size_gb,
            "params_b": params_b,
            "max_ctx": max_ctx,
            "downloaded": False,
            "path": None,
        }
        # Check if already downloaded
        for lid, lm in local.items():
            if quant in lid.lower() and any(part in lid.lower() for part in repo.lower().split("/")):
                entry["downloaded"] = True
                entry["path"] = lm["path"]
                break
        catalog.append(entry)

    # Add any local models not in builtin catalog
    builtin_repos = {r.lower() for r, _, _, _, _, _ in _BUILTIN}
    for lid, lm in local.items():
        if not any(br in lid.lower() for br in builtin_repos):
            catalog.append({
                **lm,
                "hf_repo": None,
                "quant": _detect_quant(lid),
                "params_b": _estimate_params(lm["size_gb"]),
                "max_ctx": 131072,
            })

    # Optional HF search for fresh models
    if include_hf:
        print("[models] Searching HuggingFace for GGUF models...")
        hf_models = _search_hf_gguf(limit=30)
        existing_repos = {e.get("hf_repo", "").lower() for e in catalog}
        for hm in hf_models:
            repo = hm.get("id", "")
            if repo.lower() in existing_repos:
                continue
            if not repo.lower().endswith("-gguf") and "gguf" not in repo.lower():
                continue
            downloads = hm.get("downloads", 0)
            if downloads < 1000:
                continue
            catalog.append({
                "id": f"{repo}/q4_k_m",
                "hf_repo": repo,
                "quant": "q4_k_m",
                "name": repo.split("/")[-1].replace("-GGUF", "").replace("-", " "),
                "size_gb": 0,  # unknown until downloaded
                "params_b": 0,
                "max_ctx": 131072,
                "downloaded": False,
                "path": None,
                "hf_downloads": downloads,
            })

    # Sort by params descending (largest first)
    catalog.sort(key=lambda m: m.get("params_b", 0), reverse=True)
    return catalog


def _detect_quant(filename):
    """Detect quantization type from filename."""
    lower = filename.lower()
    for q in ["q4_k_m", "q4_k_s", "q4_0", "q5_k_m", "q5_k_s", "q8_0",
              "q6_k", "q3_k_m", "q2_k", "iq4_nl", "fp16", "f16", "nvfp4"]:
        if q in lower:
            return q
    return "unknown"


def _estimate_params(size_gb):
    """Rough estimate of parameter count from file size (Q4)."""
    return round(size_gb / 0.6, 0)  # ~0.6 GB per billion params at Q4


# ── Model Download ───────────────────────────────────────────────────────────

def _gguf_pattern(quant="q4_k_m"):
    """Build a glob pattern that matches GGUF files for a given quantization."""
    # Map common short names to glob patterns
    patterns = {
        "q4_k_m":  "*Q4_K_M*.gguf",
        "q4_k_s":  "*Q4_K_S*.gguf",
        "q4_0":    "*Q4_0*.gguf",
        "q5_k_m":  "*Q5_K_M*.gguf",
        "q5_k_s":  "*Q5_K_S*.gguf",
        "q8_0":    "*Q8_0*.gguf",
        "q6_k":    "*Q6_K*.gguf",
        "q3_k_m":  "*Q3_K_M*.gguf",
        "q2_k":    "*Q2_K*.gguf",
        "iq4_nl":  "*IQ4_NL*.gguf",
        "fp16":    "*fp16*.gguf",
        "f16":     "*f16*.gguf",
        "nvfp4":   "*nvfp4*.gguf",
    }
    return patterns.get(quant.lower(), f"*{quant}*.gguf")


# Quant quality ranking — higher index = better quality
_QUANT_RANK = [
    "iq1_s", "iq1_m", "iq2_xxs", "iq2_m",
    "q2_k", "q2_k_l",
    "iq3_xxs", "q3_k_s", "q3_k_m",
    "iq4_xs", "iq4_nl", "q4_0", "q4_1", "q4_k_s", "q4_k_m",
    "q5_k_s", "q5_k_m",
    "q6_k",
    "q8_0",
    "fp16", "f16", "bf16",
]


def _hf_available_quants(hf_repo):
    """Query HF tree API for available quant variants and their total sizes.

    Returns dict: {quant_name_lower: total_size_bytes}.
    Handles both directory-based layouts (unsloth style: Q4_K_M/file.gguf)
    and flat layouts (Qwen style: model-Q8_0.gguf at root).
    """
    quants = {}
    try:
        url = f"https://huggingface.co/api/models/{hf_repo}/tree/main"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "ClusterFlock/0.1")
        with urllib.request.urlopen(req, timeout=15) as resp:
            entries = json.loads(resp.read())
    except Exception as e:
        print(f"[auto-quant] Failed to list repo tree: {e}")
        return quants

    # Check for quant directories (common in unsloth repos)
    known_quants = {q.upper().replace("_", ""): q for q in _QUANT_RANK}
    dirs = [e for e in entries if e.get("type") == "directory"]
    for d in dirs:
        dname = d["path"]
        # Normalize: strip UD- prefix, compare alphanumerically
        norm = dname.upper().replace("-", "").replace("_", "")
        # Remove common prefixes like "UD"
        for prefix in ["UD"]:
            if norm.startswith(prefix):
                norm = norm[len(prefix):]
        matched_quant = None
        for key, qname in known_quants.items():
            if key == norm or norm.startswith(key):
                matched_quant = qname
                break
        if not matched_quant:
            continue
        # Sum file sizes in this directory
        try:
            durl = f"https://huggingface.co/api/models/{hf_repo}/tree/main/{urllib.parse.quote(dname)}"
            dreq = urllib.request.Request(durl)
            dreq.add_header("User-Agent", "ClusterFlock/0.1")
            with urllib.request.urlopen(dreq, timeout=15) as dresp:
                files = json.loads(dresp.read())
            total = sum(f.get("size", 0) for f in files
                        if f.get("rfilename", f.get("path", "")).endswith(".gguf"))
            if total > 0:
                quants[matched_quant] = total
        except Exception:
            pass

    # Also check flat-layout files at root
    root_files = [e for e in entries
                  if e.get("type") == "file" and e.get("path", "").endswith(".gguf")]
    if root_files and not quants:
        # Group by detected quant
        for f in root_files:
            fname = f["path"]
            q = _detect_quant(fname)
            if q != "unknown":
                quants[q] = quants.get(q, 0) + f.get("size", 0)

    return quants


def auto_select_quant(hf_repo, vram_free_mb):
    """Pick the highest-quality quantization that fits in available VRAM.

    Reserves ~15% of VRAM for KV cache and runtime overhead.
    Returns quant name (e.g. 'q4_k_m') or 'q4_k_m' as fallback.
    """
    quants = _hf_available_quants(hf_repo)
    if not quants:
        print("[auto-quant] Could not determine available quants, defaulting to q4_k_m")
        return "q4_k_m"

    budget_bytes = vram_free_mb * 1024 * 1024 * 0.85  # 15% headroom

    # Sort by quality rank descending (best first)
    ranked = sorted(quants.items(),
                    key=lambda kv: _QUANT_RANK.index(kv[0]) if kv[0] in _QUANT_RANK else -1,
                    reverse=True)

    print(f"[auto-quant] VRAM budget: {vram_free_mb/1024:.1f} GB (85% of {vram_free_mb/1024:.1f} GB free)")
    for qname, qsize in ranked:
        size_gb = qsize / (1024**3)
        fits = "✓" if qsize <= budget_bytes else "✗"
        print(f"[auto-quant]   {qname:12s} {size_gb:7.1f} GB  {fits}")

    for qname, qsize in ranked:
        if qsize <= budget_bytes:
            print(f"[auto-quant] Selected: {qname} ({qsize/(1024**3):.1f} GB)")
            return qname

    # Nothing fits — pick smallest available
    smallest = min(quants.items(), key=lambda kv: kv[1])
    print(f"[auto-quant] Nothing fits budget, using smallest: {smallest[0]} ({smallest[1]/(1024**3):.1f} GB)")
    return smallest[0]


# Trusted GGUF publishers — prefer these over random community forks
_TRUSTED_GGUF_ORGS = {"lmstudio-community", "bartowski", "mradermacher", "unsloth"}

def _resolve_gguf_repo(hf_repo):
    """If hf_repo has no GGUF files, try the '-GGUF' suffixed repo."""
    if hf_repo.upper().endswith("-GGUF"):
        return hf_repo
    # Check _BUILTIN table first — known-good GGUF repos
    model_name = hf_repo.split("/")[-1] if "/" in hf_repo else hf_repo
    for repo, *_ in _BUILTIN:
        if model_name.lower() in repo.lower() and repo.upper().endswith("-GGUF"):
            print(f"[download] Matched builtin GGUF repo: {repo}")
            return repo
    gguf_files = _hf_repo_files(hf_repo)
    if gguf_files:
        return hf_repo
    # Try {org}/{model}-GGUF under same org
    gguf_repo = hf_repo + "-GGUF"
    gguf_files = _hf_repo_files(gguf_repo)
    if gguf_files:
        print(f"[download] Source repo has no GGUF files, using {gguf_repo}")
        return gguf_repo
    # Search HuggingFace — prefer trusted orgs over random community forks
    try:
        search_url = f"https://huggingface.co/api/models?search={urllib.parse.quote(model_name + ' GGUF')}&limit=20"
        req = urllib.request.Request(search_url)
        req.add_header("User-Agent", "ClusterFlock/0.1")
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read())
        candidates = []
        for r in results:
            rid = r.get("modelId", "")
            if rid.upper().endswith("-GGUF") and model_name.lower() in rid.lower():
                org = rid.split("/")[0].lower() if "/" in rid else ""
                candidates.append((rid, org))
        # Sort: original org first, trusted orgs second, others last
        src_org = (hf_repo.split("/")[0].lower() if "/" in hf_repo else "").lower()
        def rank(c):
            rid, org = c
            if org == src_org:
                return 0
            if org in _TRUSTED_GGUF_ORGS:
                return 1
            return 2
        candidates.sort(key=rank)
        for rid, _ in candidates:
            check = _hf_repo_files(rid)
            if check:
                print(f"[download] Source repo has no GGUF files, found {rid}")
                return rid
    except Exception:
        pass
    # No GGUF variant found — return original and let download fail with a clear error
    return hf_repo


def download_model(hf_repo, quant="q4_k_m", filename=None):
    """Download a GGUF model from HuggingFace into models/<org>/<repo>/.

    Strategy:
      1. huggingface_hub.snapshot_download (best: resume, auth, no symlinks)
      2. curl fallback with resume support

    If the given repo has no GGUF files, automatically tries the '-GGUF'
    suffixed repo under the same org (common convention for GGUF conversions).

    Returns path to the downloaded GGUF file.
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # Auto-resolve: if source repo has no GGUF files, try {org}/{model}-GGUF
    hf_repo = _resolve_gguf_repo(hf_repo)

    # Target directory: models/org/repo/
    parts = hf_repo.split("/")
    if len(parts) >= 2:
        dest_dir = MODELS_DIR / parts[0] / parts[1]
    else:
        dest_dir = MODELS_DIR / hf_repo
    dest_dir.mkdir(parents=True, exist_ok=True)

    # If a specific filename was requested, check if it already exists
    if filename:
        dest_path = dest_dir / filename
        if dest_path.exists():
            print(f"[download] Already exists: {dest_path}")
            return str(dest_path)

    # Check for any matching GGUF already downloaded (flat or subdir layout)
    pattern = _gguf_pattern(quant)
    existing = list(set(dest_dir.glob(pattern)) | set(dest_dir.rglob(pattern)))
    if not existing:
        existing = list(dest_dir.rglob("*.gguf"))
    if existing:
        existing.sort(key=lambda f: f.name)
        # For multi-shard, prefer returning the first shard (00001-of-N)
        for f in existing:
            if quant.lower() in f.name.lower() and "00001-of-" in f.name:
                print(f"[download] Already exists: {f}")
                return str(f)
        for f in existing:
            if quant.lower() in f.name.lower():
                print(f"[download] Already exists: {f}")
                return str(f)
        for f in existing:
            if "00001-of-" in f.name:
                print(f"[download] Already exists: {f}")
                return str(f)
        print(f"[download] Already exists: {existing[0]}")
        return str(existing[0])

    # Estimate expected size for progress tracking
    expected = 0
    quants = _hf_available_quants(hf_repo)
    if quant in quants:
        expected = quants[quant]
    _download_state.update(active=True, model=hf_repo.split("/")[-1],
                           expected_bytes=expected, dest_dir=str(dest_dir))

    try:
        # ── Method 1: huggingface_hub.snapshot_download ──────────────────
        if _HAS_HF_HUB:
            return _download_via_hub(hf_repo, quant, filename, dest_dir)

        # ── Method 2: curl fallback ─────────────────────────────────────
        print("[download] huggingface_hub not installed — using curl fallback")
        print("[download] (pip install huggingface_hub for better downloads)")
        return _download_via_curl(hf_repo, quant, filename, dest_dir)
    finally:
        _download_state.update(active=False, model="", expected_bytes=0, dest_dir="")


def _download_via_hub(hf_repo, quant, filename, dest_dir):
    """Download using huggingface_hub.snapshot_download."""
    hf_token = os.environ.get("HF_TOKEN")

    if filename:
        allow_pattern = [filename]
    else:
        # Include both flat (*Q4_K_M*.gguf) and directory (Q4_K_M/*.gguf) patterns
        flat = _gguf_pattern(quant)
        dir_prefix = quant.upper().replace("_", "_")  # e.g. Q4_K_M
        allow_pattern = [flat, f"{dir_prefix}/*.gguf"]

    print(f"[download] Fetching '{allow_pattern[0]}' from '{hf_repo}'...")
    if not hf_token:
        print("[download] ⚠ HF_TOKEN not set — gated models will fail")

    try:
        _snapshot_download(
            repo_id=hf_repo,
            allow_patterns=allow_pattern,
            local_dir=str(dest_dir),
            local_dir_use_symlinks=False,
            token=hf_token,
        )
        # Check if anything was actually downloaded
        if not list(dest_dir.rglob("*.gguf")):
            raise RuntimeError("No matching files downloaded")
    except Exception as e:
        # If the specific quant pattern fails, try broader Q4 match, then any GGUF
        if not filename:
            for fallback in ["*Q4_*.gguf", "Q4_*/*.gguf", "*.gguf", "*/*.gguf"]:
                if fallback in allow_pattern:
                    continue
                print(f"[download] Pattern '{allow_pattern[0]}' failed, trying '{fallback}'...")
                try:
                    _snapshot_download(
                        repo_id=hf_repo,
                        allow_patterns=[fallback],
                        local_dir=str(dest_dir),
                        local_dir_use_symlinks=False,
                        token=hf_token,
                    )
                    if list(dest_dir.rglob("*.gguf")):
                        break
                except Exception:
                    continue
            else:
                raise RuntimeError(f"Download failed for {hf_repo}: {e}")
        else:
            raise RuntimeError(f"Download failed for {hf_repo}/{filename}: {e}")

    # Find the downloaded file
    gguf_files = sorted(dest_dir.rglob("*.gguf"), key=lambda f: f.name)
    if not gguf_files:
        raise RuntimeError(f"No GGUF files found after download in {dest_dir}")

    # For multi-shard GGUF, return the first shard (00001-of-N)
    # llama-server auto-discovers subsequent shards from the first
    shard_pattern = re.compile(r'(\d{5})-of-(\d{5})')
    sharded = [(f, shard_pattern.search(f.name)) for f in gguf_files]
    first_shards = [(f, m) for f, m in sharded if m and m.group(1) == '00001']
    if first_shards:
        # Prefer the quant-matched first shard
        for f, m in first_shards:
            if quant.lower().replace("_", "") in f.name.lower().replace("_", "").replace("-", ""):
                result = f
                break
        else:
            result = first_shards[0][0]
        total_size = sum(ff.stat().st_size for ff in gguf_files
                         if quant.lower().replace("_", "") in ff.name.lower().replace("_", "").replace("-", ""))
        if not total_size:
            total_size = sum(ff.stat().st_size for ff in gguf_files)
        size_gb = round(total_size / (1024**3), 1)
        print(f"[download] ✓ {result.name} ({size_gb} GB across {len(gguf_files)} shards)")
        return str(result)

    # Single file — prefer quant-matched
    for f in gguf_files:
        if quant.lower().replace("_", "") in f.name.lower().replace("_", "").replace("-", ""):
            result = f
            break
    else:
        result = gguf_files[0]

    size_gb = round(result.stat().st_size / (1024**3), 1)
    print(f"[download] ✓ {result.name} ({size_gb} GB)")
    return str(result)


def _download_via_curl(hf_repo, quant, filename, dest_dir):
    """Fallback download using curl."""
    if not filename:
        print(f"[download] Resolving files for {hf_repo}...")
        files = _hf_repo_files(hf_repo)
        if not files:
            raise RuntimeError(f"No GGUF files found in {hf_repo}")
        filename = _pick_gguf_file(files, quant)
        if not filename:
            raise RuntimeError(f"No {quant} GGUF found in {hf_repo}. Available: {files}")

    dest_path = dest_dir / filename
    if dest_path.exists():
        print(f"[download] Already exists: {dest_path}")
        return str(dest_path)

    url = f"https://huggingface.co/{hf_repo}/resolve/main/{filename}"
    print(f"[download] Downloading {filename} from {hf_repo}...")
    print(f"[download]   Destination: {dest_path}")

    tmp_path = dest_path.with_suffix(".gguf.part")
    curl_cmd = [
        "curl", "-L", "-C", "-",
        "--progress-bar",
        "-o", str(tmp_path),
        url,
    ]

    # Pass HF_TOKEN if set
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        curl_cmd[1:1] = ["-H", f"Authorization: Bearer {hf_token}"]

    r = subprocess.run(curl_cmd, timeout=7200)
    if r.returncode != 0:
        raise RuntimeError(f"Download failed for {url}")

    tmp_path.rename(dest_path)
    size_gb = round(dest_path.stat().st_size / (1024**3), 1)
    print(f"[download] ✓ {filename} ({size_gb} GB)")
    return str(dest_path)


def delete_model(model_path):
    """Delete a downloaded model file."""
    p = Path(model_path)
    if p.exists():
        p.unlink()
        # Clean up empty parent dirs
        for parent in [p.parent, p.parent.parent]:
            try:
                if parent != MODELS_DIR and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                pass
        print(f"[models] Deleted {p.name}")


# ── Model Selection (bin-packing for Spark) ──────────────────────────────────

def _memory_estimate_mb(size_gb, params_b, ctx_length):
    """Estimate total memory needed: model weights + KV cache.

    With q4_0 KV cache (NVFP4-like), KV cache is ~4x smaller than f16.
    """
    weights_mb = size_gb * 1024
    # KV cache: params_B × bytes_per_token × context
    # With q4_0 cache: ~600 bytes per token per billion params (vs 2400 for f16)
    kv_cache_mb = (params_b * 600 * ctx_length) / (1024 * 1024)
    return weights_mb + kv_cache_mb


def pick_best_models(gpus, catalog, tight_pack=False):
    """Greedy bin-packing: select models that fit in available VRAM.

    For DGX Spark with unified memory, all memory is one big pool.

    Args:
        gpus: List of GPU dicts from hardware profiling.
        catalog: Model catalog from fetch_catalog().
        tight_pack: If True, pack multiple smaller models.

    Returns:
        List of (gpu_idx, gpu_info, model_entry) tuples.
    """
    if not gpus or not catalog:
        return []

    # Unified memory: sum all available
    is_unified = any(g.get("unified") for g in gpus)
    if is_unified:
        total_free_mb = gpus[0].get("vram_free_mb", 0)
    else:
        total_free_mb = sum(g.get("vram_free_mb", 0) for g in gpus)

    # Safety margin: 85% of free memory
    budget_mb = total_free_mb * 0.85
    assignments = []
    used_mb = 0

    # Sort catalog: largest first (greedy)
    sorted_catalog = sorted(catalog, key=lambda m: m.get("params_b", 0), reverse=True)

    for model in sorted_catalog:
        if model.get("size_gb", 0) == 0:
            continue
        # Use a conservative context for packing estimation
        est_ctx = 32768 if tight_pack else 65536
        mem_needed = _memory_estimate_mb(
            model["size_gb"], model.get("params_b", 0), est_ctx
        )
        # Apply 1.2x overhead multiplier
        mem_needed *= 1.2

        if used_mb + mem_needed <= budget_mb:
            assignments.append((0, gpus[0], model))
            used_mb += mem_needed
            if not tight_pack:
                break  # Single best model

    return assignments
