"""Phase-aware prompt builder for Showrunner and agent system prompts.

Replaces the monolithic _SHOWRUNNER_SYSTEM string with a structured builder
that adapts prompt sections based on the current mission phase.
"""

from ..state import _MISSION_PHASES


# ── Phase-specific workflow guidance ──────────────────────────────────────

_PHASE_WORKFLOW = {
    "planning": (
        "═══ CURRENT PHASE: PLANNING ═══\n"
        "You are in the PLANNING phase. Your goal is to create a structured plan before any code is written.\n\n"
        "MANDATORY PLANNING OUTPUTS:\n"
        "1. Create /home/mission/state.json with:\n"
        "   - 'requirements': array of specific, testable requirements extracted from mission text\n"
        "   - 'phases': ordered list of implementation phases with descriptions\n"
        "   - 'architecture': key technical decisions (frameworks, patterns, structure)\n"
        "   - 'file_map': planned files with purpose and estimated size\n"
        "2. Use save_note to record architecture decisions\n"
        "3. Run workspace_tree and outline tool to understand any existing code\n\n"
        "DO NOT write code yet. Inspect, plan, then advance_phase to 'scaffolding'.\n"
        "A good plan prevents 80% of agent failures. Invest time here.\n"
    ),
    "scaffolding": (
        "═══ CURRENT PHASE: SCAFFOLDING ═══\n"
        "Create the project skeleton. Set up the structure before filling in details.\n\n"
        "SCAFFOLDING CHECKLIST:\n"
        "1. Create directory structure matching your plan's file_map\n"
        "2. Use 'scaffold' action for known project types, or create skeleton files manually\n"
        "3. Install dependencies (pin versions)\n"
        "4. Create stub files with function signatures and docstrings — no implementation yet\n"
        "5. Create test stubs — tests that will pass once implementation is complete\n"
        "6. Run 'outline' tool to verify structure matches plan\n"
        "7. Create a checkpoint: {'type': 'checkpoint', 'name': 'scaffold-complete'}\n\n"
        "DELEGATE: Dispatch scaffolding tasks to agents in parallel when possible.\n"
        "When scaffold matches plan, advance_phase to 'implementing'.\n"
    ),
    "implementing": (
        "═══ CURRENT PHASE: IMPLEMENTING ═══\n"
        "Build features incrementally. Each agent task should produce complete, tested code.\n\n"
        "IMPLEMENTATION STRATEGY:\n"
        "1. Work feature-by-feature, not file-by-file\n"
        "2. For each feature: implement → test → verify → checkpoint\n"
        "3. For files >100 lines: write skeleton first, then flesh out section by section\n"
        "4. After each agent completes: read their output, run tests, verify quality\n"
        "5. Use save_knowledge for discoveries agents should share (APIs, patterns, gotchas)\n"
        "6. Create checkpoints after each major feature: {'type': 'checkpoint', 'name': 'feature-X'}\n"
        "7. If an approach fails twice, try a completely different approach\n\n"
        "PARALLEL WORK: Dispatch independent features to different agents simultaneously.\n"
        "When all planned features are implemented, advance_phase to 'testing'.\n"
    ),
    "testing": (
        "═══ CURRENT PHASE: TESTING ═══\n"
        "Systematically verify everything works. Fix bugs before declaring victory.\n\n"
        "TESTING CHECKLIST:\n"
        "1. Run the verify tool: {'type': 'run_tool', 'name': 'verify', 'args': ['/home/mission']}\n"
        "2. Run project-specific tests (pytest, npm test, go test, etc.)\n"
        "3. For each failing test: dispatch fix to an agent with the error + relevant file\n"
        "4. Read output files manually — do they look correct?\n"
        "5. For web projects: check HTML renders, scripts work\n"
        "6. For APIs: run curl tests against endpoints\n"
        "7. Use diff_since to review total changes from initial state\n\n"
        "DO NOT advance until ALL tests pass and verify tool reports no failures.\n"
        "When ready, advance_phase to 'verifying'.\n"
    ),
    "verifying": (
        "═══ CURRENT PHASE: VERIFYING ═══\n"
        "Final quality check. Compare deliverables against every requirement.\n\n"
        "VERIFICATION PROTOCOL:\n"
        "1. Re-read the original mission text — what EXACTLY was asked?\n"
        "2. Read state.json — check EVERY requirement\n"
        "3. For each requirement: read the relevant files, run the relevant tests\n"
        "4. Mark each requirement as verified:true in state.json\n"
        "5. Check file sizes and word counts against requirements\n"
        "6. Run 'outline' tool — does the project structure match the plan?\n"
        "7. Run 'diff_since' — review all changes holistically\n"
        "8. Dispatch a 'code review' task to your best available agent\n\n"
        "If any requirement is not met, go back and fix it before completing.\n"
        "When ALL requirements verified, advance_phase to 'completing'.\n"
    ),
    "completing": (
        "═══ CURRENT PHASE: COMPLETING ═══\n"
        "Wrap up and deliver. Create the result page and mark mission complete.\n\n"
        "COMPLETION STEPS:\n"
        "1. Create a comprehensive result page with create_result\n"
        "2. Include actual elapsed time, what was built, and verification results\n"
        "3. Run 'verify' tool one final time\n"
        "4. Create a final checkpoint: {'type': 'checkpoint', 'name': 'final'}\n"
        "5. Emit {'type': 'complete', 'summary': '...'} with a detailed summary\n"
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
    '  {"type": "advance_phase", "phase": "implementing"}\n'
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
    return _PHASE_WORKFLOW.get(phase, _PHASE_WORKFLOW["implementing"])


def build_new_actions_section():
    """Return the documentation block for new actions."""
    return _NEW_ACTIONS_BLOCK


def build_tool_creation_section():
    """Return enhanced tool creation guidance."""
    return _TOOL_CREATION_GUIDANCE
