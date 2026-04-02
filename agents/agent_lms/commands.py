"""Command dispatcher: execute orchestrator commands.

Transport-agnostic — called by link.py regardless of PULL or PUSH mode.
Returns a result dict (or None for fire-and-forget actions).
"""

import json
import re
from pathlib import Path

from studio import load_model, unload_model, unload_all, complete, \
    start_download_and_load, get_job_status, benchmark, get_bench

_MODEL_RE = re.compile(r'^[\w./@:\-]+$')
_CONFIG = Path(__file__).parent / "cluster.json"


def _read_config():
    try:
        return json.loads(_CONFIG.read_text())
    except Exception:
        return {}


def execute(cmd):
    """Dispatch a command dict. Returns result dict or None."""
    action = cmd.get("action")
    if action == "unload_all":
        unload_all()
    elif action in ("load", "unload"):
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        if action == "load":
            load_model(mid, gpu=cmd.get("gpu_idx"), context_length=cmd.get("context_length"))
            # Auto-benchmark if no cached result
            if get_bench(mid) == 0:
                try:
                    perf = benchmark(mid)
                    print(f"  Benchmark: {perf['tokens_per_sec']} tok/s")
                except Exception as e:
                    print(f"  Benchmark failed: {e}")
        else:
            unload_model(mid)
    elif action == "download_and_load":
        mid = cmd.get("model_id", "")
        if not mid or not _MODEL_RE.match(mid):
            raise ValueError(f"invalid model_id: {mid!r}")
        job_id = start_download_and_load(mid, gpu=cmd.get("gpu_idx"),
                                         context_length=cmd.get("context_length"))
        return {"ok": True, "job_id": job_id}
    elif action == "prompt":
        messages = cmd.get("messages")
        if not messages:
            raise ValueError("prompt requires 'messages'")
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
        return complete(messages, cmd.get("model"),
                       max_tokens=cmd.get("max_tokens"),
                       generation_timeout=cmd.get("generation_timeout"),
                       **kwargs)
    else:
        raise ValueError(f"unknown action: {action}")
    return None



