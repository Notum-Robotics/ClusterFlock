"""Mission persistence, crash-recovery, container GC, and watchdog."""

import json
import logging
import threading
import time

log = logging.getLogger(__name__)

from .state import (
    MissionState,
    MissionPlan,
    _lock,
    _missions,
    _MISSIONS_FILE,
    _WATCHDOG_INTERVAL,
)
from .container import _docker_exec


def persist_missions():
    """Save mission metadata to disk for crash recovery."""
    with _lock:
        data = {}
        for mid, m in _missions.items():
            truncated_history = []
            for th in (m.task_history or [])[-50:]:
                entry = dict(th)
                for key in ("result", "error"):
                    if entry.get(key) and len(str(entry[key])) > 1000:
                        entry[key] = str(entry[key])[:1000]
                truncated_history.append(entry)

            truncated_convo = []
            for turn in (m.conversation or [])[-20:]:
                t = dict(turn)
                content = t.get("content", "")
                if len(content) > 3000:
                    t["content"] = content[:3000]
                truncated_convo.append(t)

            truncated_events = (m.event_log or [])[-200:]

            data[mid] = {
                "mission_id": m.mission_id,
                "mission_text": m.mission_text,
                "mission_version": m.mission_version,
                "status": m.status,
                "created_at": m.created_at,
                "container_name": m.container_name,
                "showrunner_override": m.showrunner_override,
                "round_trips": m.round_trips,
                "last_summary": m.last_summary,
                "notes": m.notes,
                "mission_phase": getattr(m, "mission_phase", "planning"),
                "plan": m.plan.to_dict() if getattr(m, "plan", None) else None,
                "phase_history": getattr(m, "phase_history", []),
                "knowledge_base": dict(m.knowledge_base) if m.knowledge_base else {},
                "task_history": truncated_history,
                "conversation": truncated_convo,
                "event_log": truncated_events,
                "_sr_node_perf": dict(m._sr_node_perf) if m._sr_node_perf else {},
                "_agent_perf": dict(m._agent_perf) if m._agent_perf else {},
                "_has_result": m._has_result,
                "_flock_advice": dict(m._flock_advice) if m._flock_advice else {},
                "_advice_milestone_tracker": dict(m._advice_milestone_tracker) if m._advice_milestone_tracker else {},
            }
    try:
        tmp = _MISSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_MISSIONS_FILE)
    except Exception as e:
        log.error(f"[mission] Failed to persist missions: {e}")


def restore_missions():
    """Restore missions from disk after nCore restart."""
    if not _MISSIONS_FILE.exists():
        return

    try:
        data = json.loads(_MISSIONS_FILE.read_text())
    except Exception as e:
        log.error(f"[mission] Failed to read missions.json: {e}")
        return

    if not isinstance(data, dict):
        return

    restored = 0
    for mid, mdata in data.items():
        if not isinstance(mdata, dict):
            continue

        container_name = mdata.get("container_name", f"cf-mission-{mid}")

        out, _, rc = _docker_exec(
            ["docker", "inspect", "--format", "{{.Id}}", container_name],
            timeout=10,
        )
        if rc != 0:
            log.warning(f"[mission] Skipping {mid} — container {container_name} not found")
            continue

        container_id = out.strip()
        _docker_exec(["docker", "start", container_name], timeout=15)

        old_status = mdata.get("status", "completed")
        mission = MissionState(mid, mdata.get("mission_text", ""))
        mission.mission_version = mdata.get("mission_version", 1)
        mission.status = "paused"
        mission.created_at = mdata.get("created_at", time.time())
        mission.container_id = container_id
        mission.container_name = container_name
        mission.showrunner_override = mdata.get("showrunner_override")
        mission.round_trips = mdata.get("round_trips", 0)
        mission.last_summary = mdata.get("last_summary", "")
        mission.notes = mdata.get("notes", [])
        mission.mission_phase = mdata.get("mission_phase", "planning")
        mission.phase_history = mdata.get("phase_history", [])
        mission.knowledge_base = mdata.get("knowledge_base", {})
        mission.task_history = mdata.get("task_history", [])
        mission.conversation = mdata.get("conversation", [])
        mission.event_log = mdata.get("event_log", [])
        mission._sr_node_perf = mdata.get("_sr_node_perf", {})
        mission._agent_perf = mdata.get("_agent_perf", {})
        mission._has_result = mdata.get("_has_result", False)
        mission._flock_advice = mdata.get("_flock_advice", {})
        mission._advice_milestone_tracker = mdata.get("_advice_milestone_tracker", {})

        plan_data = mdata.get("plan")
        if plan_data:
            try:
                mission.plan = MissionPlan.from_dict(plan_data)
            except Exception as e:
                log.error(f"[mission] Failed to restore plan for {mid}: {e}")
                mission.plan = None
        mission.log_event("INFO",
            f"Mission restored from persistence (was {old_status}) — paused, ready to resume")

        with _lock:
            _missions[mid] = mission
        restored += 1

    if restored:
        log.info(f"[mission] Restored {restored} mission(s) from persistence")


def gc_containers():
    """Remove Docker containers and volumes whose missions no longer exist."""
    with _lock:
        known_ids = set(_missions.keys())

    removed = []
    kept = []

    out, _, rc = _docker_exec(
        ["docker", "ps", "-a", "--filter", "name=cf-mission-",
         "--format", "{{.Names}}"],
        timeout=15,
    )
    if rc == 0 and out:
        for name in out.strip().splitlines():
            name = name.strip()
            if not name.startswith("cf-mission-"):
                continue

            mission_id = name[len("cf-mission-"):]

            if mission_id in known_ids:
                kept.append(name)
                continue

            log.info(f"[gc] removing orphaned container {name}")
            _docker_exec(["docker", "stop", name], timeout=30)
            _docker_exec(["docker", "rm", "-f", name], timeout=15)
            _docker_exec(["docker", "volume", "rm", f"{name}-home"], timeout=15)
            removed.append(name)
    elif rc != 0:
        return {"removed": [], "kept": [], "error": "docker query failed"}

    vol_out, _, vol_rc = _docker_exec(
        ["docker", "volume", "ls", "--filter", "name=cf-mission-",
         "--format", "{{.Name}}"],
        timeout=15,
    )
    removed_volumes = []
    if vol_rc == 0 and vol_out:
        for vol_name in vol_out.strip().splitlines():
            vol_name = vol_name.strip()
            if not vol_name.startswith("cf-mission-") or not vol_name.endswith("-home"):
                continue

            mission_id = vol_name[len("cf-mission-"):-len("-home")]

            if mission_id in known_ids:
                continue

            log.info(f"[gc] removing orphaned volume {vol_name}")
            _docker_exec(["docker", "volume", "rm", vol_name], timeout=15)
            removed_volumes.append(vol_name)

    if removed or removed_volumes:
        log.info(f"[gc] cleaned up {len(removed)} container(s), "
              f"{len(removed_volumes)} volume(s), kept {len(kept)}")
    return {
        "removed": removed,
        "removed_volumes": removed_volumes,
        "kept": kept,
        "error": None,
    }


def watchdog_loop():
    """Background thread: periodically run container GC and persist state."""
    time.sleep(60)
    while True:
        try:
            gc_containers()
        except Exception:
            pass
        try:
            persist_missions()
        except Exception:
            pass
        time.sleep(_WATCHDOG_INTERVAL)
