"""Session (mission) management for ClusterFlock.

Each session maps 1:1 to a mission. Sessions are identified by a short
hex ID generated client-side and passed in on creation.  The server
enforces a configurable cap on concurrent active sessions.

Session lifecycle:  idle → active → paused / completed
                              ↑         │
                              └─────────┘  (resume)

All functions are thread-safe.
"""

import secrets
import threading
import time

_lock = threading.Lock()

# session_id → session dict
_sessions: dict = {}

# Configurable limits
_max_concurrent = 3


# ── Configuration ────────────────────────────────────────────────────────

def set_max_concurrent(n):
    global _max_concurrent
    with _lock:
        _max_concurrent = max(1, int(n))


def get_max_concurrent():
    with _lock:
        return _max_concurrent


# ── Helpers ──────────────────────────────────────────────────────────────

def _active_count():
    """Number of sessions in active/paused state (hold resources)."""
    return sum(1 for s in _sessions.values() if s["status"] in ("active", "paused"))


def _new_session(session_id):
    now = time.time()
    return {
        "id": session_id,
        "status": "idle",
        "mission_text": "",
        "mission_version": 0,
        "mission_history": [],      # [{version, text, timestamp}]
        "created_at": now,
        "updated_at": now,
        "last_activity": now,
    }


# ── CRUD ─────────────────────────────────────────────────────────────────

def create(session_id=None):
    """Create a new session. Returns (session_dict, error_string|None)."""
    with _lock:
        if session_id and session_id in _sessions:
            return _sessions[session_id], None  # reuse existing

        if not session_id:
            session_id = secrets.token_hex(6)

        s = _new_session(session_id)
        _sessions[session_id] = s
        return dict(s), None


def get(session_id):
    with _lock:
        s = _sessions.get(session_id)
        return dict(s) if s else None


def list_all():
    with _lock:
        return [dict(s) for s in _sessions.values()]


def delete(session_id):
    """Remove a session. Returns True if found."""
    with _lock:
        return _sessions.pop(session_id, None) is not None


# ── Mission text ─────────────────────────────────────────────────────────

def set_mission_text(session_id, text):
    """Update mission text with versioning. Returns (session, error)."""
    with _lock:
        s = _sessions.get(session_id)
        if s is None:
            return None, "session not found"

        old_text = s["mission_text"]
        if old_text == text:
            return dict(s), None  # no change

        # Archive previous version
        if old_text:
            s["mission_history"].append({
                "version": s["mission_version"],
                "text": old_text,
                "timestamp": s["updated_at"],
            })

        s["mission_text"] = text
        s["mission_version"] += 1
        s["updated_at"] = time.time()
        s["last_activity"] = s["updated_at"]
        return dict(s), None


# ── Lifecycle ────────────────────────────────────────────────────────────

def activate(session_id):
    """Move session to active. Enforces max concurrent limit.
    Returns (session, error)."""
    with _lock:
        s = _sessions.get(session_id)
        if s is None:
            return None, "session not found"
        if s["status"] == "active":
            return dict(s), None

        # Count other active/paused sessions (not this one)
        others = sum(1 for sid, ss in _sessions.items()
                     if sid != session_id and ss["status"] in ("active", "paused"))
        if others >= _max_concurrent:
            return None, f"max concurrent sessions reached ({_max_concurrent})"

        if not s["mission_text"]:
            return None, "mission text required before activation"

        s["status"] = "active"
        s["last_activity"] = time.time()
        s["updated_at"] = s["last_activity"]
        return dict(s), None


def pause(session_id):
    """Pause an active session. Returns (session, error)."""
    with _lock:
        s = _sessions.get(session_id)
        if s is None:
            return None, "session not found"
        if s["status"] != "active":
            return None, "session not active"
        s["status"] = "paused"
        s["updated_at"] = time.time()
        return dict(s), None


def resume(session_id):
    """Resume a paused session. Returns (session, error)."""
    with _lock:
        s = _sessions.get(session_id)
        if s is None:
            return None, "session not found"
        if s["status"] != "paused":
            return None, "session not paused"

        others = sum(1 for sid, ss in _sessions.items()
                     if sid != session_id and ss["status"] in ("active", "paused"))
        if others >= _max_concurrent:
            return None, f"max concurrent sessions reached ({_max_concurrent})"

        s["status"] = "active"
        s["last_activity"] = time.time()
        s["updated_at"] = s["last_activity"]
        return dict(s), None


def complete(session_id):
    """Mark session completed. Returns (session, error)."""
    with _lock:
        s = _sessions.get(session_id)
        if s is None:
            return None, "session not found"
        s["status"] = "completed"
        s["updated_at"] = time.time()
        return dict(s), None


def touch(session_id):
    """Update last_activity timestamp (called on any interaction)."""
    with _lock:
        s = _sessions.get(session_id)
        if s:
            s["last_activity"] = time.time()


# ── Persistence ──────────────────────────────────────────────────────────

def dump():
    """Return serialisable state for persistence."""
    with _lock:
        return {
            "max_concurrent": _max_concurrent,
            "sessions": {sid: dict(s) for sid, s in _sessions.items()},
        }


def load(data):
    """Restore from persisted state."""
    global _max_concurrent
    if not data:
        return
    with _lock:
        _max_concurrent = data.get("max_concurrent", 3)
        _sessions.clear()
        for sid, s in data.get("sessions", {}).items():
            _sessions[sid] = s
