"""Mission memory — file-based active memory system.

Tier 1 — Mission Memory (container directory):
    File-based memory at /home/mission/.memory/ — each note is a separate
    file organized by topic. The SR can create, read, update, list, and
    delete memory files. Works exactly like a workspace knowledge base.
    Survives conversation compaction (it's files, not conversation).
    Dies when the container is removed (mission-scoped).

Tier 2 — Pre-compaction capture:
    Before conversation compaction prunes old turns, key details
    are auto-extracted and saved to mission memory files.

Tier 3 — Long-term Memory (cross-mission):
    Persistent store in mission_memory.json. Insights, patterns, and lessons
    that transfer across missions.
"""

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

_MEMORY_DIR = "/home/mission/.memory"
_MEMORY_MAX_FILES = 30
_MEMORY_MAX_FILE_SIZE = 4000

# Legacy path (for backward compat during transition)
_WORKING_MEMORY_PATH = "/home/mission/.memory/working.json"
_WORKING_MAX_ENTRIES = 50
_WORKING_MAX_CONTENT = 500

_MEMORY_FILE = Path(__file__).resolve().parent.parent / "mission_memory.json"
_MAX_MEMORIES = 200
_MAX_VALUE_LEN = 1500
_MAX_AUTO_MEMORIES = 10
_RELEVANCE_WINDOW = 20


# ═══════════════════════════════════════════════════════════════════════════
# TIER 1: FILE-BASED MISSION MEMORY (container directory)
# ═══════════════════════════════════════════════════════════════════════════

def init_working_memory(container_id):
    """Initialize the .memory directory in the container."""
    if not container_id:
        return False
    from .container import _container_exec
    _container_exec(container_id, f"mkdir -p {_MEMORY_DIR}")
    return True


def memory_create(container_id, path, content):
    """Create a new memory file. Path is relative to .memory/ dir."""
    if not container_id or not path or not content:
        return {"ok": False, "error": "container_id, path, and content required"}
    from .container import _container_exec, _container_read_file, _container_write_file

    path = _sanitize_memory_path(path)
    full_path = f"{_MEMORY_DIR}/{path}"

    # Check file count limit
    existing = _list_memory_files(container_id)
    if len(existing) >= _MEMORY_MAX_FILES:
        return {"ok": False, "error": f"Memory limit reached ({_MEMORY_MAX_FILES} files). "
                "Delete old files first."}

    # Check if file already exists
    existing_content = _container_read_file(container_id, full_path)
    if existing_content:
        return {"ok": False, "error": f"File already exists: {path}. Use update to modify it."}

    # Ensure parent directory exists
    parent = "/".join(full_path.split("/")[:-1])
    if parent != _MEMORY_DIR:
        _container_exec(container_id, f"mkdir -p {parent}")

    content = content[:_MEMORY_MAX_FILE_SIZE]
    ok = _container_write_file(container_id, full_path, content)
    return {"ok": ok, "path": path, "size": len(content)}


def memory_read(container_id, path):
    """Read a memory file. Path is relative to .memory/ dir."""
    if not container_id or not path:
        return {"ok": False, "error": "container_id and path required"}
    from .container import _container_read_file

    path = _sanitize_memory_path(path)
    full_path = f"{_MEMORY_DIR}/{path}"
    content = _container_read_file(container_id, full_path)
    if content is None:
        return {"ok": False, "error": f"File not found: {path}"}
    return {"ok": True, "path": path, "content": content}


def memory_update(container_id, path, content):
    """Overwrite a memory file with new content."""
    if not container_id or not path or not content:
        return {"ok": False, "error": "container_id, path, and content required"}
    from .container import _container_read_file, _container_write_file

    path = _sanitize_memory_path(path)
    full_path = f"{_MEMORY_DIR}/{path}"

    # Verify file exists
    existing = _container_read_file(container_id, full_path)
    if existing is None:
        return {"ok": False, "error": f"File not found: {path}. Use create for new files."}

    content = content[:_MEMORY_MAX_FILE_SIZE]
    ok = _container_write_file(container_id, full_path, content)
    return {"ok": ok, "path": path, "size": len(content)}


