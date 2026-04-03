"""Model quality scoring, composite scores, generation limits, and context budgeting."""

import math
import re

from registry import get_node

from .state import (
    _QUALITY_TIERS,
    _COMPLEX_TASK_KEYWORDS,
    _SIMPLE_TASK_KEYWORDS,
    _CONTEXT_BUDGET_FRACTION,
    _CHARS_PER_TOKEN,
    _MIN_CONTEXT_BUDGET,
)


# ── Model quality scoring ────────────────────────────────────────────────

def _model_quality_tier(model_name):
    """Return 1-3 quality rating based on model size."""
    if not model_name:
        return 1
    lower = model_name.lower()

    # Try regex extraction first: find NNb pattern (e.g., 120b, 70b, 8b, 0.5b)
    size_match = re.search(r'(\d+\.?\d*)\s*b(?:[^a-z]|$)', lower)
    if size_match:
        params_b = float(size_match.group(1))
        if params_b >= 27:
            return 3
        if params_b >= 7:
            return 2
        return 1

    # Fallback: static dict (for edge cases)
    for pattern, tier in _QUALITY_TIERS.items():
        if pattern in lower:
            return tier

    # Default: if name suggests instruct/chat, bump to 2
    if any(kw in lower for kw in ("instruct", "chat", "it")):
        return 2
    # Known large models without explicit size
    if any(kw in lower for kw in ("nemotron", "command-r", "dbrx", "mixtral", "grok", "minimax")):
        return 3
    return 1


def _composite_score(toks_per_sec, model_name, context_length=0):
    """Composite score prioritizing intelligence and context over speed.
    Showrunner orchestrates — reasoning quality matters most, speed least.
    Formula: tier³ × ctx_bonus² × speed_bonus
      - tier³: massively favors smarter models (tier3=27 vs tier2=8 vs tier1=1)
      - ctx_bonus²: large context is critical for tracking multi-agent missions
      - speed_bonus: minor sqrt factor so speed is a tiebreaker, not a dominator"""
    tier = _model_quality_tier(model_name)
    ctx = max(context_length or 4096, 4096)
    ctx_bonus = 1.0 + math.log2(ctx / 4096) * 0.5
    speed_bonus = 1.0 + math.log2(max(toks_per_sec or 1.0, 1.0)) * 0.1
    return (tier ** 3) * (ctx_bonus ** 2) * speed_bonus


# ── Smart agent-task matching ────────────────────────────────────────────

def _score_task_complexity(goal_text):
    """Score task complexity 1-3 based on goal keywords and length.
    1 = simple (file ops, lookups), 2 = medium, 3 = complex (reasoning, generation)."""
    if not goal_text:
        return 2
    lower = goal_text.lower()
    words = lower.split()
    complex_hits = sum(1 for w in words if w in _COMPLEX_TASK_KEYWORDS)
    simple_hits = sum(1 for w in words if w in _SIMPLE_TASK_KEYWORDS)

    if complex_hits >= 2 or len(words) > 100:
        return 3
    if complex_hits >= 1 and simple_hits == 0:
        return 2
    if simple_hits >= 2 and complex_hits == 0:
        return 1
    return 2


def _find_better_agent(mission, current_agent, task_complexity):
    """Find a more capable available agent for a complex task.
    Returns (agent_name, reason) or (None, None)."""
    if task_complexity < 3:
        return None, None

    current_tier = _model_quality_tier(current_agent.model)
    if current_tier >= 2:
        return None, None

    best_name = None
    best_tier = current_tier
    for name, agent in mission.flock.items():
        if agent.status != "available":
            continue
        tier = _model_quality_tier(agent.model)
        if tier > best_tier:
            best_tier = tier
            best_name = name

    if best_name:
        return (best_name,
                f"{current_agent.name} is tier-{current_tier} (small model) for a complex task; "
                f"{best_name} (tier-{best_tier}) is available and better suited")
    return None, None


# ── Endpoint helpers ─────────────────────────────────────────────────────

def _get_endpoint_tps(node_id, model):
    """Get tokens_per_sec for a specific endpoint."""
    node = get_node(node_id)
    if not node:
        return 0
    for ep in node.get("endpoints", []):
        if ep.get("model") == model:
            return ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0
    return 0


def _get_endpoint_ctx(node_id, model):
    """Get loaded context_length for a specific endpoint."""
    node = get_node(node_id)
    if not node:
        return 0
    for ep in node.get("endpoints", []):
        if ep.get("model") == model:
            return ep.get("context_length") or 0
    return 0


