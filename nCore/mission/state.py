"""Mission data structures, constants, and global state.

All shared state (_lock, _missions) lives here so every submodule
imports from the same canonical location.
"""

import secrets
import threading
import time
from collections import deque
from pathlib import Path

# ── Global lock & mission registry ────────────────────────────────────────

_lock = threading.Lock()
_missions: dict = {}  # mission_id → MissionState

# ── Constants ─────────────────────────────────────────────────────────────

_MAX_MISSIONS = 5
_MAX_CONCURRENT = 3
_CONTAINER_CPUS = "4"
_CONTAINER_MEM = "4g"
_PROMPT_STACK_MAX = 3
_FLOCK_RENAME_COOLDOWN = 60

# Autonomous agent
_AUTO_MAX_ITERATIONS = 100
_AUTO_MAX_SHELL = 200
_AUTO_TIMEOUT = 7200
_AUTO_CHECKPOINT_INTERVAL = 5
_AUTO_CHECKPOINT_SECONDS = 120

# Conversation compaction
_COMPACTION_INTERVAL = 15
_COMPACTION_TIMEOUT = 180

# Context budget
_CONTEXT_BUDGET_FRACTION = 0.80
_CHARS_PER_TOKEN = 3
_MIN_CONTEXT_BUDGET = 12000
_PREFLIGHT_HEADROOM = 0.90
_PREFLIGHT_MIN_HISTORY = 2
_MAX_CONTEXT_RETRIES = 2
_WORKSPACE_TREE_MAX_ENTRIES = 400

# Shell timeouts
_SHELL_TIMEOUT_DEFAULT = 600
_SHELL_TIMEOUT_INSTALL = 1800

# Docker
_DOCKER_NETWORK = "mission-net"
_CONTAINER_IMAGE = "ubuntu:24.04"
_CONTAINER_IMAGE_PREBAKED = "cf-mission:latest"

# Persistence
_MISSIONS_FILE = Path(__file__).resolve().parent.parent / "missions.json"
_WATCHDOG_INTERVAL = 1800

# Quality tiers
_QUALITY_TIERS = {
    "120b": 3, "70b": 3, "72b": 3, "65b": 3, "34b": 3, "35b": 3, "32b": 3, "27b": 3,
    "14b": 2, "13b": 2, "12b": 2, "8b": 2, "7b": 2, "9b": 2,
    "4b": 1, "3b": 1, "2b": 1, "1b": 1, "0.5b": 1, "0.6b": 1,
}

# Mission phases
_MISSION_PHASES = ("planning", "working", "verifying", "completing")

# Agent iteration control
_MAX_CONSECUTIVE_FAILURES = 5
_AGENT_READONLY_FIRST_ITER = True
_READONLY_ACTIONS = frozenset({
    "read_file", "workspace_tree", "search", "shell",
})
_READONLY_SHELL_PREFIXES = (
    "ls", "cat", "head", "tail", "find", "grep", "wc", "file", "stat",
    "which", "echo", "pwd", "env", "printenv", "whoami", "hostname",
    "tree", "du", "df", "uname", "date", "id", "test",
)
_SYNTAX_CHECK_EXTENSIONS = frozenset({"py", "js", "mjs", "ts", "json", "sh", "bash"})

# Hard safety caps
_SHELL_STDOUT_HARD_CAP = 8000
_ACTION_RESULT_HARD_CAP = 12000


# ── Data structures ──────────────────────────────────────────────────────

class FlockAgent:
    __slots__ = (
        "endpoint_id", "node_id", "hostname", "model", "name",
        "role", "experience", "toks_per_sec", "context_length",
        "gpu_name", "status", "failures", "last_used", "assigned_task",
        "system_prompt", "conversation_history", "scratchpad",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.failures = self.failures or 0
        self.last_used = self.last_used or 0
        self.assigned_task = self.assigned_task or None
        self.status = self.status or "available"
        self.system_prompt = self.system_prompt or ""
        self.conversation_history = self.conversation_history or []
        self.scratchpad = self.scratchpad or {}

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__
             if k not in ("conversation_history", "scratchpad")}
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
        d["scratchpad"] = dict(self.scratchpad) if self.scratchpad else {}
        return d


class PlanTask:
    __slots__ = (
        "id", "title", "type", "deps", "agent_tier",
        "files", "verify_cmd", "status", "result",
        "assigned_agent", "dispatch_count", "artifacts", "failed_agents",
    )

    def __init__(self, **kw):
        self.id = kw.get("id", "")
        self.title = kw.get("title", "")
        self.type = kw.get("type", "implement")
        self.deps = kw.get("deps") or []
        self.agent_tier = kw.get("agent_tier", 1)
        self.files = kw.get("files") or []
        self.verify_cmd = kw.get("verify_cmd")
        self.status = kw.get("status", "pending")
        self.result = kw.get("result")
        self.assigned_agent = kw.get("assigned_agent")
        self.dispatch_count = kw.get("dispatch_count", 0)
        self.failed_agents = kw.get("failed_agents") or []
        self.artifacts = kw.get("artifacts") or []

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.__slots__ if k in d})


