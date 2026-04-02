"""Orchestrator: command queue, prompt broadcasting, lock state.

Manages pending commands per node (delivered via heartbeat responses)
and coordinates multi-node prompt tasks with result aggregation.
"""

import json
import math
import re
import secrets
import threading
import time
import urllib.request
import urllib.error

from registry import all_nodes, get_node
from catalog import is_graylisted

_lock = threading.Lock()

# node_id → [cmd_dict, ...]  —  drained by heartbeat handler
_commands = {}

# task_id → {status, expected, results: [...], created}
_tasks = {}

# Model selection lock — when True, agents skip auto-prime
_locked = False
# Tight packing — when True, bin-pack multiple models per GPU
_tight_pack = False


# ── Lock / config state ──────────────────────────────────────────────────

def is_locked():
    with _lock:
        return _locked


def set_locked(val):
    global _locked
    with _lock:
        _locked = bool(val)


def is_tight_pack():
    with _lock:
        return _tight_pack


def set_tight_pack(val):
    pass  # Tight packing disabled — always off


# ── Command queue ────────────────────────────────────────────────────────

def enqueue(node_id, cmd):
    """Push a command for a node. It will be included in the next heartbeat response."""
    with _lock:
        _commands.setdefault(node_id, []).append(cmd)


def drain(node_id):
    """Pop all pending commands for a node (called in heartbeat handler)."""
    with _lock:
        cmds = _commands.pop(node_id, [])
        return cmds


# ── Prompt broadcast ─────────────────────────────────────────────────────

def broadcast_prompt(prompt_text):
    """Send a prompt to every loaded model on every healthy node.

    Sends one command per loaded endpoint (model) so every model responds.
    Nodes with no loaded models get a single unspecified-model command
    to trigger auto-prime.  Returns (task_id, expected_count).
    """
    task_id = "task-" + secrets.token_hex(6)
    nodes = all_nodes()
    expected = 0
    base_cmd = {
        "action": "prompt",
        "task_id": task_id,
        "messages": [{"role": "user", "content": prompt_text}],
        "ttl": 300,
        "locked": is_locked(),
        "tight_pack": is_tight_pack(),
    }

    for n in nodes:
        if n.get("status") == "dead":
            continue
        endpoints = [ep for ep in n.get("endpoints", [])
                     if ep.get("status") == "ready" and ep.get("model")]
        if endpoints:
            for ep in endpoints:
                cmd = dict(base_cmd)
                cmd["model"] = ep["model"]
                enqueue(n["node_id"], cmd)
                expected += 1
        else:
            # No loaded models — send one to trigger auto-prime
            enqueue(n["node_id"], dict(base_cmd))
            expected += 1

    with _lock:
        _tasks[task_id] = {
            "status": "pending" if expected > 0 else "done",
            "expected": expected,
            "results": [],
            "created": time.time(),
        }

    return task_id, expected


def record_result(task_id, result, node_id=None, hostname=None):
    """Store a prompt result for a task (called when agent POSTs back)."""
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return False
        entry = dict(result)
        entry["node_id"] = node_id or ""
        entry["hostname"] = hostname or ""
        entry["status"] = "done"
        entry["received_at"] = time.time()
        task["results"].append(entry)
        if len(task["results"]) >= task["expected"]:
            task["status"] = "done"
        else:
            task["status"] = "running"
        return True


def get_task(task_id):
    """Return task state, or None."""
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return None
        # Timeout stale tasks — but only force-done if they have results,
        # otherwise let _wait_for_result's own timeout handle it.
        age = time.time() - task["created"]
        if task["status"] != "done" and age > 300 and task["results"]:
            task["status"] = "done"
        return dict(task)


def cleanup_old_tasks(max_age=600):
    """Remove tasks older than max_age seconds."""
    with _lock:
        cutoff = time.time() - max_age
        stale = [tid for tid, t in _tasks.items() if t["created"] < cutoff]
        for tid in stale:
            del _tasks[tid]


# ── Autoload planning ────────────────────────────────────────────────────

# VRAM overhead: actual usage ≈ 1.2× GGUF file size (weight expansion + KV buffers)
_VRAM_OVERHEAD = 1.2
# Safety margin: reserve 15% of total VRAM to keep host responsive
_VRAM_SAFETY = 0.85
# Conservative safety margin for devices with < 16 GB VRAM
_VRAM_SAFETY_SMALL = 0.75
# Minimum context length (tokens) — skip models that can't sustain this
_MIN_CONTEXT_TOKENS = 8192
# Estimated KV-cache bytes per billion parameters per token (conservative)
_KV_BYTES_PER_BP_PER_TOKEN = 10000


def _effective_safety(vram_mb):
    """Return the VRAM safety factor for a device.
    Devices with < 16 GB get a more conservative margin (0.75)
    to leave room for KV cache and OS/driver overhead."""
    return _VRAM_SAFETY_SMALL if vram_mb < 16384 else _VRAM_SAFETY


