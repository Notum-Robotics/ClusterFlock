"""Mission engine — Showrunner election, flock management, async agent loop.

Each mission has:
 - A Docker container (lifecycle managed here)
 - A Showrunner (best available model, auto-elected)
 - A flock.json mapping endpoints → friendly names
 - A mission_log.md inside the container
 - An async loop dispatching work to agents and feeding results back

All public functions are thread-safe.
"""

import threading
import time

from .state import (
    MissionState,
    _lock,
    _missions,
    _MAX_MISSIONS,
    _MAX_CONCURRENT,
)
from .container import (
    _container_exec,
    _container_list_dir,
    _container_read_file,
    _destroy_container,
)
from .showrunner import (
    _find_endpoint,
    _build_showrunner_context,
)
from .loop import _mission_loop
from .persistence import (
    _persist_missions,
    _restore_missions,
    gc_containers,
    _watchdog_loop,
)


# ── Public API ───────────────────────────────────────────────────────────

def start_mission(mission_id, mission_text, showrunner_override=None):
    """Start a new mission, or continue an existing one. Returns (mission_dict, error)."""
    with _lock:
        if mission_id in _missions:
            m = _missions[mission_id]
            if m.status in ("running", "initializing"):
                if mission_text and mission_text != m.mission_text:
                    m.mission_text = mission_text
                    m.mission_version += 1
                    m.log_event("MISSION_CHANGED",
                                f"v{m.mission_version}: {mission_text[:200]}")
                return m.to_dict(), None

            m._stop_event.clear()
            old_text = m.mission_text
            if mission_text and mission_text != old_text:
                m.mission_text = mission_text
                m.mission_version += 1
                m.log_event("MISSION_CHANGED",
                            f"v{m.mission_version}: {mission_text[:200]}")
            if (showrunner_override and
                    showrunner_override.get("node_id") and
                    showrunner_override.get("model")):
                m.showrunner_override = showrunner_override
            m.status = "running"
            m.status_message = "Continuing mission..."
            m.status_progress = -1
            m._has_result = False
            mission = m
        else:
            if len(_missions) >= _MAX_MISSIONS:
                return None, (f"Maximum {_MAX_MISSIONS} missions reached "
                              "— delete old missions to start new ones")
            active = sum(1 for mx in _missions.values()
                         if mx.status in ("running", "initializing"))
            if active >= _MAX_CONCURRENT:
                return None, f"Maximum {_MAX_CONCURRENT} concurrent missions reached"
            mission = MissionState(mission_id, mission_text)
            if (showrunner_override and
                    showrunner_override.get("node_id") and
                    showrunner_override.get("model")):
                mission.showrunner_override = showrunner_override
            _missions[mission_id] = mission

    t = threading.Thread(target=_mission_loop, args=(mission,),
                         daemon=True, name=f"mission-{mission_id}")
    mission._thread = t
    t.start()

    _persist_missions()
    return mission.to_dict(), None


def get_mission(mission_id):
    """Get mission state."""
    with _lock:
        m = _missions.get(mission_id)
        return m.to_dict() if m else None


def get_mission_log(mission_id, offset=0, limit=100, level=None, agent=None):
    """Get filtered event log entries."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None

        events = m.event_log
        if level:
            events = [e for e in events if e["level"] == level]
        if agent:
            events = [e for e in events
                      if e.get("agent", "").lower() == agent.lower()]

        total = len(events)
        events = events[offset:offset + limit]
        return {"events": events, "total": total, "offset": offset}


def get_mission_flock(mission_id):
    """Get flock agent details."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None
        return {
            "flock": {name: a.to_dict() for name, a in m.flock.items()},
            "showrunner": {
                "node_id": m.showrunner_node_id,
                "model": m.showrunner_model,
                "score": m.showrunner_score,
            } if m.showrunner_node_id else None,
        }


def pause_mission(mission_id):
    """Pause a running mission."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None, "mission not found"
        if m.status != "running":
            return None, "mission not running"
        m.status = "paused"
        m._stop_event.set()
        m.log_event("INFO", "Mission paused")
        result = m.to_dict()
    _persist_missions()
    return result, None


def resume_mission(mission_id):
    """Resume a paused mission."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None, "mission not found"
        if m.status != "paused":
            return None, "mission not paused"
        m.status = "running"
        m._stop_event.clear()

    t = threading.Thread(target=_mission_loop, args=(m,),
                         daemon=True, name=f"mission-{mission_id}")
    m._thread = t
    t.start()

    _persist_missions()
    return m.to_dict(), None