def memory_append(container_id, path, content):
    """Append content to an existing memory file."""
    if not container_id or not path or not content:
        return {"ok": False, "error": "container_id, path, and content required"}
    from .container import _container_read_file, _container_write_file

    path = _sanitize_memory_path(path)
    full_path = f"{_MEMORY_DIR}/{path}"

    existing = _container_read_file(container_id, full_path)
    if existing is None:
        return {"ok": False, "error": f"File not found: {path}. Use create for new files."}

    combined = existing + "\n" + content
    if len(combined) > _MEMORY_MAX_FILE_SIZE:
        combined = combined[:_MEMORY_MAX_FILE_SIZE]
    ok = _container_write_file(container_id, full_path, combined)
    return {"ok": ok, "path": path, "size": len(combined)}


def memory_delete(container_id, path):
    """Delete a memory file."""
    if not container_id or not path:
        return {"ok": False, "error": "container_id and path required"}
    from .container import _container_exec

    path = _sanitize_memory_path(path)
    full_path = f"{_MEMORY_DIR}/{path}"
    out, err, rc = _container_exec(container_id, f"rm -f {full_path}")
    return {"ok": rc == 0, "path": path}


def memory_list(container_id):
    """List all memory files."""
    if not container_id:
        return {"ok": False, "error": "container_id required"}
    files = _list_memory_files(container_id)
    return {"ok": True, "files": files, "count": len(files)}


def _list_memory_files(container_id):
    """Return list of memory file paths relative to .memory/ dir."""
    from .container import _container_exec
    out, _, rc = _container_exec(
        container_id,
        f"find {_MEMORY_DIR} -type f -not -name 'working.json' "
        f"| sed 's|{_MEMORY_DIR}/||' | sort",
        timeout=5)
    if rc != 0 or not out:
        return []
    return [f.strip() for f in out.strip().split("\n") if f.strip()]


def _sanitize_memory_path(path):
    """Sanitize memory file path — prevent directory traversal."""
    path = path.strip().lstrip("/")
    # Block traversal
    parts = path.split("/")
    clean = [p for p in parts if p and p != ".." and p != "."]
    path = "/".join(clean)
    # Ensure it ends with .md if no extension
    if "." not in path.split("/")[-1]:
        path += ".md"
    return path[:200]


def format_working_memory_for_context(container_id):
    """Build the memory section for SR context — lists files + shows content."""
    if not container_id:
        return ""
    files = _list_memory_files(container_id)

    # Also check for legacy working.json entries
    legacy_entries = _read_legacy_working_memory(container_id)

    if not files and not legacy_entries:
        return ""

    lines = [
        "=== MISSION MEMORY (/home/mission/.memory/) ===",
        "Your file-based knowledge store. Survives conversation compaction.",
        "Organize notes by topic — one file per subject.",
        "Commands: memory_create, memory_read, memory_update, memory_append, "
        "memory_delete, memory_list",
        "",
    ]

    if files:
        lines.append("Files:")
        from .container import _container_read_file, _container_exec
        total_size = 0
        for f in files[:_MEMORY_MAX_FILES]:
            full_path = f"{_MEMORY_DIR}/{f}"
            # Get file size
            out, _, _ = _container_exec(
                container_id, f"wc -c < {full_path} 2>/dev/null", timeout=3)
            size = out.strip() if out else "?"
            lines.append(f"  {f} ({size}B)")
            total_size += int(size) if size.isdigit() else 0

        # Auto-include small files (under 500 bytes) in context
        lines.append("")
        for f in files[:15]:
            full_path = f"{_MEMORY_DIR}/{f}"
            content = _container_read_file(container_id, full_path)
            if content and len(content) <= 800:
                lines.append(f"── {f} ──")
                lines.append(content.strip())
                lines.append("")
            elif content:
                # Show first 2 lines + truncation notice
                preview = "\n".join(content.strip().split("\n")[:3])
                lines.append(f"── {f} (truncated, use memory_read for full) ──")
                lines.append(preview)
                lines.append(f"  ... [{len(content)} bytes total]")
                lines.append("")

    if legacy_entries:
        lines.append("── legacy notes ──")
        for e in legacy_entries[-10:]:
            cat = e.get("category", "note").upper()
            rt = e.get("rt", "?")
            content = e.get("content", "")
            lines.append(f"  [RT{rt} {cat}] {content}")
        lines.append("")

    return "\n".join(lines)