def _passes_min_context(file_size, vram_budget_bytes, model_name):
    """Check whether a model leaves enough VRAM for >= 8 K context.
    Estimates KV-cache cost from params parsed from the model name.
    Returns True when the check passes or when params can't be determined."""
    from catalog import _parse_params_from_name
    params_b = _parse_params_from_name(model_name or "")
    if not params_b:
        return True  # can't check — allow
    cost = int(file_size * _VRAM_OVERHEAD)
    remaining_after_load = vram_budget_bytes - cost
    min_kv = int(params_b * _KV_BYTES_PER_BP_PER_TOKEN * _MIN_CONTEXT_TOKENS)
    return remaining_after_load >= min_kv


def all_downloads():
    """Return deduplicated list of all downloaded models across the cluster."""
    nodes = all_nodes()
    models = {}
    for node in nodes:
        if node.get("status") == "dead":
            continue
        downloaded = node.get("downloaded") or []
        for m in downloaded:
            mid = m.get("id", "")
            if not mid or m.get("file_size", 0) <= 0:
                continue
            if mid not in models:
                models[mid] = {
                    "id": mid,
                    "name": m.get("name", mid),
                    "file_size": m["file_size"],
                    "params_b": m.get("params_b"),
                    "nodes": [],
                }
            elif m["file_size"] > models[mid]["file_size"]:
                models[mid]["file_size"] = m["file_size"]
            models[mid]["nodes"].append({
                "node_id": node["node_id"],
                "hostname": node.get("hostname", ""),
            })
    return sorted(models.values(), key=lambda m: m["file_size"], reverse=True)


