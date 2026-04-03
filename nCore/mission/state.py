"""Mission data structures, constants, and global state.

All shared state (_lock, _missions) lives here so every submodule
imports from the same canonical location.
"""

import secrets
import threading
import time
from pathlib import Path

# ── Global lock & mission registry ────────────────────────────────────────
# Every module that touches _missions must acquire _lock first.
# NEVER hold _lock during long operations (network, Docker, inference).

_lock = threading.Lock()

# mission_id → MissionState
_missions: dict = {}

# ── Configurable constants ────────────────────────────────────────────────

_MAX_MISSIONS = 5          # global cap — delete old missions to make room
_MAX_CONCURRENT = 3
_CONTAINER_CPUS = "4"
_CONTAINER_MEM = "4g"
_CONTAINER_DISK = "10g"
_IDLE_TIMEOUT = 7200        # 2 hours — single-user, long-running
_MAX_DURATION = 604800      # 7 days
_PROMPT_STACK_MAX = 3
_FLOCK_RENAME_COOLDOWN = 60   # 1 min
_RETRY_BUDGET = 3           # retries per task per agent

# Autonomous agent defaults
_AUTO_MAX_ITERATIONS = 100
_AUTO_MAX_SHELL = 200
_AUTO_TIMEOUT = 7200         # wall-clock seconds for full autonomous loop (2 hours)
_AUTO_CHECKPOINT_INTERVAL = 5  # iterations between checkpoints
_AUTO_CHECKPOINT_SECONDS = 120 # seconds between checkpoints

# Conversation compaction
_COMPACTION_INTERVAL = 15      # compact every N round-trips (fallback; token-budget trigger is primary)
_COMPACTION_TIMEOUT = 180      # seconds to wait for summary generation

# Context budget — use as much of the loaded context as safely possible
_CONTEXT_BUDGET_FRACTION = 0.80   # use 80% of loaded context for content
_CHARS_PER_TOKEN = 3              # approximate chars-per-token for budget math (conservative for code/JSON)
_MIN_CONTEXT_BUDGET = 12000       # floor even for small models (chars)

# Pre-flight context overflow protection
_PREFLIGHT_HEADROOM = 0.90        # target ≤ 90% of n_ctx for outgoing prompts
_PREFLIGHT_MIN_HISTORY = 2        # always keep at least 2 conversation pairs
_MAX_CONTEXT_RETRIES = 2          # retries after context-overflow 400 (per call)
_WORKSPACE_TREE_MAX_ENTRIES = 400 # max files in recursive tree

# Shell command timeout tiers
_SHELL_TIMEOUT_DEFAULT = 600    # most shell commands (10 min)
_SHELL_TIMEOUT_INSTALL = 1800   # package installs, browser downloads (30 min)

# Docker network
_DOCKER_NETWORK = "mission-net"
_CONTAINER_IMAGE = "ubuntu:24.04"
_CONTAINER_IMAGE_PREBAKED = "cf-mission:latest"

# Mission persistence
_MISSIONS_FILE = Path(__file__).resolve().parent.parent / "missions.json"

# Container GC watchdog
_WATCHDOG_INTERVAL = 1800      # run GC every 30 minutes

# Quality tiers for known model families
_QUALITY_TIERS = {
    "120b": 3, "70b": 3, "72b": 3, "65b": 3, "34b": 3, "35b": 3, "32b": 3, "27b": 3,
    "14b": 2, "13b": 2, "12b": 2, "8b": 2, "7b": 2, "9b": 2,
    "4b": 1, "3b": 1, "2b": 1, "1b": 1, "0.5b": 1, "0.6b": 1,
}

# Smart agent-task matching — complexity keywords
_COMPLEX_TASK_KEYWORDS = frozenset({
    "write", "implement", "create", "build", "design", "architect", "develop",
    "analyze", "research", "report", "debug", "refactor", "optimize",
    "generate", "compose", "synthesize", "evaluate", "review", "plan",
})
_SIMPLE_TASK_KEYWORDS = frozenset({
    "copy", "move", "list", "grep", "find", "format", "rename", "delete",
    "count", "check", "verify", "read", "fetch", "download", "install",
})

# ── Maximum consecutive failures before agent bail-out ────────────────────
_MAX_CONSECUTIVE_FAILURES = 5


# ── Data structures ──────────────────────────────────────────────────────

class FlockAgent:
    """Represents a named agent (an endpoint with a friendly identity)."""
    __slots__ = ("endpoint_id", "node_id", "hostname", "model", "name",
                 "role", "experience", "toks_per_sec", "context_length",
                 "gpu_name", "status", "failures", "last_used", "assigned_task",
                 "system_prompt", "conversation_history")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.failures = self.failures or 0
        self.last_used = self.last_used or 0
        self.assigned_task = self.assigned_task or None
        self.status = self.status or "available"
        self.system_prompt = self.system_prompt or ""
        self.conversation_history = self.conversation_history or []

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__
             if k != "conversation_history"}
        # Include truncated conversation history for UI display
        hist = self.conversation_history or []
        truncated = []
        for msg in hist[-20:]:
            entry = {"role": msg.get("role", "?")}
            content = msg.get("content", "")
            if len(content) > 300:
                content = content[:300] + "…"
            entry["content"] = content
            truncated.append(entry)
        d["conversation_history"] = truncated
        return d


