"""Bearer token authentication for nNode agents."""

import hashlib
import os
import secrets
import threading
import time

_lock = threading.Lock()
_tokens = {}  # token_hash → {"node_id": ..., "created": ..., "label": ...}


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def generate(node_id, label=None):
    """Create a new bearer token for a node. Returns the raw token (only shown once)."""
    raw = secrets.token_urlsafe(32)
    h = _hash(raw)
    with _lock:
        _tokens[h] = {
            "node_id": node_id,
            "label": label or node_id,
            "created": time.time(),
        }
    return raw


def verify(raw_token):
    """Validate a bearer token. Returns node_id if valid, None otherwise."""
    if not raw_token:
        return None
    h = _hash(raw_token)
    with _lock:
        entry = _tokens.get(h)
    return entry["node_id"] if entry else None


def revoke_for_node(node_id):
    """Remove all tokens belonging to a node."""
    with _lock:
        to_remove = [h for h, e in _tokens.items() if e["node_id"] == node_id]
        for h in to_remove:
            del _tokens[h]
        return len(to_remove)


def list_tokens():
    """Return metadata for all tokens (never the raw token)."""
    with _lock:
        return [
            {"hash_prefix": h[:12], "node_id": e["node_id"],
             "label": e["label"], "created": e["created"]}
            for h, e in _tokens.items()
        ]


def dump():
    """Serialisable snapshot for persistence."""
    with _lock:
        return dict(_tokens)


def load(data):
    """Restore from persisted data."""
    with _lock:
        _tokens.clear()
        _tokens.update(data or {})
