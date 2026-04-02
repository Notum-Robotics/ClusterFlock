"""Allow/deny list for node admission control.

Modes:
  - open: any node can register immediately
  - approve (default): nodes go to pending queue, admin must accept
  - allow: only explicitly allowed nodes may register
  - deny: all nodes except explicitly denied may register

Nodes are matched by node_id OR hostname.
"""

import threading
import time

_lock = threading.Lock()
_mode = "approve"       # "open" | "approve" | "allow" | "deny"
_allow_set = set()      # node_ids / hostnames allowed  (used when mode=allow)
_deny_set = set()       # node_ids / hostnames denied   (used when mode=deny)
_pending = {}           # node_id → {hostname, hardware, requested_at}


def mode():
    with _lock:
        return _mode


def set_mode(m):
    if m not in ("open", "approve", "allow", "deny"):
        raise ValueError(f"Invalid mode: {m}")
    with _lock:
        global _mode
        _mode = m


def allow(identifier):
    with _lock:
        _allow_set.add(identifier)
        _deny_set.discard(identifier)


def deny(identifier):
    with _lock:
        _deny_set.add(identifier)
        _allow_set.discard(identifier)


def remove(identifier):
    with _lock:
        _allow_set.discard(identifier)
        _deny_set.discard(identifier)


def is_permitted(node_id, hostname=""):
    """Check whether a node is allowed to join the cluster.

    Returns True (allowed), False (denied), or "pending" (queued for approval).
    """
    with _lock:
        ids = {node_id, hostname} - {""}
        if _mode == "open":
            return True
        if _mode == "approve":
            # Already approved → in allow set
            if ids & _allow_set:
                return True
            # Explicitly rejected → in deny set
            if ids & _deny_set:
                return False
            return "pending"
        if _mode == "allow":
            return bool(ids & _allow_set)
        if _mode == "deny":
            return not bool(ids & _deny_set)
    return False


def enqueue(node_id, hostname="", hardware=None):
    """Add a node to the pending approval queue."""
    with _lock:
        _pending[node_id] = {
            "hostname": hostname,
            "hardware": hardware,
            "requested_at": time.time(),
        }


def approve_node(node_id):
    """Approve a pending node. Returns True if it was pending."""
    with _lock:
        if node_id in _pending:
            del _pending[node_id]
        _allow_set.add(node_id)
        _deny_set.discard(node_id)
        return True


def reject_node(node_id):
    """Reject and remove a pending node. Adds to deny set."""
    with _lock:
        _pending.pop(node_id, None)
        _deny_set.add(node_id)
        return True


def pending_list():
    """Return list of nodes waiting for approval."""
    with _lock:
        return [
            {"node_id": nid, **info}
            for nid, info in _pending.items()
        ]


def is_pending(node_id):
    with _lock:
        return node_id in _pending


def status():
    with _lock:
        return {
            "mode": _mode,
            "allow_list": sorted(_allow_set),
            "deny_list": sorted(_deny_set),
            "pending": [
                {"node_id": nid, **info}
                for nid, info in _pending.items()
            ],
        }


def dump():
    with _lock:
        return {"mode": _mode,
                "allow": list(_allow_set),
                "deny": list(_deny_set),
                "pending": {nid: info for nid, info in _pending.items()}}


def load(data):
    with _lock:
        global _mode
        _mode = data.get("mode", "approve")
        _allow_set.clear()
        _allow_set.update(data.get("allow", []))
        _deny_set.clear()
        _deny_set.update(data.get("deny", []))
        _pending.clear()
        _pending.update(data.get("pending", {}))
