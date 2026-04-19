"""Model quality scoring, composite scores, generation limits, and context budgeting."""

import math
import re

from registry import get_node

from .state import (
    _QUALITY_TIERS,
    _CONTEXT_BUDGET_FRACTION,
    _CHARS_PER_TOKEN,
    _MIN_CONTEXT_BUDGET,
)


def model_size_label(model_name):
    if not model_name:
        return ""
    m = re.search(r'(\d+\.?\d*)\s*b(?:[^a-z]|$)', model_name.lower())
    if m:
        raw = float(m.group(1))
        return f"{int(raw)}B" if raw == int(raw) else f"{raw}B"
    return ""


def model_quality_tier(model_name):
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
    tier = model_quality_tier(model_name)
    ctx = max(context_length or 4096, 4096)
    ctx_bonus = 1.0 + math.log2(ctx / 4096) * 0.5
    speed_bonus = 1.0 + math.log2(max(toks_per_sec or 1.0, 1.0)) * 0.1
    return (tier ** 3) * (ctx_bonus ** 2) * speed_bonus


def score_task_complexity(goal_text):
    if not goal_text:
        return 2
    lower = goal_text.lower()
    words = lower.split()
    _COMPLEX = frozenset({
        "write", "implement", "create", "build", "design", "architect", "develop",
        "analyze", "research", "report", "debug", "refactor", "optimize",
        "generate", "compose", "synthesize", "evaluate", "review", "plan",
    })
    _SIMPLE = frozenset({
        "copy", "move", "list", "grep", "find", "format", "rename", "delete",
        "count", "check", "verify", "read", "fetch", "download", "install",
    })
    complex_hits = sum(1 for w in words if w in _COMPLEX)
    simple_hits = sum(1 for w in words if w in _SIMPLE)
    if complex_hits >= 2 or len(words) > 100:
        return 3
    if complex_hits >= 1 and simple_hits == 0:
        return 2
    if simple_hits >= 2 and complex_hits == 0:
        return 1
    return 2


def find_better_agent(mission, current_agent, task_complexity):
    if task_complexity < 3:
        return None, None
    current_tier = model_quality_tier(current_agent.model)
    if current_tier >= 2:
        return None, None
    best_name, best_tier = None, current_tier
    for name, agent in mission.flock.items():
        if agent.status != "available":
            continue
        tier = model_quality_tier(agent.model)
        if tier > best_tier:
            best_tier = tier
            best_name = name
    if best_name:
        return (best_name,
                f"{current_agent.name} is tier-{current_tier} for complex task; "
                f"{best_name} (tier-{best_tier}) is better suited")
    return None, None


# ── Endpoint helpers ─────────────────────────────────────────────────────

def get_endpoint_tps(node_id, model):
    node = get_node(node_id)
    if not node:
        return 0
    for ep in node.get("endpoints", []):
        if ep.get("model") == model:
            return ep.get("tokens_per_sec") or ep.get("toks_per_sec") or 0
    return 0


def get_endpoint_ctx(node_id, model):
    node = get_node(node_id)
    if not node:
        return 0
    for ep in node.get("endpoints", []):
        if ep.get("model") == model:
            return ep.get("context_length") or 0
    return 0


# ── Generation limits ────────────────────────────────────────────────────

def generation_limits(node_id, model, role="worker", overrides=None):
    ctx = get_endpoint_ctx(node_id, model) or 32768
    tps = get_endpoint_tps(node_id, model) or 20
    tier = model_quality_tier(model)
    overrides = overrides or {}

    if role == "showrunner" or overrides.get("no_gen_limit"):
        gen_timeout = 1800
        max_tokens = min(ctx, int(tps * gen_timeout * 0.9))
        max_tokens = max(max_tokens, 8192)
    elif role == "utility":
        max_tokens = max(2048, ctx // 8)
        gen_timeout = max(240, int(max_tokens / max(tps, 1) * 1.5))
        gen_timeout = min(gen_timeout, 600)
    else:
        if tier >= 3:
            max_tokens = max(8192, ctx // 3)
        elif tier >= 2:
            max_tokens = max(8192, ctx // 4)
        else:
            max_tokens = max(4096, ctx // 6)
        gen_timeout = max(360, int(max_tokens / max(tps, 1) * 1.5))
        gen_timeout = min(gen_timeout, 1200)

    if "max_tokens" in overrides:
        v = int(overrides["max_tokens"])
        if v == -1:
            max_tokens = ctx
        elif v > 0:
            max_tokens = v
    if "generation_timeout" in overrides:
        v = int(overrides["generation_timeout"])
        if v > 0:
            gen_timeout = min(v, 1800)

    wait_timeout = int(gen_timeout * 1.3) + 30
    return max_tokens, gen_timeout, wait_timeout


# ── Context budget ───────────────────────────────────────────────────────

def context_budget(context_length):
    ctx = max(context_length or 4096, 4096)
    budget = int(ctx * _CONTEXT_BUDGET_FRACTION * _CHARS_PER_TOKEN)
    return max(budget, _MIN_CONTEXT_BUDGET)


def estimate_tokens(messages):
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        total += len(content) // _CHARS_PER_TOKEN + 4
    return total


def estimate_conversation_tokens(mission):
    total = 0
    for msg in mission.conversation:
        content = msg.get("content") or ""
        total += len(content) // _CHARS_PER_TOKEN + 4
    return total


def is_context_overflow(result):
    if not result or not result.get("_agent_error"):
        return False, 0, 0
    err = result.get("error", "")
    if "exceed_context_size_error" in err or "exceeds the available context size" in err:
        try:
            m = re.search(r'\\?"n_prompt_tokens\\?"\s*:\s*(\d+)', err)
            n_prompt = int(m.group(1)) if m else 0
            m = re.search(r'\\?"n_ctx\\?"\s*:\s*(\d+)', err)
            n_ctx = int(m.group(1)) if m else 0
            return True, n_prompt, n_ctx
        except Exception:
            return True, 0, 0
    return False, 0, 0


def scaled_limits(context_length):
    budget = context_budget(context_length)
    return {
        "read_file_max": budget // 4,
        "action_result_max": budget // 6,
        "total_results_max": budget // 3,
        "agent_result_max": budget // 8,
        "smart_truncate_max": budget // 5,
        "search_max": budget // 6,
        "conversation_window": min(20, max(4, budget // 8000)),
    }