def _read_legacy_working_memory(container_id):
    """Read legacy working.json entries for backward compatibility."""
    from .container import _container_read_file
    raw = _container_read_file(container_id, _WORKING_MEMORY_PATH)
    if not raw:
        return []
    try:
        return json.loads(raw).get("entries", [])
    except json.JSONDecodeError:
        return []


# Legacy compatibility — save_working_memory still works, writes to
# both legacy JSON and a new memory file
def save_working_memory(container_id, category, content, round_trip=0):
    if not container_id or not content:
        return {"ok": False, "error": "container_id and content required"}
    from .container import _container_read_file, _container_write_file

    content = content.strip()[:_WORKING_MAX_CONTENT]
    category = category.strip().lower() if category else "observation"

    # Write to new file-based system
    path = f"notes/{category}.md"
    full_path = f"{_MEMORY_DIR}/{path}"
    from .container import _container_exec
    _container_exec(container_id, f"mkdir -p {_MEMORY_DIR}/notes")

    existing = _container_read_file(container_id, full_path) or ""
    ts = time.strftime("%H:%M:%S")
    entry = f"\n[RT{round_trip} {ts}] {content}"
    combined = existing + entry
    if len(combined) > _MEMORY_MAX_FILE_SIZE:
        # Keep the most recent entries
        lines = combined.split("\n")
        combined = "\n".join(lines[-40:])
    _container_write_file(container_id, full_path, combined)

    return {"ok": True, "entries": 1, "category": category, "path": path}


def read_working_memory(container_id):
    """Read all memory — returns legacy entries for backward compatibility."""
    return _read_legacy_working_memory(container_id)


# ═══════════════════════════════════════════════════════════════════════════
# TIER 2: PRE-COMPACTION CAPTURE
# ═══════════════════════════════════════════════════════════════════════════

_ERROR_RE = re.compile(
    r'error|traceback|exception|failed|failure|bug|broken|crash',
    re.IGNORECASE,
)


def pre_compaction_capture(mission, pruned_turns):
    if not getattr(mission, "container_id", None) or not pruned_turns:
        return 0

    saved = 0
    for turn in pruned_turns:
        if saved >= 3:
            break
        content = turn.get("content", "")
        role = turn.get("role", "")
        if role != "user" or not content or len(content) < 50:
            continue
        if content.lstrip().startswith(("{", "[", '"')) or content.count("\\n") > 10:
            continue
        lines = content.split('\n')
        error_lines = []
        for line in lines:
            stripped = line.strip()
            if 15 <= len(stripped) <= 300 and _ERROR_RE.search(stripped) and not stripped.startswith('"'):
                error_lines.append(stripped)
        if error_lines:
            snippet = '; '.join(error_lines[:2])[:_WORKING_MAX_CONTENT]
            save_working_memory(mission.container_id, "error",
                                f"[auto] {snippet}",
                                round_trip=getattr(mission, "round_trips", 0))
            saved += 1

    if saved:
        mission.log_event("MEMORY", f"Pre-compaction: captured {saved} entries to working memory")
    return saved


# ═══════════════════════════════════════════════════════════════════════════
# TIER 3: LONG-TERM MEMORY (cross-mission persistence)
# ═══════════════════════════════════════════════════════════════════════════

def _load_memories():
    if not _MEMORY_FILE.exists():
        return []
    try:
        data = json.loads(_MEMORY_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_memories(memories):
    if len(memories) > _MAX_MEMORIES:
        memories = memories[-_MAX_MEMORIES:]
    try:
        tmp = _MEMORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(memories, indent=2))
        tmp.replace(_MEMORY_FILE)
    except OSError as e:
        log.error(f"[memory] Failed to save memories: {e}")


