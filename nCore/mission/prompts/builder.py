"""Phase-aware prompt builder for Showrunner and agent system prompts.

Replaces the monolithic _SHOWRUNNER_SYSTEM string with a structured builder
that adapts prompt sections based on the current mission phase.
"""

from ..state import _MISSION_PHASES


# ── Phase-specific workflow guidance ──────────────────────────────────────

_PHASE_WORKFLOW = {
    "initializing": (
        "═══ CURRENT PHASE: INITIALIZING ═══\n"
        "Container and flock are being set up. Showrunner is being elected.\n"
        "No action needed — the system handles this phase automatically.\n"
    ),
    "planning": (
        "═══ CURRENT PHASE: PLANNING ═══\n"
        "Analyze the mission requirements and create your implementation plan.\n"
        "Break the work into tasks, identify what you'll do vs what to delegate.\n"
        "Write state.json with requirements and task breakdown.\n"
    ),
    "working": (
        "═══ CURRENT PHASE: WORKING ═══\n"
        "You are the lead developer — ALL production code is written by you personally.\n"
        "Your flock agents are your support team for reviews, tests, docs, and research.\n"
        "TEST-FIRST WORKFLOW: Write a test/validation script EARLY — before or alongside your\n"
        "first implementation files. Define expected behavior through tests, then code to pass them.\n"
        "Run tests after each major change. Fix failures immediately, don't accumulate them.\n"
        "After writing core code, dispatch flock agents to review it and write tests.\n"
        "Save memories after key decisions. Checkpoint after milestones.\n"
        "Update state.json task statuses as you complete them.\n"
    ),
    "executing": (
        "═══ CURRENT PHASE: EXECUTING ═══\n"
        "Active implementation phase. You write ALL production code yourself.\n"
        "Dispatch flock agents to review your code, write tests, and create docs.\n"
        "Review completed helper work and integrate their feedback.\n"
    ),
    "verifying": (
        "═══ CURRENT PHASE: VERIFYING ═══\n"
        "Implementation is done. Run all tests, lint checks, and verification.\n"
        "Fix any failing tests or issues found during verification.\n"
        "Review all deliverables against the original requirements.\n"
    ),
    "completing": (
        "═══ CURRENT PHASE: COMPLETING ═══\n"
        "Generating result.html, extracting memories, finalizing state.\n"
        "The system handles this phase automatically.\n"
    ),
}


# ── New actions not in the original prompt ────────────────────────────────

_NEW_ACTIONS_BLOCK = (
    "Version Control & Knowledge:\n"
    '  {"type": "checkpoint", "name": "feature-auth", "description": "Auth module complete, tests passing"}\n'
    '  {"type": "restore", "checkpoint": "feature-auth"}\n'
    '  {"type": "list_checkpoints"}\n'
    '  {"type": "diff_since", "ref": "HEAD~3"}\n'
    '  {"type": "save_knowledge", "key": "db_pattern", "value": "Using SQLAlchemy with async sessions"}\n'
    '  {"type": "advance_phase", "phase": "implementing"}\n\n'
    "Artifacts (register outputs for downstream tasks):\n"
    "  When your task produces outputs that other tasks depend on (API contracts,\n"
    "  schemas, type definitions, config), publish them as artifacts. Dependent tasks\n"
    "  will automatically receive the artifact content in their prompts.\n"
    '  {"type": "publish_artifact", "name": "api-contract", "artifact_type": "contract", '
    '"path": "api/routes.py", "summary": "REST API: GET/POST /tasks, GET/PUT/DELETE /tasks/:id"}\n'
    '  {"type": "publish_artifact", "name": "db-schema", "artifact_type": "schema", '
    '"path": "db/schema.sql", "summary": "Tables: users, tasks, sessions"}\n'
    '  {"type": "publish_artifact", "name": "types", "artifact_type": "file", '
    '"path": "src/types.ts", "summary": "TypeScript interfaces for API request/response types"}\n'
    "  artifact_type: file (default), contract (API specs), schema (data models)\n\n"
    "Test Runner (run tests with structured output parsing):\n"
    '  {"type": "test_runner"}\n'
    "    Auto-detects framework (pytest/jest/mocha) and parses results.\n"
    '  {"type": "test_runner", "command": "python3 -m pytest tests/ -v --tb=short"}\n'
    '  {"type": "test_runner", "command": "npm test", "path": "/home/mission/frontend/"}\n'
    "  Returns: {ok, passed, failed, errors: [{test, message}], output}\n\n"
    "Memory (ACTIVELY maintain your knowledge base):\n"
    "  Your memory is a file-based store at /home/mission/.memory/ — one file per topic.\n"
    "  Memory files survive conversation compaction. Use them to persist anything you'll need later.\n\n"
    "  File operations:\n"
    '    {"type": "memory_create", "path": "architecture.md", "content": "# Architecture\\n- Server: Flask on port 5000\\n- DB: SQLite at data/app.db"}\n'
    '    {"type": "memory_create", "path": "bugs/auth-fix.md", "content": "# Auth Bug\\nFixed null check in auth.py:42"}\n'
    '    {"type": "memory_read", "path": "architecture.md"}\n'
    '    {"type": "memory_update", "path": "architecture.md", "content": "# Architecture\\n(full new content)"}\n'
    '    {"type": "memory_append", "path": "architecture.md", "content": "\\n- Cache: Redis on port 6379"}\n'
    '    {"type": "memory_delete", "path": "bugs/auth-fix.md"}\n'
    '    {"type": "memory_list"}\n\n'
    "  ORGANIZE BY TOPIC — one file per subject (architecture, errors, decisions, test-results, etc.).\n"
    "  Small files (<800B) appear automatically in your context.\n"
    "  Large files only show a preview — use memory_read for full content.\n\n"
    "  Legacy (still works): save_memory writes to notes/{category}.md:\n"
    '    {"type": "save_memory", "category": "decision", "content": "Using Flask because mission needs REST API"}\n\n'
    "  Global memory (cross-mission, for future missions):\n"
    '    {"type": "save_memory", "scope": "global", "key": "flask_rest_pattern", "content": "...", "category": "pattern", "tags": ["flask"]}\n'
    '    {"type": "recall_memory", "query": "flask REST API"}\n\n'
    "  ⚡ YOUR CONVERSATION GETS COMPACTED (summarized) PERIODICALLY.\n"
    "  Summaries are lossy — specific error messages, line numbers, and reasoning get lost.\n"
    "  Memory files survive compaction because they're files, not conversation.\n"
    "  SAVE anything specific you'll need later:\n"
    "    - Key decisions and WHY (before you forget the reasoning)\n"
    "    - Error messages and how you fixed them (exact details)\n"
    "    - Which approaches failed and why (so you don't retry them)\n"
    "    - Test results and what's passing/failing\n"
    "    - Agent performance observations (who's good at what)\n"
    "  Your memory directory listing + small file contents appear in context every round-trip.\n"
)