# ── Generation limits ────────────────────────────────────────────────────

def _generation_limits(node_id, model, role="worker", overrides=None):
    """Calculate (max_tokens, generation_timeout, wait_timeout) for a prompt.

    Auto-adapts to any model based on context length, speed, and quality tier.

    Roles
    ─────
    "showrunner" — No generation cap. Full model context. 10-min request timeout.
    "worker"     — Algorithmic defaults scaled to model tier / speed / context.
                   Showrunner can override per-dispatch via constraints.
    "utility"    — Compact limits for ancillary calls (compaction, naming).

    overrides (from dispatch constraints)
    ─────────
    max_tokens         — explicit per-request token limit (-1 = unlimited)
    generation_timeout — explicit per-request timeout in seconds
    no_gen_limit       — if truthy, promote worker to showrunner-grade limits
    """
    ctx = _get_endpoint_ctx(node_id, model) or 32768
    tps = _get_endpoint_tps(node_id, model) or 20
    tier = _model_quality_tier(model)
    overrides = overrides or {}

    if role == "showrunner" or overrides.get("no_gen_limit"):
        max_tokens = ctx
        gen_timeout = 600  # 10 minutes hard cap

    elif role == "utility":
        max_tokens = max(2048, ctx // 8)
        gen_timeout = max(120, int(max_tokens / max(tps, 1) * 1.5))
        gen_timeout = min(gen_timeout, 300)  # 5 min cap

    else:  # "worker"
        if tier >= 3:
            max_tokens = max(8192, ctx // 3)
        elif tier >= 2:
            max_tokens = max(8192, ctx // 4)
        else:
            max_tokens = max(4096, ctx // 6)
        gen_timeout = max(180, int(max_tokens / max(tps, 1) * 1.5))
        gen_timeout = min(gen_timeout, 600)  # 10 min cap

    # ── Explicit overrides from showrunner dispatch constraints ──
    if "max_tokens" in overrides:
        v = int(overrides["max_tokens"])
        if v == -1:
            max_tokens = ctx  # -1 = unlimited
        elif v > 0:
            max_tokens = v
    if "generation_timeout" in overrides:
        v = int(overrides["generation_timeout"])
        if v > 0:
            gen_timeout = min(v, 600)  # hard cap 10 min per request

    wait_timeout = int(gen_timeout * 1.3) + 30

    return max_tokens, gen_timeout, wait_timeout


# ── Dynamic context budget ───────────────────────────────────────────────

def _context_budget(context_length):
    """Calculate character budget for Showrunner content based on loaded context_length."""
    ctx = max(context_length or 4096, 4096)
    budget = int(ctx * _CONTEXT_BUDGET_FRACTION * _CHARS_PER_TOKEN)
    return max(budget, _MIN_CONTEXT_BUDGET)


def _estimate_tokens(messages):
    """Estimate total token count for a list of chat messages.
    Uses chars / _CHARS_PER_TOKEN + small per-message overhead."""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        total += len(content) // _CHARS_PER_TOKEN + 4
    return total


def _is_context_overflow(result):
    """Check if a result dict represents a context-size overflow from llama-server.
    Returns (True, n_prompt_tokens, n_ctx) if overflow, else (False, 0, 0)."""
    if not result or not result.get("_agent_error"):
        return False, 0, 0
    err = result.get("error", "")
    if "exceed_context_size_error" in err or "exceeds the available context size" in err:
        try:
            m = re.search(r'"n_prompt_tokens"\s*:\s*(\d+)', err)
            n_prompt = int(m.group(1)) if m else 0
            m = re.search(r'"n_ctx"\s*:\s*(\d+)', err)
            n_ctx = int(m.group(1)) if m else 0
            return True, n_prompt, n_ctx
        except Exception:
            return True, 0, 0
    return False, 0, 0


def _estimate_conversation_tokens(mission):
    """Estimate total token weight of the showrunner's conversation history."""
    total = 0
    for msg in mission.conversation:
        content = msg.get("content") or ""
        total += len(content) // _CHARS_PER_TOKEN + 4
    return total


def _scaled_limits(context_length):
    """Return a dict of dynamically scaled limits based on context_length.
    No hard caps — use the full proportional budget from the model's context window."""
    budget = _context_budget(context_length)
    return {
        "read_file_max": budget // 4,
        "action_result_max": budget // 6,
        "total_results_max": budget // 3,
        "agent_result_max": budget // 8,
        "smart_truncate_max": budget // 5,
        "search_max": budget // 6,
        "conversation_window": max(4, budget // 8000),
    }
