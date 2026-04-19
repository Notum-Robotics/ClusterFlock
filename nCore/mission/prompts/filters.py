"""Custom Jinja2 filters for prompt rendering."""


def budget_truncate(text, max_chars):
    """Truncate text to fit a character budget."""
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...truncated...]"


def elapsed_str(seconds):
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}min"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m}m"


def json_compact(obj):
    """Format object as compact JSON string."""
    import json
    return json.dumps(obj, separators=(",", ":"))