def save_long_term(category, key, value, source_mission=None, tags=None):
    key = key.strip()[:100]
    value = value.strip()[:_MAX_VALUE_LEN]
    if not key or not value:
        return {"ok": False, "error": "key and value required"}

    memories = _load_memories()
    for entry in memories:
        if entry.get("key") == key:
            entry["value"] = value
            entry["category"] = category
            entry["updated_at"] = time.time()
            entry["source_mission"] = source_mission or entry.get("source_mission")
            entry["tags"] = tags or entry.get("tags", [])
            entry["recall_count"] = entry.get("recall_count", 0)
            _save_memories(memories)
            return {"ok": True, "action": "updated", "key": key}

    memories.append({
        "key": key, "value": value, "category": category,
        "tags": tags or [], "source_mission": source_mission,
        "created_at": time.time(), "updated_at": time.time(), "recall_count": 0,
    })
    _save_memories(memories)
    return {"ok": True, "action": "created", "key": key}


def recall_long_term(query, limit=None):
    limit = limit or _RELEVANCE_WINDOW
    memories = _load_memories()
    if not memories:
        return []

    query_lower = query.lower()
    query_words = set(re.findall(r'\w+', query_lower))

    scored = []
    for entry in memories:
        score = 0
        key_lower = entry.get("key", "").lower()
        if query_lower in key_lower:
            score += 10
        elif any(w in key_lower for w in query_words):
            score += 5
        value_lower = entry.get("value", "").lower()
        score += sum(2 for w in query_words if w in value_lower)
        tags = [t.lower() for t in entry.get("tags", [])]
        score += sum(3 for w in query_words if any(w in t for t in tags))
        cat = entry.get("category", "").lower()
        if any(w in cat for w in query_words):
            score += 2
        age_days = (time.time() - entry.get("updated_at", 0)) / 86400
        if age_days < 7:
            score += 1
        score += min(entry.get("recall_count", 0), 5) * 0.5
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda x: -x[0])
    result_keys = set()
    results = []
    for _, entry in scored[:limit]:
        results.append(entry)
        result_keys.add(entry["key"])

    if result_keys:
        for entry in memories:
            if entry["key"] in result_keys:
                entry["recall_count"] = entry.get("recall_count", 0) + 1
        _save_memories(memories)
    return results


def delete_long_term(key):
    memories = _load_memories()
    before = len(memories)
    memories = [m for m in memories if m.get("key") != key]
    if len(memories) < before:
        _save_memories(memories)
        return {"ok": True, "deleted": key}
    return {"ok": False, "error": f"memory '{key}' not found"}


def get_all_memories():
    return _load_memories()


def recall_for_mission(mission_text):
    if not mission_text:
        return ""
    memories = recall_long_term(mission_text, limit=_RELEVANCE_WINDOW)
    if not memories:
        return ""
    lines = ["=== LONG-TERM MEMORY (from past missions) ===",
             "These insights were learned from previous missions and may help:"]
    for m in memories:
        cat_label = m.get("category", "insight").upper()
        lines.append(f"  [{cat_label}] {m['key']}: {m['value']}")
    lines.extend(["", "Use save_memory to store new insights for future missions.",
                   "Use recall_memory to search past experiences.", ""])
    return "\n".join(lines)


# ── Auto-extract memories at mission completion ──────────────────────────