# ── Enhanced tool creation guidance ───────────────────────────────────────

_TOOL_CREATION_GUIDANCE = (
    "<tools_creation>\n"
    "═══ TOOL CREATION ═══\n"
    "A standard toolkit is pre-installed in /home/mission/tools/ (outline, lint, test, search_def, verify, diff_since).\n"
    "Use run_tool to invoke them. Create mission-specific tools when you notice patterns:\n\n"
    "WHEN TO CREATE TOOLS:\n"
    "- After running the same shell command 2+ times → make it a tool\n"
    "- After grepping for the same pattern twice → make a search tool\n"
    "- When agents need a shared validation check → make a verify tool\n"
    "- When a complex pipeline needs repeating → make a pipeline tool\n\n"
    "CONCRETE EXAMPLES:\n"
    '  {"type": "create_tool", "name": "check_api", "description": "Test all API endpoints",\n'
    '   "script": "#!/bin/bash\\nset -e\\nfor endpoint in /users /auth /items; do\\n'
    "  status=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3000$endpoint)\\n"
    '  echo \\"$endpoint: $status\\"\\n  if [ \\"$status\\" != \\"200\\" ]; then echo \\"FAIL\\"; exit 1; fi\\ndone\\necho \\"All OK\\""}\n\n'
    '  {"type": "create_tool", "name": "count_todos", "description": "Find all TODO/FIXME in project",\n'
    '   "script": "#!/bin/bash\\ngrep -rn \'TODO\\\\|FIXME\\\\|HACK\\\\|XXX\' /home/mission/ --include=\'*.py\' --include=\'*.js\' --include=\'*.ts\' 2>/dev/null | head -50"}\n\n'
    "Tools are visible to ALL agents via run_tool. Creating shared tools multiplies your flock's effectiveness.\n"
    "</tools_creation>\n\n"
)


# ── Knowledge base injection ─────────────────────────────────────────────

def build_knowledge_section(knowledge_base):
    """Build the MISSION KNOWLEDGE section from shared knowledge base."""
    if not knowledge_base:
        return ""
    lines = ["=== MISSION KNOWLEDGE (shared across all agents) ==="]
    for k, v in knowledge_base.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    return "\n".join(lines)


# ── Phase-aware prompt assembly ──────────────────────────────────────────

def build_phase_section(phase):
    """Return the phase-specific workflow guidance."""
    return _PHASE_WORKFLOW.get(phase, _PHASE_WORKFLOW["executing"])


def build_new_actions_section():
    """Return the documentation block for new actions."""
    return _NEW_ACTIONS_BLOCK


def build_tool_creation_section():
    """Return enhanced tool creation guidance."""
    return _TOOL_CREATION_GUIDANCE
