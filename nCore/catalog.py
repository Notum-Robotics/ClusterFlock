"""Model catalog: HuggingFace + built-in models, cached for UI dropdowns.

Also manages the model graylist — models that have exhibited defective
behaviour (e.g. returning empty content).  Graylisted models are
discouraged from autoloading and benchmarking and displayed with a
warning indicator in the UI.
"""

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

HF_API = "https://huggingface.co/api/models"
_CATALOG_JSON = Path(__file__).resolve().parent.parent / "model_catalog.json"
_GRAYLIST_JSON = Path(__file__).resolve().parent.parent / "model_graylist.json"

_lock = threading.Lock()
_catalog = []
_fetched_at = 0
_TTL = 600  # re-fetch every 10 min


def _estimate_memory(params_b, file_size_bytes, max_ctx):
    return file_size_bytes + int(params_b * 600 * max_ctx)


def _make_entry(m):
    file_bytes = int(m["size_gb"] * (1024 ** 3))
    params = float(m["params_b"])
    return {
        "id": m["id"], "name": m["name"], "params_b": params,
        "file_size": file_bytes, "max_ctx": m["max_ctx"],
        "total_mem": _estimate_memory(params, file_bytes, m["max_ctx"]),
    }


def _parse_params_from_name(name):
    m = re.search(r'(?:^|[-_ ])(\d+\.?\d*)\s*B(?:[-_ ]|$)', name, re.I)
    return float(m.group(1)) if m else 0.0


_NON_TEXT_TAGS = ("-vl-", "-vl.", "_vl_", "_vl.", "vl-", "vision",
                  "-mm-", "multimodal", "pixtral", "llava", "cogvlm",
                  "minicpm-v", "internvl", "moondream")

def _is_non_text_model(name):
    """Return True if model name suggests a non-text (vision/multimodal) model."""
    low = name.lower()
    return any(tag in low for tag in _NON_TEXT_TAGS)


def _builtin_catalog():
    try:
        raw = json.loads(_CATALOG_JSON.read_text())
        return [_make_entry(m) for m in raw]
    except Exception as e:
        print(f"[catalog] Failed to load {_CATALOG_JSON}: {e}")
        return []


def _hf_catalog(limit=50):
    url = (f"{HF_API}?author=lmstudio-community"
           f"&sort=downloads&direction=-1&limit={limit}&filter=gguf")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ClusterFlock-nCore/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            models = json.loads(resp.read())
    except Exception as e:
        print(f"[catalog] HuggingFace fetch failed: {e}")
        return []

    catalog = []
    for m in models:
        model_id = m.get("id", "")
        name = model_id.split("/")[-1].replace("-GGUF", "")
        if any(skip in name.lower() for skip in ("mmproj", "embed", "mlx")):
            continue
        if _is_non_text_model(name):
            continue
        params_b = _parse_params_from_name(name)
        if not params_b:
            continue
        size_gb = round(params_b * 0.6, 1)
        catalog.append(_make_entry({
            "id": model_id, "name": name, "size_gb": size_gb,
            "params_b": params_b, "max_ctx": 131072,
        }))
    return catalog


def refresh():
    """Fetch fresh catalog from builtin + HuggingFace. Called on startup."""
    global _catalog, _fetched_at
    builtin = _builtin_catalog()
    hf = _hf_catalog()
    seen = set()
    merged = []
    for source in [builtin, hf]:
        for m in source:
            if m["id"] not in seen:
                seen.add(m["id"])
                merged.append(m)
    merged.sort(key=lambda c: c["file_size"], reverse=True)
    with _lock:
        _catalog = merged
        _fetched_at = time.time()
    print(f"[catalog] {len(merged)} models ({len(builtin)} builtin, {len(hf)} HuggingFace)")


def get_catalog():
    """Return cached catalog merged with models loaded on nodes."""
    from registry import all_nodes
    with _lock:
        base = list(_catalog)
    known_ids = {m["id"] for m in base}

    # Add models currently loaded on agents that aren't in the catalog
    for node in all_nodes():
        for ep in node.get("endpoints", []):
            model_key = ep.get("model", "")
            if model_key and model_key not in known_ids:
                if _is_non_text_model(model_key):
                    continue
                known_ids.add(model_key)
                base.append({
                    "id": model_key,
                    "name": model_key.split("/")[-1],
                    "params_b": 0,
                    "file_size": 0,
                    "max_ctx": 0,
                    "total_mem": 0,
                    "loaded": True,
                })
    return base


def models_for_vram(vram_mb):
    """Return catalog entries that fit in given VRAM, largest first."""
    budget = vram_mb * (1024 ** 2)
    return [m for m in get_catalog() if m["total_mem"] <= budget]


# ── Model graylist ───────────────────────────────────────────────────────

_graylist: dict = {}  # model_id → {reason, added_at, count}


def _load_graylist():
    """Load graylist from disk. Called once at import time."""
    global _graylist
    try:
        _graylist = json.loads(_GRAYLIST_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _graylist = {}


def _save_graylist():
    """Persist graylist to disk."""
    try:
        _GRAYLIST_JSON.write_text(json.dumps(_graylist, indent=2) + "\n")
    except Exception as e:
        print(f"[catalog] graylist save failed: {e}")


def graylist_add(model_id, reason="defective behaviour"):
    """Add a model to the graylist (or increment its count)."""
    with _lock:
        if model_id in _graylist:
            _graylist[model_id]["count"] = _graylist[model_id].get("count", 1) + 1
            _graylist[model_id]["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        else:
            _graylist[model_id] = {
                "reason": reason,
                "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "count": 1,
            }
            print(f"[catalog] graylisted model: {model_id} — {reason}")
        _save_graylist()


def graylist_remove(model_id):
    """Remove a model from the graylist."""
    with _lock:
        if model_id in _graylist:
            del _graylist[model_id]
            _save_graylist()
            print(f"[catalog] un-graylisted model: {model_id}")
            return True
    return False


def is_graylisted(model_id):
    """Check if a model is graylisted."""
    with _lock:
        return model_id in _graylist


def get_graylist():
    """Return a copy of the graylist dict."""
    with _lock:
        return dict(_graylist)


_load_graylist()
