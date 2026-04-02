"""Node registry: tracks agents, heartbeats, health status."""

import threading
import time

# Nodes not heard from within this window are marked stale, then dead.
# Thresholds scale with heartbeat interval (default 5s).
STALE_AFTER = 20    # seconds (4 missed 5s heartbeats)
DEAD_AFTER = 60     # seconds
REMOVE_AFTER = 600  # seconds — dead nodes reaped after 10 min

_lock = threading.Lock()
_nodes = {}  # node_id → node dict


def all_nodes():
    """Return snapshot of all registered nodes with computed health."""
    with _lock:
        now = time.time()
        return [_enrich(dict(n), now) for n in _nodes.values()]


def get_node(node_id):
    with _lock:
        n = _nodes.get(node_id)
        if n is None:
            return None
        return _enrich(dict(n), time.time())


def register(node_id, hostname, hardware=None, token=None,
             conn_mode="pull", address=None, orchestrator_token=None):
    """Add or update a node in the registry. Returns the node dict."""
    with _lock:
        now = time.time()
        existing = _nodes.get(node_id)
        node = {
            "node_id": node_id,
            "hostname": hostname,
            "hardware": hardware or (existing or {}).get("hardware"),
            "metrics": (existing or {}).get("metrics"),
            "endpoints": (existing or {}).get("endpoints", []),
            "token": token or (existing or {}).get("token"),
            "conn_mode": conn_mode,
            "registered_at": (existing or {}).get("registered_at", now),
            "last_seen": now,
        }
        if conn_mode == "push":
            node["address"] = address
            node["orchestrator_token"] = orchestrator_token
        _nodes[node_id] = node
        return _enrich(dict(node), now)


def heartbeat(node_id, hostname, metrics=None, endpoints=None, hardware=None,
              agent_version=None, downloaded=None, agent_type=None,
              cpu_ram_enabled=None, activity=None):
    """Process a heartbeat from a node. Returns False if node unknown."""
    with _lock:
        node = _nodes.get(node_id)
        if node is None:
            return False
        now = time.time()
        node["last_seen"] = now
        node["hostname"] = hostname
        if metrics:
            node["metrics"] = metrics
        if endpoints is not None:
            node["endpoints"] = endpoints
        if hardware:
            node["hardware"] = hardware
        if agent_version is not None:
            node["agent_version"] = agent_version
        if downloaded is not None:
            node["downloaded"] = downloaded
        if agent_type is not None:
            node["agent_type"] = agent_type
        if cpu_ram_enabled is not None:
            node["cpu_ram_enabled"] = cpu_ram_enabled
        if activity is not None:
            node["activity"] = activity
        return True


def remove(node_id):
    with _lock:
        return _nodes.pop(node_id, None) is not None


def reap_dead():
    """Remove nodes that have been dead longer than REMOVE_AFTER.
    Local agents are never reaped.
    Returns list of (node_id, hostname) removed."""
    with _lock:
        now = time.time()
        to_remove = []
        for nid, n in _nodes.items():
            if n.get("conn_mode") == "local":
                continue
            age = now - n.get("last_seen", 0)
            if age >= REMOVE_AFTER:
                to_remove.append((nid, n.get("hostname", "")))
        for nid, _ in to_remove:
            _nodes.pop(nid, None)
        return to_remove


def start_reaper(on_remove=None, interval=60):
    """Background thread that periodically reaps dead nodes."""
    def _loop():
        while True:
            time.sleep(interval)
            removed = reap_dead()
            for nid, hostname in removed:
                if on_remove:
                    on_remove(nid, hostname)
    threading.Thread(target=_loop, daemon=True).start()


def node_count():
    with _lock:
        return len(_nodes)


def _enrich(node, now):
    """Add computed 'status' field based on last heartbeat age."""
    if node.get("conn_mode") == "local":
        node["status"] = "healthy"
        return node
    age = now - node.get("last_seen", 0)
    if age < STALE_AFTER:
        node["status"] = "healthy"
    elif age < DEAD_AFTER:
        node["status"] = "stale"
    else:
        node["status"] = "dead"
    return node


# ── Push-mode helpers ────────────────────────────────────────────────────

def push_nodes():
    """Return [(node_id, address, orch_token)] for active push-mode nodes."""
    with _lock:
        return [
            (n["node_id"], n["address"], n["orchestrator_token"])
            for n in _nodes.values()
            if n.get("conn_mode") == "push" and n.get("address")
        ]


def push_configs():
    """Serialisable push-node connection data for persistence."""
    with _lock:
        return {
            n["node_id"]: {
                "hostname": n.get("hostname", ""),
                "address": n["address"],
                "orchestrator_token": n["orchestrator_token"],
                "hardware": n.get("hardware"),
            }
            for n in _nodes.values()
            if n.get("conn_mode") == "push"
        }


def restore_push(data):
    """Restore push-mode nodes from persisted state."""
    with _lock:
        now = time.time()
        for nid, info in (data or {}).items():
            _nodes[nid] = {
                "node_id": nid,
                "hostname": info.get("hostname", ""),
                "hardware": info.get("hardware"),
                "metrics": None,
                "endpoints": [],
                "token": None,
                "conn_mode": "push",
                "address": info["address"],
                "orchestrator_token": info["orchestrator_token"],
                "registered_at": now,
                "last_seen": 0,
            }
