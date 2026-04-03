"""JSON parsing resilience stack — extraction, repair, and diagnostics.

Unifies the previously duplicated brace-matching logic into a single
_find_json_objects() utility used by all extraction functions.
"""

import json
import re


# ── Core utility: escape-aware brace matching ────────────────────────────

def _find_json_objects(text):
    """Find all top-level JSON object substrings via escape-aware brace matching.
    Returns a list of candidate strings sorted largest-first."""
    depth = 0
    start = -1
    candidates = []
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(text[start:i + 1])
                start = -1
    candidates.sort(key=len, reverse=True)
    return candidates


# ── Newline / special-char fixer ─────────────────────────────────────────

def _fix_json_newlines(text):
    """Replace literal newlines/tabs inside JSON string values with escape sequences.
    Also handles unescaped double-quotes inside strings (common in HTML content).
    Uses lookahead to distinguish string boundary quotes from internal literals."""
    result = []
    in_string = False
    escape = False
    n = len(text)
    for i, ch in enumerate(text):
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape = True
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                result.append(ch)
                continue
            else:
                # Lookahead: a real closing quote is followed by , } ] : or end-of-text
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j >= n or text[j] in ',}]:':
                    in_string = False
                    result.append(ch)
                    continue
                else:
                    # Unescaped quote inside a string — escape it
                    result.append('\\"')
                    continue
        if in_string:
            if ch == '\n':
                result.append('\\n')
                continue
            if ch == '\r':
                result.append('\\r')
                continue
            if ch == '\t':
                result.append('\\t')
                continue
        result.append(ch)
    return ''.join(result)


# ── JSON extraction strategies ───────────────────────────────────────────

def _try_parse_json(text):
    """Try multiple strategies to extract a JSON object from text."""
    # Pre-process: fix literal newlines in strings once upfront
    fixed_text = _fix_json_newlines(text)

    # Strategy A: direct parse
    for src in (text, fixed_text):
        try:
            return json.loads(src)
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy B: extract from code fences — find ALL fences
    for m in re.finditer(r'```(?:json)?\s*\n?([\s\S]*?)```', text):
        inner = m.group(1).strip()
        if inner.startswith('{'):
            try:
                return json.loads(inner)
            except (json.JSONDecodeError, ValueError):
                pass
            try:
                return json.loads(_fix_json_newlines(inner))
            except (json.JSONDecodeError, ValueError):
                pass
            obj = _extract_json_object(inner)
            if obj is not None:
                return obj

    # Strategy C: brace matching on whole text
    obj = _extract_json_object(text)
    if obj is not None:
        return obj

    # Strategy D: repair truncated/malformed
    obj = _extract_json_object_with_repair(text)
    if obj is not None:
        return obj

    return None


def _extract_json_object(text):
    """Find the largest valid JSON object in text using brace-depth matching."""
    candidates = _find_json_objects(text)
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            try:
                return json.loads(_fix_json_newlines(c))
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _extract_json_object_with_repair(text):
    """Like _extract_json_object but attempts to repair truncated/malformed JSON."""
    first_brace = text.find('{')
    if first_brace < 0:
        return None
    fragment = text[first_brace:]

    # Use _find_json_objects to get complete objects first
    candidates = _find_json_objects(fragment)
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, ValueError):
            try:
                return json.loads(_fix_json_newlines(c))
            except (json.JSONDecodeError, ValueError):
                pass

    # No complete objects — try to close truncated JSON
    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escape = False
    for ch in fragment:
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth_brace += 1
        elif ch == '}':
            depth_brace -= 1
        elif ch == '[':
            depth_bracket += 1
        elif ch == ']':
            depth_bracket -= 1

    if depth_brace > 0 or depth_bracket > 0:
        tail = fragment.rstrip()
        if in_string:
            tail += '"'
        tail += ']' * max(0, depth_bracket) + '}' * max(0, depth_brace)
        try:
            return json.loads(tail)
        except (json.JSONDecodeError, ValueError):
            pass

    return None


# ── Showrunner response parsing ──────────────────────────────────────────

def _parse_showrunner_response(text):
    """Parse Showrunner JSON response. Returns dict with thinking + actions, or None."""
    if not text:
        return None

    # 0. Extract thinking from <think> tags
    think_match = re.search(r'<think>(.*?)</think>', text, flags=re.DOTALL)
    thinking_text = think_match.group(1).strip() if think_match else ""

    # 1. Strip <think>...</think> wrappers
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if not cleaned:
        cleaned = text

    # 1b. Handle model-native tool-call tags (MiniMax, Qwen, etc.)
    native = _extract_native_tool_calls(cleaned, thinking_text)
    if native:
        return native

    # 2. Try all sources in priority order
    for src in [cleaned, text]:
        obj = _try_parse_json(src)
        if obj and isinstance(obj, dict):
            if obj.get("thinking") and not obj.get("actions"):
                inner = _try_parse_json(obj["thinking"])
                if inner and isinstance(inner, dict) and inner.get("actions"):
                    return inner
            return obj

    # 3. Also check inside reasoning/thinking text for JSON
    if thinking_text:
        obj = _try_parse_json(thinking_text)
        if obj and isinstance(obj, dict) and obj.get("actions"):
            return obj

    # 4. Fallback — treat entire response as thinking with no actions
    return {"thinking": thinking_text or text, "actions": []}