def stop_mission(mission_id):
    """Stop and complete a mission. Container stays alive until deletion."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None, "mission not found"
        m._stop_event.set()
        m.status = "completed"
        m.log_event("INFO", "Mission stopped by user")
        result = m.to_dict()

    _persist_missions()
    return result, None


def set_showrunner_override(mission_id, node_id=None, model=None):
    """Set or clear the Showrunner override. Only when mission is not running.
    Pass node_id=None, model=None to clear (auto mode)."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None, "mission not found"
        if m.status in ("running", "initializing"):
            return None, ("cannot change showrunner while mission is running "
                          "— stop or pause first")
        if node_id and model:
            ep = _find_endpoint(node_id, model)
            if not ep:
                return None, f"endpoint not found or not ready: {model} on {node_id}"
            m.showrunner_override = {"node_id": node_id, "model": model}
            m.log_event("INFO", f"Showrunner override set: {model} on {ep[4]}")
        else:
            m.showrunner_override = None
            m.log_event("INFO", "Showrunner override cleared (auto mode)")
        return m.to_dict(), None


def delete_mission(mission_id):
    """Delete a mission and its container."""
    with _lock:
        m = _missions.pop(mission_id, None)
        if not m:
            return False
        m._stop_event.set()

    threading.Thread(target=_destroy_container, args=(mission_id,),
                     daemon=True).start()
    _persist_missions()
    return True


def respond_to_prompt(mission_id, prompt_id, response_text):
    """User responds to a Showrunner prompt."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None, "mission not found"

        for p in m.pending_prompts:
            if p["id"] == prompt_id and not p["answered"]:
                p["answered"] = True
                p["response"] = response_text
                m.user_responses.append({
                    "prompt_id": prompt_id,
                    "question": p["question"],
                    "response": response_text,
                    "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "_new": True,
                })
                m.log_event("USER_RESPONSE",
                            f"Q: {p['question'][:100]} A: {response_text[:200]}")
                return m.to_dict(), None

        return None, "prompt not found or already answered"


def get_container_files(mission_id, path="/home/mission"):
    """List files in mission container."""
    with _lock:
        m = _missions.get(mission_id)
        if not m or not m.container_id:
            return None
    return _container_list_dir(m.container_id, path)


def get_container_file(mission_id, path):
    """Read a file from mission container."""
    with _lock:
        m = _missions.get(mission_id)
        if not m or not m.container_id:
            return None
    return _container_read_file(m.container_id, path)


def get_container_id(mission_id):
    """Return the Docker container ID for a mission (or None)."""
    with _lock:
        m = _missions.get(mission_id)
        if not m or not m.container_id:
            return None
        return m.container_id


def exec_in_container(mission_id, command):
    """Execute command in mission container (for terminal)."""
    with _lock:
        m = _missions.get(mission_id)
        if not m or not m.container_id:
            return None, "no container"
    out, err, rc = _container_exec(m.container_id, command, timeout=30)
    return {"stdout": out, "stderr": err, "exit_code": rc}, None


def list_missions():
    """Return all missions summary."""
    with _lock:
        return [m.to_dict() for m in _missions.values()]


def get_showrunner_context(mission_id):
    """Return the current Showrunner context as structured messages for the UI."""
    with _lock:
        m = _missions.get(mission_id)
        if not m:
            return None
        msgs = []
        sys_prompt = _build_showrunner_context(m, include_history=False)
        msgs.append({"role": "system", "content": sys_prompt})
        for msg in (m.conversation or []):
            msgs.append({"role": msg.get("role", "unknown"),
                         "content": msg.get("content", "")})
        return msgs


# ── Module-level startup ─────────────────────────────────────────────────

# Restore persisted missions BEFORE starting the watchdog — this populates
# _missions so the GC knows which containers are still in use.
_restore_missions()

# Start watchdog — GC runs after 60s delay, then periodically.
threading.Thread(target=_watchdog_loop, daemon=True,
                 name="mission-watchdog").start()