def auto_extract_memories(mission):
    mid = mission.mission_id
    extracted = 0

    # 1. Promote knowledge_base entries
    kb = getattr(mission, "knowledge_base", {}) or {}
    for key, value in kb.items():
        if len(value) < 20 or value.startswith("/home/mission/"):
            continue
        save_long_term(category="pattern", key=key, value=value,
                       source_mission=mid, tags=_extract_tags(value))
        extracted += 1
        if extracted >= _MAX_AUTO_MEMORIES:
            break

    # 2. Error recovery patterns from event log
    events = getattr(mission, "event_log", [])
    for i, evt in enumerate(events):
        if extracted >= _MAX_AUTO_MEMORIES:
            break
        if evt.get("level") == "ERROR":
            error_msg = evt.get("message", "")
            for j in range(i + 1, min(i + 10, len(events))):
                later = events[j]
                if later.get("level") in ("COMPLETE", "INFO") and "fix" in later.get("message", "").lower():
                    save_long_term(category="error_fix",
                                   key=f"error_{mid}_{extracted}",
                                   value=f"Error: {error_msg[:200]}\nFix: {later['message'][:200]}",
                                   source_mission=mid,
                                   tags=_extract_tags(error_msg + " " + later["message"]))
                    extracted += 1
                    break

    # 3. Mission summary
    summary = getattr(mission, "last_summary", "")
    if summary and len(summary) > 50 and extracted < _MAX_AUTO_MEMORIES:
        mission_type = _classify_mission(mission.mission_text or "")
        save_long_term(
            category="lesson",
            key=f"mission_summary_{mid}",
            value=(f"Mission: {(mission.mission_text or '')[:200]}\n"
                   f"Type: {mission_type}\n"
                   f"Duration: {(time.time() - mission.created_at) / 60:.0f}min, "
                   f"RTs: {mission.round_trips}, Tasks: {len(mission.task_history)}\n"
                   f"Summary: {summary[:500]}"),
            source_mission=mid,
            tags=[mission_type] + _extract_tags(mission.mission_text or ""),
        )
        extracted += 1

    # 4. Performance insights
    sr_perf = getattr(mission, "_sr_node_perf", {})
    for node_id, perf in sr_perf.items():
        if extracted >= _MAX_AUTO_MEMORIES:
            break
        timeouts = perf.get("timeouts", 0)
        successes = perf.get("successes", 0)
        if timeouts > 0 or successes >= 5:
            save_long_term(
                category="performance",
                key=f"sr_perf_{node_id}",
                value=(f"Showrunner perf on {node_id}: "
                       f"{successes} successes, {timeouts} timeouts, "
                       f"avg_time={perf.get('total_time', 0) / max(successes, 1):.1f}s"),
                source_mission=mid,
                tags=["showrunner", "performance", node_id],
            )
            extracted += 1

    # 5. Promote high-value working memory entries
    container_id = getattr(mission, "container_id", None)
    if container_id:
        wm_entries = read_working_memory(container_id)
        promoted = 0
        for entry in wm_entries:
            if extracted >= _MAX_AUTO_MEMORIES or promoted >= 3:
                break
            content = entry.get("content", "")
            cat = entry.get("category", "")
            if content.startswith("[auto]") or len(content) < 30:
                continue
            if cat in ("decision", "pattern", "error"):
                save_long_term(
                    category="lesson" if cat == "decision" else cat,
                    key=f"wm_{mid}_{promoted}",
                    value=f"[from working memory] {content}",
                    source_mission=mid,
                    tags=_extract_tags(content),
                )
                extracted += 1
                promoted += 1

    return extracted


# ── Helpers ──────────────────────────────────────────────────────────────

_TECH_WORDS = frozenset({
    "python", "javascript", "html", "css", "flask", "react", "node",
    "api", "rest", "server", "http", "database", "sql", "json",
    "docker", "test", "pytest", "curl", "webpack", "npm", "pip",
    "frontend", "backend", "deploy", "auth", "websocket", "chat",
    "crud", "spa", "ssr", "cli", "script", "bash", "shell",
    "fastapi", "express", "django", "sqlite", "mongodb", "redis",
})


def _extract_tags(text):
    words = set(re.findall(r'\w+', text.lower()))
    return sorted(words & _TECH_WORDS)[:10]


def _classify_mission(mission_text):
    text = mission_text.lower()
    if any(w in text for w in ("web app", "website", "html", "frontend", "spa")):
        return "web-app"
    if any(w in text for w in ("api", "rest", "endpoint", "server")):
        return "api-server"
    if any(w in text for w in ("cli", "command-line", "script")):
        return "cli-tool"
    if any(w in text for w in ("test", "benchmark", "stress")):
        return "testing"
    if any(w in text for w in ("analyze", "research", "report")):
        return "analysis"
    return "general"