def plan_autoload(clean_slate=False, priorities=None):
    """Plan optimal model loading for every GPU in the cluster.

    Returns a list of steps: [{node_id, hostname, gpu_idx, gpu_name,
    vram_mb, action, model_id, model_name, file_size}].
    Uses tight_pack setting to decide strategy.

    Only considers models actually downloaded on each node (reported via
    heartbeat 'downloaded' field) so lms load commands will succeed.

    Applies a 1.2× overhead multiplier on file_size and reserves 15% of
    total VRAM to prevent OOM and keep the host responsive.

    clean_slate: if True, assume all models will be unloaded first —
    uses total VRAM and ignores currently-loaded endpoints.

    priorities: optional list of model IDs to load first. Priority models
    are placed before non-priority models in the greedy sort so they get
    loaded on the best-fitting devices first, then remaining capacity is
    tight-packed with the rest.
    """
    priority_set = set(priorities or [])
    nodes = all_nodes()
    tight = is_tight_pack()
    steps = []

    for node in nodes:
        if node.get("status") == "dead":
            continue
        gpus = (node.get("hardware") or {}).get("gpu", [])
        endpoints = node.get("endpoints", [])
        gpu_metrics = (node.get("metrics") or {}).get("gpu", [])
        downloaded = node.get("downloaded") or []

        if not gpus or not downloaded:
            continue

        # Deduplicate downloaded list by model ID, keep largest variant
        seen_dl = {}
        for m in downloaded:
            mid = m.get("id", "")
            if mid and m.get("file_size", 0) > 0:
                if mid not in seen_dl or m["file_size"] > seen_dl[mid]["file_size"]:
                    seen_dl[mid] = m
        dl_sorted = sorted(seen_dl.values(),
                           key=lambda m: (m["id"] in priority_set, m["file_size"]),
                           reverse=True)

        # Skip graylisted models (unless explicitly prioritized)
        dl_sorted = [m for m in dl_sorted
                     if m["id"] in priority_set or not is_graylisted(m["id"])]

        # Build set of currently loaded model IDs on this node
        loaded_set = set()
        if not clean_slate:
            for ep in endpoints:
                if ep.get("model") and ep.get("status") == "ready":
                    loaded_set.add(ep["model"])

        # LM Studio spreads models across ALL visible GPUs on multi-GPU
        # systems — it cannot pin a model to a single GPU.
        # For multi-GPU non-unified nodes, combine into one VRAM pool.
        #
        # Linux agent: each GPU runs independently (pinned via
        # CUDA_VISIBLE_DEVICES). Never combine — no model splitting.
        has_unified = any(g.get("unified") for g in gpus)
        agent_independent = node.get("agent_type") == "linux"
        multi_gpu = len(gpus) > 1 and not has_unified and not agent_independent

        if multi_gpu:
            # Treat all GPUs as one combined pool
            total_vram_mb = sum(g.get("vram_total_mb", 0) for g in gpus)
            safety = _effective_safety(total_vram_mb)
            if clean_slate:
                remaining = int(total_vram_mb * safety) * 1024 * 1024
            else:
                total_free_mb = sum(
                    (gpu_metrics[i].get("vram_free_mb", 0)
                     if i < len(gpu_metrics) else 0)
                    for i in range(len(gpus))
                )
                remaining = int(total_free_mb * safety) * 1024 * 1024

            gpu_label = " + ".join(g.get("name", f"GPU {i}") for i, g in enumerate(gpus))
            num_desired = len(gpus) if not tight else 999  # at least one per GPU, or fill
            count = 0
            for m in dl_sorted:
                cost = int(m["file_size"] * _VRAM_OVERHEAD)
                if cost > remaining:
                    continue
                if not _passes_min_context(m["file_size"], remaining, m.get("name", m["id"])):
                    continue
                if m["id"] in loaded_set:
                    continue
                steps.append({
                    "node_id": node["node_id"],
                    "hostname": node.get("hostname", ""),
                    "gpu_idx": 0,
                    "gpu_name": gpu_label,
                    "vram_mb": total_vram_mb,
                    "action": "load",
                    "model_id": m["id"],
                    "model_name": m.get("name", m["id"]),
                    "file_size": m["file_size"],
                })
                loaded_set.add(m["id"])
                remaining -= cost
                count += 1
                if not tight and count >= num_desired:
                    break
        else:
            # Single GPU, unified memory, or linux agent — plan per-GPU
            for g_idx, gpu in enumerate(gpus):
                # CPU device (Linux agent): system RAM used as "VRAM"
                is_cpu_device = gpu.get("device") == "cpu"

                vram_mb = gpu.get("vram_total_mb", 0)
                if vram_mb <= 0:
                    continue

                # Budget = total * safety margin (clean slate) or free VRAM * safety
                safety = _effective_safety(vram_mb)
                if clean_slate:
                    remaining = int(vram_mb * safety) * 1024 * 1024
                else:
                    gm = gpu_metrics[g_idx] if g_idx < len(gpu_metrics) else {}
                    vram_free_mb = gm.get("vram_free_mb")
                    if vram_free_mb is not None:
                        remaining = int(vram_free_mb * safety) * 1024 * 1024
                    else:
                        remaining = int(vram_mb * safety) * 1024 * 1024

                step_gpu_idx = "cpu" if is_cpu_device else g_idx

                if tight:
                    # Fill GPU: pick models from largest to smallest until VRAM is full
                    for m in dl_sorted:
                        cost = int(m["file_size"] * _VRAM_OVERHEAD)
                        if cost > remaining:
                            continue
                        if not _passes_min_context(m["file_size"], remaining, m.get("name", m["id"])):
                            continue
                        if m["id"] in loaded_set:
                            continue
                        steps.append({
                            "node_id": node["node_id"],
                            "hostname": node.get("hostname", ""),
                            "gpu_idx": step_gpu_idx,
                            "gpu_name": gpu.get("name", "GPU " + str(g_idx)),
                            "vram_mb": vram_mb,
                            "action": "load",
                            "model_id": m["id"],
                            "model_name": m.get("name", m["id"]),
                            "file_size": m["file_size"],
                        })
                        loaded_set.add(m["id"])
                        remaining -= cost
                else:
                    # One model per GPU: pick biggest that fits
                    if not clean_slate:
                        gpu_has_model = False
                        for ep in endpoints:
                            if ep.get("status") == "ready":
                                ep_gpu = ep.get("gpu")
                                if is_cpu_device and ep_gpu == "cpu":
                                    gpu_has_model = True
                                    break
                                elif not is_cpu_device and ep_gpu == g_idx:
                                    gpu_has_model = True
                                    break
                        if gpu_has_model:
                            continue
                    for m in dl_sorted:
                        cost = int(m["file_size"] * _VRAM_OVERHEAD)
                        if cost > remaining:
                            continue
                        if not _passes_min_context(m["file_size"], remaining, m.get("name", m["id"])):
                            continue
                        if m["id"] in loaded_set:
                            continue
                        steps.append({
                            "node_id": node["node_id"],
                            "hostname": node.get("hostname", ""),
                            "gpu_idx": step_gpu_idx,
                            "gpu_name": gpu.get("name", "GPU " + str(g_idx)),
                            "vram_mb": vram_mb,
                            "action": "load",
                            "model_id": m["id"],
                            "model_name": m.get("name", m["id"]),
                            "file_size": m["file_size"],
                        })
                        loaded_set.add(m["id"])
                        break  # one per GPU

    return steps


def _ts():
    return time.strftime("%H:%M:%S")


# ── Autoload progress tracking ──────────────────────────────────────────

# Tracks the latest autoload run so the UI can poll for real status.
# _autoload_state = {status, steps: [{node_id, model_id, hostname, state, error}], started}
_autoload_state = None


def autoload_status():
    """Return current autoload progress, or None if no run is active."""
    with _lock:
        if _autoload_state is None:
            return None
        return dict(_autoload_state, steps=list(_autoload_state["steps"]))