class MissionPlan:
    def __init__(self, requirements=None, tasks=None):
        self.requirements: list[str] = requirements or []
        self.tasks: list[PlanTask] = tasks or []

    def progress_summary(self) -> str:
        by_status: dict[str, int] = {}
        for t in self.tasks:
            by_status[t.status] = by_status.get(t.status, 0) + 1
        total = len(self.tasks)
        done = by_status.get("done", 0)
        parts = [f"{done}/{total} done"]
        for s in ("dispatched", "pending", "failed", "skipped"):
            n = by_status.get(s, 0)
            if n:
                parts.append(f"{n} {s}")
        return ", ".join(parts)

    def to_dict(self):
        return {
            "requirements": self.requirements,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, d):
        tasks = [PlanTask.from_dict(td) for td in d.get("tasks", [])]
        return cls(requirements=d.get("requirements", []), tasks=tasks)


class AgentTask:
    __slots__ = (
        "task_id", "mission_id", "agent_name", "prompt",
        "status", "result", "error", "retries",
        "created_at", "completed_at", "timeout",
        "capabilities", "constraints",
        "task_context", "checkpoint",
        "_cancel_event", "_sr_guidance",
    )

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
        self.capabilities = kw.get("capabilities", [])
        self.constraints = kw.get("constraints", {})
        self.task_context = kw.get("task_context", [])
        self.checkpoint = kw.get("checkpoint")
        self._cancel_event = threading.Event()
        self._sr_guidance = deque()

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__ if not k.startswith("_")}
        d["cancelled"] = self._cancel_event.is_set()
        return d


class MissionState:
    def __init__(self, mission_id, mission_text=""):
        self.mission_id = mission_id
        self.mission_text = mission_text
        self.mission_version = 1
        self.status = "initializing"
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
        self.showrunner_override = None

        # Flock
        self.flock: dict[str, FlockAgent] = {}
        self.flock_last_update = 0
        self._departed_flock: dict[str, tuple] = {}

        # Tasks
        self.tasks: dict[str, AgentTask] = {}
        self.task_history: list[dict] = []

        # Context management
        self.round_trips = 0
        self.conversation: list[dict] = []
        self.conversation_window_override = None
        self.last_summary = ""
        self.last_summary_at = 0

        # User interaction
        self.pending_prompts: list[dict] = []
        self.user_responses: list[dict] = []
        self.status_message = ""
        self.status_progress = -1

        # Event log
        self.event_log: list[dict] = []

        # Tools, notes, knowledge
        self.tools: list[dict] = []
        self.notes: list[dict] = []
        self.knowledge_base: dict[str, str] = {}

        # Phase tracking
        self.mission_phase = "planning"
        self.phase_history: list[dict] = []

        # Structured plan
        self.plan: MissionPlan | None = None

        # Thread control
        self._thread = None
        self._stop_event = threading.Event()
        self._has_result = False
        self._completion_verified = False

        # Caches
        self._workspace_tree_cache = ""
        self._workspace_tree_at = 0.0
        self._state_json_cache = ""
        self._state_json_at = 0.0

        # Performance tracking
        self._sr_overflow_streak = 0
        self._sr_node_perf = {}
        self._agent_perf = {}

        # Flock offers system (post-plan, pre-working)
        # offers: {agent_name: {"role": str, "offer": str}}
        self._flock_offers: dict[str, dict] = {}
        self._flock_offers_pending: dict[str, dict] = {}  # agent_name → {orch_task_id, timeout, started_at}
        self._flock_offers_collected: bool = False  # True once offers round is done

        # Flock advice system
        # advice: {agent_name: {"milestone": str, "advice": str, "role": str, "experience": str}}
        self._flock_advice: dict[str, dict] = {}
        self._flock_advice_pending: dict[str, dict] = {}  # agent_name → {orch_task_id, timeout, milestone, ...}
        self._advice_milestone_tracker: dict[str, any] = {}  # {"_current": task_id, "_rounds": int}

    def log_event(self, level, message, **extra):
        entry = {
            "timestamp": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": message,
            **extra,
        }
        self.event_log.append(entry)
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
            "tasks_completed": (
                sum(1 for t in self.plan.tasks if t.status == "done")
                if self.plan and self.plan.tasks
                else len(self.task_history)
            ),
            "round_trips": self.round_trips,
            "pending_prompts": self.pending_prompts,
            "status_message": self.status_message,
            "status_progress": self.status_progress,
            "tools": self.tools,
            "notes": self.notes,
            "mission_phase": self.mission_phase,
            "knowledge_base": dict(self.knowledge_base) if self.knowledge_base else {},
            "plan": self.plan.to_dict() if self.plan else None,
            "plan_progress": self.plan.progress_summary() if self.plan else None,
            "event_log_count": len(self.event_log),
            "has_result": self._has_result,
            "flock_offers": {k: {"role": v.get("role", ""),
                                  "offer": v.get("offer", "")[:400]}
                             for k, v in self._flock_offers.items()},
            "flock_advice": {k: {"milestone": v.get("milestone", ""),
                                  "agent": k,
                                  "role": v.get("role", ""),
                                  "experience": v.get("experience", ""),
                                  "advice": v.get("advice", "")[:300]}
                             for k, v in self._flock_advice.items()},

        }