def _flock_status_line(mission):
    """One-liner summarising idle/busy flock members for user-turn prompts."""
    idle, busy = [], []
    for name, agent in mission.flock.items():
        if agent.status == "available":
            idle.append(name)
        else:
            task_info = f" on {agent.assigned_task}" if agent.assigned_task else ""
            busy.append(f"{name}{task_info}")
    parts = []
    if idle:
        parts.append(f"{len(idle)} idle ({', '.join(idle)})")
    if busy:
        parts.append(f"{len(busy)} busy ({', '.join(busy)})")
    if parts:
        return "Flock: " + ", ".join(parts) + "."
    return ""


class AgentTask:
    """A single task dispatched to an agent."""
    __slots__ = ("task_id", "mission_id", "agent_name", "prompt",
                 "status", "result", "error", "retries",
                 "created_at", "completed_at", "timeout",
                 "capabilities", "constraints",
                 "task_context", "checkpoint", "_cancel_event")

    def __init__(self, **kw):
        self.task_id = kw.get("task_id", "mt-" + secrets.token_hex(6))
        self.mission_id = kw.get("mission_id", "")
        self.agent_name = kw.get("agent_name", "")
        self.prompt = kw.get("prompt", "")
        self.status = kw.get("status", "pending")
        self.result = kw.get("result")
        self.error = kw.get("error")
        self.retries = kw.get("retries", 0)
        self.created_at = kw.get("created_at", time.time())
        self.completed_at = kw.get("completed_at")
        self.timeout = kw.get("timeout", 600)
        self.capabilities = kw.get("capabilities", [])  # ["shell","write_file","read_file"]
        self.constraints = kw.get("constraints", {})
        self.task_context = kw.get("task_context", [])   # agent's rolling conversation
        self.checkpoint = kw.get("checkpoint")            # latest progress checkpoint
        self._cancel_event = threading.Event()

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__ if not k.startswith('_')}
        d["cancelled"] = self._cancel_event.is_set()
        return d


class MissionState:
    """Full state for a running mission."""

    def __init__(self, mission_id, mission_text=""):
        self.mission_id = mission_id
        self.mission_text = mission_text
        self.mission_version = 1
        self.status = "initializing"  # initializing, running, paused, completed, error
        self.created_at = time.time()
        self.updated_at = time.time()

        # Container
        self.container_id = None
        self.container_name = f"cf-mission-{mission_id}"

        # Showrunner
        self.showrunner_node_id = None
        self.showrunner_model = None
        self.showrunner_endpoint_id = None
        self.showrunner_score = 0
        self.showrunner_override = None  # {"node_id": ..., "model": ...} or None for auto

        # Flock
        self.flock: dict[str, FlockAgent] = {}  # name → FlockAgent
        self.flock_last_update = 0
        self._departed_flock: dict[str, tuple] = {}  # endpoint_id → (FlockAgent, departed_at)

        # Tasks
        self.tasks: dict[str, AgentTask] = {}  # task_id → AgentTask
        self.task_history: list[dict] = []  # completed tasks

        # Context management
        self.round_trips = 0
        self.conversation: list[dict] = []  # showrunner conversation history
        self.conversation_window_override = None  # showrunner can request wider window
        self.last_summary = ""
        self.last_summary_at = 0

        # User interaction
        self.pending_prompts: list[dict] = []  # user prompts from showrunner
        self.user_responses: list[dict] = []  # responses from user
        self.status_message = ""
        self.status_progress = -1

        # Event log (in-memory, also written to container)
        self.event_log: list[dict] = []

        # Tool manifest
        self.tools: list[dict] = []

        # Persistent scratchpad — always visible in system prompt
        self.notes: list[dict] = []  # [{"key": ..., "value": ...}]

        # Thread control
        self._thread = None
        self._stop_event = threading.Event()
        self._has_result = False

        # Workspace tree cache
        self._workspace_tree_cache = ""
        self._workspace_tree_at = 0.0

        # Stall detection
        self._consecutive_empty = 0
        self._sr_consecutive_fails = 0   # consecutive showrunner timeout/failures
        self._idle_flock_rounds = 0      # consecutive rounds with idle agents and no tasks
        self._sr_overflow_streak = 0     # consecutive showrunner context overflow rounds

    def log_event(self, level, message, **extra):
        """Append to event log."""
        entry = {
            "timestamp": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": message,
            **extra,
        }
        self.event_log.append(entry)
        # Keep log bounded
        if len(self.event_log) > 2000:
            self.event_log = self.event_log[-1500:]
        return entry

    def to_dict(self):
        return {
            "mission_id": self.mission_id,
            "mission_text": self.mission_text,
            "mission_version": self.mission_version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "showrunner": {
                "node_id": self.showrunner_node_id,
                "model": self.showrunner_model,
                "score": self.showrunner_score,
                "override": self.showrunner_override is not None,
            } if self.showrunner_node_id else None,
            "showrunner_override": self.showrunner_override,
            "flock": {name: a.to_dict() for name, a in self.flock.items()},
            "tasks_active": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "tasks_completed": len(self.task_history),
            "round_trips": self.round_trips,
            "pending_prompts": self.pending_prompts,
            "status_message": self.status_message,
            "status_progress": self.status_progress,
            "tools": self.tools,
            "notes": self.notes,
            "event_log_count": len(self.event_log),
            "has_result": self._has_result,
        }