def _extract_native_tool_calls(text, thinking=""):
    """Extract actions from model-native tool-call formats (MiniMax, Qwen, etc.)."""
    # MiniMax format
    mm_match = re.search(r'<minimax:tool_call>(.*?)(?:</minimax:tool_call>|$)', text, flags=re.DOTALL)
    if not mm_match:
        mm_match = re.search(r'<minimax:tool_call>\s*(.*)', text, flags=re.DOTALL)
    if mm_match:
        tool_text = mm_match.group(1).strip()
        actions = _parse_native_actions(tool_text)
        if actions:
            return {"thinking": thinking, "actions": actions}

    # Qwen format
    qwen_match = re.search(r'<tool_call>(.*?)(?:</tool_call>|$)', text, flags=re.DOTALL)
    if qwen_match:
        tool_text = qwen_match.group(1).strip()
        actions = _parse_native_actions(tool_text)
        if actions:
            return {"thinking": thinking, "actions": actions}

    # Gemma format: <|tool_call>call:namespace:action_type{json_params}<tool_call|>
    # Also handles: <|tool_call>{json}<|tool_call|>
    gemma_calls = re.findall(
        r'<\|tool_call\|?>(.*?)(?:<\|?tool_call\|>|$)', text, flags=re.DOTALL)
    if gemma_calls:
        actions = []
        for call_text in gemma_calls:
            call_text = call_text.strip()
            # Pattern: call:namespace:action_type{...}
            fn_match = re.match(r'(?:call:\w+:)?(\w+)\s*(\{.*)', call_text, flags=re.DOTALL)
            if fn_match:
                action_type = fn_match.group(1)
                json_part = fn_match.group(2)
                try:
                    params = json.loads(json_part)
                except (json.JSONDecodeError, ValueError):
                    params = _extract_json_object(json_part)
                    if params is None:
                        params = {}
                if isinstance(params, dict):
                    params["type"] = action_type
                    actions.append(params)
            else:
                # Bare JSON inside the tag
                parsed = _parse_native_actions(call_text)
                if parsed:
                    actions.extend(parsed)
        if actions:
            return {"thinking": thinking, "actions": actions}

    return None


def _parse_native_actions(tool_text):
    """Parse action objects from raw JSON fragments inside model-native tool-call tags."""
    actions = []

    # Try as complete JSON
    try:
        arr = json.loads(tool_text)
        if isinstance(arr, list):
            return [a for a in arr if isinstance(a, dict) and a.get("type")]
        if isinstance(arr, dict) and arr.get("type"):
            return [arr]
        if isinstance(arr, dict) and arr.get("actions"):
            return [a for a in arr["actions"] if isinstance(a, dict) and a.get("type")]
    except (json.JSONDecodeError, ValueError):
        pass

    # Try wrapping in array brackets
    try:
        arr = json.loads("[" + tool_text + "]")
        actions = [a for a in arr if isinstance(a, dict) and a.get("type")]
        if actions:
            return actions
    except (json.JSONDecodeError, ValueError):
        pass

    # Extract all JSON objects via unified brace matching
    for candidate in _find_json_objects(tool_text):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and obj.get("type"):
                actions.append(obj)
        except (json.JSONDecodeError, ValueError):
            try:
                obj = json.loads(_fix_json_newlines(candidate))
                if isinstance(obj, dict) and obj.get("type"):
                    actions.append(obj)
            except (json.JSONDecodeError, ValueError):
                pass

    return actions


def _diagnose_parse_failure(text):
    """Diagnose why a response failed to parse into actions. Returns a human-readable reason."""
    if not text:
        return "Empty response — no text returned."
    if not text.strip():
        return "Response was only whitespace."

    stripped = text.strip()

    if stripped.startswith("```"):
        return "Response wrapped in markdown code fences (```). Respond with raw JSON only."
    if not any(ch in stripped[:200] for ch in ('{', '[')):
        return "Response appears to be plain text, not JSON. Start with { and include an 'actions' array."

    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            if "actions" not in obj:
                return f"Valid JSON but missing 'actions' key. Found keys: {list(obj.keys())}"
            if not obj["actions"]:
                return "Parsed successfully but 'actions' array is empty."
            return "JSON parsed but actions had no recognizable type fields."
        return f"Parsed as {type(obj).__name__}, expected a JSON object (dict)."
    except json.JSONDecodeError as e:
        if "Unterminated string" in str(e):
            return f"Unterminated string in JSON (unescaped newline in a string value?). Error: {e}"
        if "Expecting ',' delimiter" in str(e):
            return f"Missing comma between JSON elements. Error at position {e.pos}: {e.msg}"
        if "Expecting property name" in str(e):
            return f"Trailing comma or malformed object. Error: {e}"
        return f"JSON parse error: {e.msg} at position {e.pos}"