def _autoload_update_step(node_id, model_id, state, error=None):
    """Update a specific step in the active autoload run."""
    with _lock:
        if _autoload_state is None:
            return
        for s in _autoload_state["steps"]:
            if s["node_id"] == node_id and s["model_id"] == model_id:
                s["state"] = state
                if error:
                    s["error"] = error
                break
        # Recompute overall status
        states = [s["state"] for s in _autoload_state["steps"]]
        if all(st in ("done", "failed") for st in states):
            _autoload_state["status"] = "done"
        elif any(st == "sending" for st in states):
            _autoload_state["status"] = "running"


def autoload_record_load_result(node_id, model_id, ok, error=None):
    """Called by push.py / heartbeat handler when a load command completes."""
    state = "done" if ok else "failed"
    _autoload_update_step(node_id, model_id, state, error)
    with _lock:
        if _autoload_state is None:
            return
        for s in _autoload_state["steps"]:
            if s["node_id"] == node_id and s["model_id"] == model_id:
                ts = time.strftime("%H:%M:%S")
                tag = "ok" if ok else f"FAIL {error}" if error else "done"
                print(f"[{ts}] autoload    {s.get('hostname','?'):20} {model_id}: {tag}")
                break


def autoload_check_heartbeat(node_id, endpoints):
    """Check if any newly-ready endpoints complete autoload steps (pull-mode fallback)."""
    with _lock:
        if _autoload_state is None or _autoload_state["status"] != "running":
            return
        ready_models = set()
        for ep in (endpoints or []):
            if ep.get("status") == "ready" and ep.get("model"):
                ready_models.add(ep["model"])
        for s in _autoload_state["steps"]:
            if s["node_id"] == node_id and s["state"] in ("queued", "sending"):
                if s["model_id"] in ready_models:
                    s["state"] = "done"
        # Recompute overall status
        states = [s["state"] for s in _autoload_state["steps"]]
        if all(st in ("done", "failed") for st in states):
            _autoload_state["status"] = "done"


def execute_autoload(priorities=None):
    """Unload all models on every node, then plan and enqueue optimal loads.

    Returns (plan, step_count) where step_count includes the unload commands.
    """
    global _autoload_state
    prio_label = f" (priorities: {len(priorities)})" if priorities else ""
    print(f"[{_ts()}] autoload ── starting{prio_label}")

    nodes = all_nodes()
    unloaded = 0
    for node in nodes:
        if node.get("status") == "dead":
            print(f"[{_ts()}] autoload    skip {node.get('hostname','?'):15} (dead)")
            continue
        enqueue(node["node_id"], {"action": "unload_all", "ttl": 120})
        unloaded += 1
        print(f"[{_ts()}] autoload    unload_all → {node.get('hostname','?')}")

    plan = plan_autoload(clean_slate=True, priorities=priorities)

    if not plan:
        print(f"[{_ts()}] autoload    no models to load (0 steps)")
        with _lock:
            _autoload_state = {
                "status": "done",
                "steps": [],
                "started": time.time(),
            }
        return plan, 0

    # Build tracking state
    tracked_steps = []
    for step in plan:
        tracked_steps.append({
            "node_id": step["node_id"],
            "model_id": step["model_id"],
            "hostname": step.get("hostname", ""),
            "model_name": step.get("model_name", step["model_id"]),
            "gpu_name": step.get("gpu_name", ""),
            "file_size": step.get("file_size", 0),
            "state": "queued",  # queued → sending → done / failed
            "error": None,
        })
    with _lock:
        _autoload_state = {
            "status": "running",
            "steps": tracked_steps,
            "started": time.time(),
        }

    # Group by hostname for readable output
    by_host = {}
    total_gb = 0
    for step in plan:
        h = step.get("hostname", "?")
        by_host.setdefault(h, []).append(step)
        total_gb += step.get("file_size", 0) / 1e9

    print(f"[{_ts()}] autoload ── plan: {len(plan)} model(s), {total_gb:.1f}GB total across {len(by_host)} node(s)")
    for host, host_steps in by_host.items():
        for s in host_steps:
            sz = s.get('file_size', 0) / 1e9
            print(f"[{_ts()}] autoload    {host:20} ← {s['model_name']:30} ({sz:.1f}GB) on {s.get('gpu_name','?')}")

    for step in plan:
        cmd = {
            "action": "load",
            "model_id": step["model_id"],
            "gpu_idx": step.get("gpu_idx"),
            "ttl": 600,
            "_autoload": True,  # marker for push.py to report back
        }
        # CPU device (Linux agent): send device="cpu" so agent routes correctly
        if step.get("gpu_idx") == "cpu":
            cmd["device"] = "cpu"
        enqueue(step["node_id"], cmd)

    print(f"[{_ts()}] autoload ── {len(plan)} load commands queued")
    return plan, len(plan)


# ── Benchmark Autoload ───────────────────────────────────────────────────

