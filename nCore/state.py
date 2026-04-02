"""nCore state persistence — save/load registry, tokens, access lists, lock."""

import json
from pathlib import Path

STATE_FILE = Path(__file__).parent / "state.json"


def save(auth_data, access_data, push_data=None, locked=None, tight_pack=None,
         session_data=None, local_agent=None, oapi_config=None,
         heartbeat_interval=None):
    """Persist current state to disk."""
    state = {
        "auth": auth_data,
        "access": access_data,
        "push_nodes": push_data or {},
    }
    if locked is not None:
        state["locked"] = locked
    if tight_pack is not None:
        state["tight_pack"] = tight_pack
    if session_data is not None:
        state["sessions"] = session_data
    if local_agent is not None:
        state["local_agent"] = local_agent
    if oapi_config is not None:
        state["oapi_config"] = oapi_config
    if heartbeat_interval is not None:
        state["heartbeat_interval"] = heartbeat_interval
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def load():
    """Load persisted state. Returns (auth_data, access_data, push_data, locked, tight_pack, session_data, local_agent, oapi_config, heartbeat_interval)."""
    if not STATE_FILE.exists():
        return None, None, None, False, False, None, None, None, None
    try:
        state = json.loads(STATE_FILE.read_text())
        return (state.get("auth"), state.get("access"),
                state.get("push_nodes"), state.get("locked", False),
                state.get("tight_pack", False), state.get("sessions"),
                state.get("local_agent"), state.get("oapi_config"),
                state.get("heartbeat_interval"))
    except Exception:
        return None, None, None, False, False, None, None, None, None
