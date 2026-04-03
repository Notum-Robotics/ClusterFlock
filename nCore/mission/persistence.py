"""Mission persistence, crash-recovery, container GC, and watchdog."""

import json
import threading
import time

from .state import (
    MissionState,
    _lock,
    _missions,
    _MISSIONS_FILE,
    _WATCHDOG_INTERVAL,
)
from .container import (
    _docker_exec,
    _container_exec,
    _container_write_file,
    _container_read_file,
)


def _write_mission_log_to_container(mission):
    """Write a condensed mission log to /home/mission/mission_log.txt.
    Called periodically and on compaction so showrunner can read_file it."""
    if not mission.container_id:
        return
    lines = []
    lines.append(f"Mission: {mission.mission_id}")
    lines.append(f"Status: {mission.status}, Round trips: {mission.round_trips}")
    lines.append(f"Elapsed: {(time.time() - mission.created_at)/60:.0f} min")
    lines.append(f"Showrunner: {mission.showrunner_model}")
    lines.append("")

    # Completed tasks summary
    if mission.task_history:
        lines.append("=== COMPLETED TASKS ===")
        for td in mission.task_history[-20:]:
            agent = td.get("agent_name", "?")
            status = td.get("status", "?")
            result = (td.get("result") or td.get("error") or "")[:200]
            lines.append(f"- {agent} ({status}): {result}")
        lines.append("")

    # Key events (filter for important ones only)
    important = ("THINKING", "COMPLETE", "ERROR", "DISPATCH", "CANCEL_TASK",
                 "MISSION_CHANGED", "AUTO_DONE", "CONFIG", "REFLECT")
    key_events = [e for e in mission.event_log if e.get("level") in important]
    if key_events:
        lines.append("=== KEY EVENTS (recent) ===")
        for e in key_events[-30:]:
            ts = e.get("time_str", "")
            level = e.get("level", "")
            agent = e.get("agent", "")
            msg = e.get("message", "")[:200]
            prefix = f"[{agent}] " if agent else ""
            lines.append(f"{ts} {level} {prefix}{msg}")
        lines.append("")

    # Last summary if available
    if mission.last_summary:
        lines.append("=== PROGRESS SUMMARY ===")
        lines.append(mission.last_summary[:2000])

    try:
        _container_write_file(mission.container_id, "/home/mission/mission_log.txt",
                              "\n".join(lines))
    except Exception:
        pass  # best effort


def _persist_missions():
    """Save mission metadata to disk for crash recovery.
    Call OUTSIDE of _lock to avoid deadlock — this function acquires it briefly."""
    with _lock:
        data = {}
        for mid, m in _missions.items():
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
            }
    try:
        tmp = _MISSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_MISSIONS_FILE)
    except Exception as e:
        print(f"[mission] Failed to persist missions: {e}")


def _restore_missions():
    """Restore missions from disk after nCore restart.
    Reconnects to existing Docker containers. Restored missions are paused."""
    if not _MISSIONS_FILE.exists():
        return

    try:
        data = json.loads(_MISSIONS_FILE.read_text())
    except Exception as e:
        print(f"[mission] Failed to read missions.json: {e}")
        return

    if not isinstance(data, dict):
        return

    restored = 0
    for mid, mdata in data.items():
        if not isinstance(mdata, dict):
            continue

        container_name = mdata.get("container_name", f"cf-mission-{mid}")

        # Check if container still exists and get its ID
        out, _, rc = _docker_exec(
            ["docker", "inspect", "--format", "{{.Id}}", container_name],
            timeout=10,
        )
        if rc != 0:
            print(f"[mission] Skipping {mid} — container {container_name} not found")
            continue

        container_id = out.strip()

        # Ensure container is running (may have been stopped)
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
        mission.log_event("INFO",
            f"Mission restored from persistence (was {old_status}) — paused, ready to resume")

        with _lock:
            _missions[mid] = mission
        restored += 1

    if restored:
        print(f"[mission] Restored {restored} mission(s) from persistence")


def gc_containers():
    """Remove Docker containers and volumes whose missions no longer exist in memory.

    Containers and volumes belonging to ANY existing mission (running, completed,
    paused, etc.) are kept — only truly orphaned resources are cleaned up.
    Returns dict with 'removed' and 'kept' lists.
    """
    with _lock:
        known_ids = set(_missions.keys())

    removed = []
    kept = []

    # 1. Clean orphaned containers
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

            print(f"[gc] removing orphaned container {name}")
            _docker_exec(["docker", "stop", name], timeout=30)
            _docker_exec(["docker", "rm", "-f", name], timeout=15)
            _docker_exec(["docker", "volume", "rm", f"{name}-home"], timeout=15)
            removed.append(name)
    elif rc != 0:
        return {"removed": [], "kept": [], "error": "docker query failed"}

    # 2. Clean orphaned volumes
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

            print(f"[gc] removing orphaned volume {vol_name}")
            _docker_exec(["docker", "volume", "rm", vol_name], timeout=15)
            removed_volumes.append(vol_name)

    if removed or removed_volumes:
        print(f"[gc] cleaned up {len(removed)} container(s), "
              f"{len(removed_volumes)} volume(s), kept {len(kept)}")
    return {
        "removed": removed,
        "removed_volumes": removed_volumes,
        "kept": kept,
        "error": None,
    }


def _watchdog_loop():
    """Background thread: periodically run container GC and persist mission state."""
    time.sleep(60)
    while True:
        try:
            gc_containers()
        except Exception:
            pass
        try:
            _persist_missions()
        except Exception:
            pass
        time.sleep(_WATCHDOG_INTERVAL)