# _bench_autoload_state tracks the full benchmark autoload process.
# {status, target_tps, devices: [{node_id, hostname, gpu_idx, gpu_name,
#    vram_mb, phase, model_id, model_name, tps, message, attempts}],
#  log: [{ts, msg}], iteration, max_iterations, started}
_bench_autoload_state = None
_bench_autoload_thread = None

# VRAM budget fractions to try progressively
_BENCH_VRAM_FRACTIONS = [0.25, 0.50, 0.75, 1.00]
_BENCH_MAX_ITERATIONS = 40


def benchmark_autoload_status():
    """Return current benchmark autoload progress, or None."""
    with _lock:
        if _bench_autoload_state is None:
            return None
        return {
            "status": _bench_autoload_state["status"],
            "target_tps": _bench_autoload_state["target_tps"],
            "devices": list(_bench_autoload_state["devices"]),
            "log": list(_bench_autoload_state["log"][-50:]),
            "iteration": _bench_autoload_state["iteration"],
            "max_iterations": _bench_autoload_state["max_iterations"],
            "started": _bench_autoload_state["started"],
        }


def _bench_log(msg):
    """Append to benchmark autoload log."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] bench-al    {msg}")
    with _lock:
        if _bench_autoload_state is not None:
            _bench_autoload_state["log"].append({"ts": ts, "msg": msg})


def _bench_update_device(idx, **kw):
    """Update a device entry in the benchmark autoload state."""
    with _lock:
        if _bench_autoload_state is None:
            return
        for k, v in kw.items():
            _bench_autoload_state["devices"][idx][k] = v


def _send_command_to_node(node_id, cmd, timeout=600):
    """Send a command directly to a push-mode agent, or enqueue for pull.

    Returns the response dict for push-mode, or None for pull-mode.
    """
    node = get_node(node_id)
    if not node:
        return None
    address = node.get("address")
    token = node.get("orchestrator_token")
    if not address or not token:
        enqueue(node_id, cmd)
        return None  # pull-mode, async
    try:
        body = json.dumps(cmd).encode()
        req = urllib.request.Request(
            f"{address}/api/v1/command",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        _bench_log(f"command failed for {node_id[:8]}: {e}")
        return {"error": str(e)}


def _normalize_model_name(mid):
    """Extract a comparable base model name, stripping repo, quant, extension."""
    mid = mid.rsplit("/", 1)[-1]  # drop org/repo prefix
    mid = mid.replace(".gguf", "").replace("-GGUF", "")
    mid = re.sub(r'[-_.]Q\d+[-_A-Za-z0-9]*$', '', mid)  # strip quant suffix
    return mid.lower()


def _model_matches(model_id, ep_model):
    """Flexible model ID matching (catalog IDs vs agent endpoint IDs)."""
    if model_id in ep_model or ep_model in model_id:
        return True
    if model_id.replace("/", "_") in ep_model:
        return True
    # Normalize both to base model name and compare
    norm_id = _normalize_model_name(model_id)
    norm_ep = _normalize_model_name(ep_model)
    return norm_id and norm_ep and (norm_id in norm_ep or norm_ep in norm_id)


def _wait_for_endpoint_ready(node_id, model_id, timeout=300, gpu_idx=None):
    """Wait until an endpoint with the given model appears ready on the node.

    Returns (tokens_per_sec, True) on success, (0, False) on timeout.
    When gpu_idx is given, only match endpoints on that specific device.
    If model is ready but tps stays at 0 for 60s, returns early so caller
    can try an explicit benchmark command.
    """
    deadline = time.time() + timeout
    ready_since = None  # when we first saw model ready with tps=0
    while time.time() < deadline:
        node = get_node(node_id)
        if node:
            for ep in node.get("endpoints", []):
                if ep.get("status") == "ready":
                    ep_model = ep.get("model", "")
                    if _model_matches(model_id, ep_model):
                        # If gpu_idx specified, filter by device
                        # (only when endpoint reports device info)
                        if gpu_idx is not None:
                            ep_gpu = ep.get("gpu")
                            ep_dev = ep.get("device", "")
                            has_dev_info = ep_gpu is not None or ep_dev
                            if has_dev_info and ep_gpu != gpu_idx \
                               and ep_dev != str(gpu_idx) \
                               and ep_dev != f"gpu{gpu_idx}":
                                continue
                        tps = ep.get("tokens_per_sec", 0)
                        if tps and tps > 0:
                            return tps, True
                        # Model ready but no tps yet — track how long
                        if ready_since is None:
                            ready_since = time.time()
                        elif time.time() - ready_since > 60:
                            return 0, False  # give up waiting for auto-bench
        time.sleep(5)
    return 0, False


def _pick_model_for_budget(downloaded, budget_bytes, exclude_ids=None):
    """Pick the best (largest params_b) model fitting in budget.

    downloaded: list of model dicts with file_size, id, name, params_b
    budget_bytes: max file_size * overhead that must fit
    exclude_ids: set of model IDs to skip

    Returns model dict or None.
    """
    exclude = exclude_ids or set()
    candidates = []
    for m in downloaded:
        mid = m.get("id", "")
        fsize = m.get("file_size", 0)
        if fsize <= 0 or mid in exclude:
            continue
        if is_graylisted(mid):
            continue
        cost = int(fsize * _VRAM_OVERHEAD)
        if cost <= budget_bytes:
            if not _passes_min_context(fsize, budget_bytes, m.get("name", mid)):
                continue
            candidates.append(m)
    if not candidates:
        return None
    # Sort by file_size descending (biggest that fits = best quality)
    candidates.sort(key=lambda m: m.get("file_size", 0), reverse=True)
    return candidates[0]


def _catalog_model_for_budget(budget_bytes, exclude_ids=None):
    """Pick the best model from the nCore catalog that fits in budget.

    Used to suggest models for download if no suitable downloaded model exists.
    Returns catalog entry dict or None.
    """
    import catalog as catalog_mod
    exclude = exclude_ids or set()
    for m in catalog_mod.get_catalog():
        if m["id"] in exclude:
            continue
        if is_graylisted(m["id"]):
            continue
        cost = int(m.get("file_size", 0) * _VRAM_OVERHEAD)
        if 0 < cost <= budget_bytes:
            return m
    return None


def execute_benchmark_autoload(target_tps=50):
    """Start a benchmark autoload process in a background thread.

    For each device in the cluster:
    1. Start with 25% VRAM budget, find best fitting model
    2. Load it, benchmark it
    3. If tps < target or close (within 25%), keep it
    4. If tps >= target * 1.25, try bigger model (50% budget), repeat
    5. Up to 40 total iterations across all devices

    Returns immediately. Poll benchmark_autoload_status() for progress.
    """
    global _bench_autoload_state, _bench_autoload_thread

    with _lock:
        if (_bench_autoload_state is not None and
                _bench_autoload_state["status"] == "running"):
            return {"error": "benchmark autoload already running"}

    # Build device list from cluster
    nodes = all_nodes()
    tight = is_tight_pack()
    devices = []

    for node in nodes:
        if node.get("status") == "dead":
            continue
        gpus = (node.get("hardware") or {}).get("gpu", [])
        downloaded = node.get("downloaded") or []
        if not gpus:
            continue

        agent_independent = node.get("agent_type") == "linux"
        has_unified = any(g.get("unified") for g in gpus)
        multi_gpu = len(gpus) > 1 and not has_unified and not agent_independent

        if multi_gpu:
            # Combined pool — treat as single device
            total_vram_mb = sum(g.get("vram_total_mb", 0) for g in gpus)
            gpu_label = " + ".join(g.get("name", f"GPU {i}") for i, g in enumerate(gpus))
            devices.append({
                "node_id": node["node_id"],
                "hostname": node.get("hostname", ""),
                "gpu_idx": 0,
                "gpu_name": gpu_label,
                "vram_mb": total_vram_mb,
                "phase": "pending",
                "fraction_idx": 0,
                "model_id": None,
                "model_name": None,
                "tps": 0,
                "message": "Waiting...",
                "attempts": 0,
                "done": False,
                "downloaded": downloaded,
            })
        else:
            for g_idx, gpu in enumerate(gpus):
                is_cpu = gpu.get("device") == "cpu"
                vram_mb = gpu.get("vram_total_mb", 0)
                if vram_mb <= 0:
                    continue
                devices.append({
                    "node_id": node["node_id"],
                    "hostname": node.get("hostname", ""),
                    "gpu_idx": "cpu" if is_cpu else g_idx,
                    "gpu_name": gpu.get("name", f"GPU {g_idx}"),
                    "vram_mb": vram_mb,
                    "phase": "pending",
                    "fraction_idx": 0,
                    "model_id": None,
                    "model_name": None,
                    "tps": 0,
                    "message": "Waiting...",
                    "attempts": 0,
                    "done": False,
                    "downloaded": downloaded,
                })
            # If not tight_pack, only use first GPU per node (non-linux)
            if not tight and not agent_independent and len(gpus) > 1:
                devices = [d for d in devices
                           if d["node_id"] != node["node_id"] or
                           d == next((dd for dd in devices
                                      if dd["node_id"] == node["node_id"]), None)]

    if not devices:
        return {"error": "no devices available for benchmark autoload"}

    with _lock:
        _bench_autoload_state = {
            "status": "running",
            "target_tps": target_tps,
            "devices": devices,
            "log": [],
            "iteration": 0,
            "max_iterations": _BENCH_MAX_ITERATIONS,
            "started": time.time(),
        }

    _bench_autoload_thread = threading.Thread(
        target=_benchmark_autoload_worker,
        args=(target_tps,),
        daemon=True,
    )
    _bench_autoload_thread.start()

    return {"ok": True, "devices": len(devices), "target_tps": target_tps}


def _benchmark_autoload_worker(target_tps):
    """Background worker that runs the benchmark autoload process."""
    global _bench_autoload_state

    try:
        _bench_log(f"Starting benchmark autoload — target: {target_tps} tok/s")

        with _lock:
            devices = _bench_autoload_state["devices"]

        # First, unload everything on all involved nodes
        unloaded_nodes = set()
        for dev in devices:
            nid = dev["node_id"]
            if nid not in unloaded_nodes:
                _bench_log(f"Unloading all on {dev['hostname']}")
                _send_command_to_node(nid, {"action": "unload_all", "ttl": 120})
                unloaded_nodes.add(nid)
        time.sleep(3)  # Give agents time to unload

        iteration = 0

        for dev_idx, dev in enumerate(devices):
            if dev.get("done"):
                continue

            nid = dev["node_id"]
            hostname = dev["hostname"]
            vram_mb = dev["vram_mb"]
            gpu_idx = dev["gpu_idx"]
            downloaded = dev.get("downloaded", [])

            _bench_update_device(dev_idx, phase="active", message="Starting...")
            _bench_log(f"Device {dev_idx+1}/{len(devices)}: {hostname} {dev['gpu_name']} ({vram_mb}MB)")

            # Try each VRAM fraction
            tried_models = set()
            final_model = None
            final_model_name = None
            final_tps = 0
            prev_model_id = None  # track what to unload

            for frac_idx, fraction in enumerate(_BENCH_VRAM_FRACTIONS):
                if iteration >= _BENCH_MAX_ITERATIONS:
                    _bench_log(f"Reached max iterations ({_BENCH_MAX_ITERATIONS})")
                    break

                budget_mb = int(vram_mb * _effective_safety(vram_mb) * fraction)
                budget_bytes = budget_mb * 1024 * 1024
                budget_label = f"{int(fraction*100)}% VRAM ({budget_mb}MB)"

                _bench_update_device(dev_idx,
                                     fraction_idx=frac_idx,
                                     message=f"Finding model for {budget_label}...")
                _bench_log(f"  Trying {budget_label}")

                # Find best model that fits
                model = _pick_model_for_budget(downloaded, budget_bytes, tried_models)

                if not model:
                    # Try from catalog (would need download)
                    cat_model = _catalog_model_for_budget(budget_bytes, tried_models)
                    if cat_model:
                        _bench_update_device(dev_idx,
                                             message=f"Downloading {cat_model['name']}...")
                        _bench_log(f"  Downloading {cat_model['name']} ({cat_model.get('file_size',0)/1e9:.1f}GB)")

                        cmd = {
                            "action": "download_and_load",
                            "model_id": cat_model["id"],
                            "ttl": 1800,
                        }
                        if gpu_idx != 0 or dev.get("gpu_idx") == "cpu":
                            cmd["gpu_idx"] = gpu_idx
                        result = _send_command_to_node(nid, cmd, timeout=1800)
                        if result and result.get("error"):
                            _bench_log(f"  Download failed: {result['error']}")
                            _bench_update_device(dev_idx,
                                                 message=f"Download failed: {result.get('error','')[:60]}")
                            tried_models.add(cat_model["id"])
                            iteration += 1
                            continue

                        model_id = cat_model["id"]
                        model_name = cat_model.get("name", model_id)
                    else:
                        _bench_log(f"  No model available for {budget_label}")
                        if final_model:
                            break  # Keep what we have
                        _bench_update_device(dev_idx,
                                             message=f"No model fits {budget_label}")
                        continue
                else:
                    model_id = model["id"]
                    model_name = model.get("name", model_id)

                    _bench_update_device(dev_idx,
                                         model_id=model_id,
                                         model_name=model_name,
                                         message=f"Loading {model_name}...")
                    _bench_log(f"  Loading {model_name} ({model.get('file_size',0)/1e9:.1f}GB)")

                    # Unload current model on this device only (not all devices)
                    # Skip when prev_model_id is None — initial unload_all already cleared
                    if prev_model_id:
                        _send_command_to_node(nid, {"action": "unload", "model_id": prev_model_id, "ttl": 120})
                        time.sleep(2)

                    cmd = {"action": "load", "model_id": model_id, "ttl": 600}
                    if gpu_idx != 0 or dev.get("gpu_idx") == "cpu":
                        cmd["gpu_idx"] = gpu_idx
                    result = _send_command_to_node(nid, cmd, timeout=600)
                    if result and result.get("error"):
                        _bench_log(f"  Load failed: {result['error']}")
                        tried_models.add(model_id)
                        iteration += 1
                        _bench_update_device(dev_idx, attempts=dev.get("attempts", 0) + 1,
                                             message=f"Load failed, trying next...")
                        with _lock:
                            _bench_autoload_state["iteration"] = iteration
                        continue

                tried_models.add(model_id)
                prev_model_id = model_id
                iteration += 1
                with _lock:
                    _bench_autoload_state["iteration"] = iteration

                # Wait for model to be ready and benchmarked
                _bench_update_device(dev_idx,
                                     model_id=model_id,
                                     model_name=model_name,
                                     message=f"Waiting for {model_name} to load & benchmark...")
                _bench_log(f"  Waiting for load + benchmark...")

                tps, ready = _wait_for_endpoint_ready(nid, model_id, timeout=600,
                                                       gpu_idx=gpu_idx)

                if not ready:
                    # Try explicit benchmark command
                    _bench_update_device(dev_idx, message=f"Triggering benchmark...")
                    _bench_log(f"  Endpoint not reporting tps, sending benchmark command")
                    bench_result = _send_command_to_node(nid,
                        {"action": "benchmark", "ttl": 300}, timeout=300)
                    if bench_result and not bench_result.get("error"):
                        tps = bench_result.get("tokens_per_sec", 0)
                    if not tps:
                        _bench_log(f"  Benchmark failed/timeout for {model_name}")
                        _bench_update_device(dev_idx,
                                             tps=0,
                                             attempts=dev.get("attempts", 0) + 1,
                                             message=f"Benchmark failed, trying next...")
                        continue

                _bench_update_device(dev_idx, tps=tps,
                                     message=f"{model_name}: {tps:.1f} tok/s")
                _bench_log(f"  {model_name}: {tps:.1f} tok/s (target: {target_tps})")

                # Decision logic — keep if within ±15% of target (or below)
                if tps < target_tps * 1.15:
                    # Check if previous best was closer to target
                    if final_tps > 0:
                        dist_new = abs(tps - target_tps)
                        dist_prev = abs(final_tps - target_tps)
                        if dist_prev < dist_new:
                            _bench_log(f"  ↓ {tps:.1f} tok/s further from target than {final_tps:.1f} — reverting")
                            # Reload the previous best model
                            _send_command_to_node(nid, {"action": "unload", "model_id": model_id, "ttl": 120})
                            time.sleep(2)
                            cmd = {"action": "load", "model_id": final_model, "ttl": 600}
                            if gpu_idx != 0 or dev.get("gpu_idx") == "cpu":
                                cmd["gpu_idx"] = gpu_idx
                            _send_command_to_node(nid, cmd, timeout=600)
                            _bench_update_device(dev_idx, phase="done", done=True,
                                                 model_id=final_model,
                                                 model_name=final_model_name,
                                                 tps=final_tps,
                                                 message=f"✓ Reverted to {final_model_name}: {final_tps:.1f} tok/s")
                            break
                    final_model = model_id
                    final_model_name = model_name
                    final_tps = tps
                    _bench_log(f"  ✓ Keeping {model_name} ({tps:.1f} tok/s)")
                    _bench_update_device(dev_idx, phase="done", done=True,
                                         message=f"✓ {model_name}: {tps:.1f} tok/s")
                    break
                else:
                    # Faster than needed — try a bigger model
                    final_model = model_id
                    final_model_name = model_name
                    final_tps = tps
                    _bench_log(f"  ↑ {tps:.1f} tok/s > {target_tps*1.15:.0f} threshold — trying bigger")
                    _bench_update_device(dev_idx,
                                         message=f"{model_name}: {tps:.1f} tok/s — trying bigger...")
            else:
                # Exhausted all fractions
                if final_model:
                    # If last tried model differs from best, reload best
                    if prev_model_id and prev_model_id != final_model:
                        _bench_log(f"  Reloading best model: {final_model_name or final_model}")
                        _send_command_to_node(nid, {"action": "unload", "model_id": prev_model_id, "ttl": 120})
                        time.sleep(2)
                        cmd = {"action": "load", "model_id": final_model, "ttl": 600}
                        if gpu_idx != 0 or dev.get("gpu_idx") == "cpu":
                            cmd["gpu_idx"] = gpu_idx
                        _send_command_to_node(nid, cmd, timeout=600)
                    _bench_update_device(dev_idx, phase="done", done=True,
                                         model_id=final_model,
                                         model_name=final_model_name,
                                         tps=final_tps,
                                         message=f"✓ Best: {final_model_name or final_model}: {final_tps:.1f} tok/s")
                    _bench_log(f"  ✓ Best result: {final_model_name or final_model} at {final_tps:.1f} tok/s")
                else:
                    _bench_update_device(dev_idx, phase="failed", done=True,
                                         message="No suitable model found")
                    _bench_log(f"  ✗ No suitable model found for {hostname}")

        # Done
        _bench_log(f"Benchmark autoload complete — {iteration} iterations")
        with _lock:
            _bench_autoload_state["status"] = "done"
            _bench_autoload_state["iteration"] = iteration

    except Exception as e:
        _bench_log(f"ERROR: {e}")
        with _lock:
            if _bench_autoload_state:
                _bench_autoload_state["status"] = "error"
