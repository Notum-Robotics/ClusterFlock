"""Model ranking and endpoint selection.

Shared module for scoring, showrunner election, and endpoint selection
strategies.  Imported by oapi.py and potentially mission.py.
"""

import math
import re
import time

from registry import all_nodes, get_node
from catalog import is_graylisted

# Quality tiers (keep in sync with mission.py)
_QUALITY_TIERS = {
    "120b": 3, "70b": 3, "72b": 3, "65b": 3, "34b": 3, "35b": 3, "32b": 3, "27b": 3,
    "14b": 2, "13b": 2, "12b": 2, "8b": 2, "7b": 2, "9b": 2,
    "4b": 1, "3b": 1, "2b": 1, "1b": 1, "0.5b": 1, "0.6b": 1,
}


def is_vl_model(model_name):
    """Return True if model is a vision/VL model (unsuitable for structured text tasks)."""
    if not model_name:
        return False
    lower = model_name.lower()
    return any(tag in lower for tag in ("-vl-", "-vl.", "_vl_", "_vl.", "vl-", "vision"))


def model_quality_tier(model_name):
    """Return 1-3 quality rating based on model size."""
    if not model_name:
        return 1
    lower = model_name.lower()

    size_match = re.search(r'(\d+\.?\d*)\s*b(?:[^a-z]|$)', lower)
    if size_match:
        params_b = float(size_match.group(1))
        if params_b >= 27:
            return 3
        if params_b >= 7:
            return 2
        return 1

    for pattern, tier in _QUALITY_TIERS.items():
        if pattern in lower:
            return tier

    if any(kw in lower for kw in ("instruct", "chat", "it")):
        return 2
    if any(kw in lower for kw in ("nemotron", "command-r", "dbrx", "mixtral", "grok", "minimax")):
        return 3
    return 1


def composite_score(toks_per_sec, model_name, context_length=0):
    """Composite quality × speed × context score (used for showrunner election)."""
    tier = model_quality_tier(model_name)
    ctx = max(context_length or 4096, 4096)
    ctx_bonus = 1.0 + math.log2(ctx / 4096) * 0.25
    return (toks_per_sec or 1.0) * (tier * tier) * ctx_bonus


def collect_ready_endpoints():
    """All ready endpoints across the cluster.
    Returns list of {node_id, hostname, model, context_length, toks_per_sec}.
    """
    nodes = all_nodes()
    endpoints = []
    for node in nodes:
        if node.get("status") == "dead":
            continue
        for ep in node.get("endpoints", []):
            if ep.get("status") != "ready" or not ep.get("model"):
                continue
            if is_vl_model(ep["model"]):
                continue
            endpoints.append({
                "node_id": node["node_id"],
                "hostname": node.get("hostname", ""),
                "model": ep["model"],
                "context_length": ep.get("context_length") or 4096,
                "toks_per_sec": ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0,
                "graylisted": is_graylisted(ep["model"]),
            })
    return endpoints


# ── Showrunner election ──────────────────────────────────────────────────

def elect_showrunner(exclude_node_id=None, min_tier=2):
    """Pick the best model as Showrunner (highest composite score, tier >= min_tier).
    Prefers models from starred nodes; falls back to all nodes if none qualify.
    Returns dict {node_id, model, score, hostname, context_length, toks_per_sec} or None.
    """
    import orchestrator as orch_mod
    stars = orch_mod.starred_nodes()

    def _best_from(nodes, only_starred=False):
        best = None
        best_score = -1
        for node in nodes:
            if node.get("status") == "dead":
                continue
            if exclude_node_id and node["node_id"] == exclude_node_id:
                continue
            if only_starred and node["node_id"] not in stars:
                continue
            for ep in node.get("endpoints", []):
                if ep.get("status") != "ready" or not ep.get("model"):
                    continue
                if is_vl_model(ep["model"]):
                    continue
                if is_graylisted(ep["model"]):
                    continue
                tier = model_quality_tier(ep["model"])
                if tier < min_tier:
                    continue
                tps = ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 10
                ctx = ep.get("context_length") or 0
                score = composite_score(tps, ep["model"], ctx)
                if score > best_score:
                    best_score = score
                    best = {
                        "node_id": node["node_id"],
                        "model": ep["model"],
                        "score": score,
                        "hostname": node.get("hostname", ""),
                        "context_length": ctx,
                        "toks_per_sec": tps,
                    }
        return best

    nodes = all_nodes()
    if stars:
        result = _best_from(nodes, only_starred=True)
        if result:
            return result
    return _best_from(nodes)


# ── Speed selection ──────────────────────────────────────────────────────

def select_fastest(endpoints, required_context=0):
    """Pick the fastest endpoint whose context window fits the requirement.
    Returns endpoint dict or None.
    """
    required = required_context or 0
    candidates = [ep for ep in endpoints
                  if ep["context_length"] >= required] if required else endpoints
    if not candidates:
        candidates = endpoints  # fallback: ignore context requirement
    if not candidates:
        return None
    return max(candidates, key=lambda ep: ep["toks_per_sec"])


# ── Manual selection ─────────────────────────────────────────────────────

def select_manual(endpoints, selector):
    """Pick a specific endpoint.  selector can be 'model@hostname' or just 'model'."""
    if "@" in selector:
        model, hostname = selector.rsplit("@", 1)
        for ep in endpoints:
            if ep["model"] == model and ep["hostname"] == hostname:
                return ep
    for ep in endpoints:
        if ep["model"] == selector:
            return ep
    return None
