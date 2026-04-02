# ClusterFlock

ClusterFlock is a distributed AI orchestrator. It manages a cluster of heterogeneous hardware — NVIDIA GPUs, Apple Silicon, DGX systems — loading optimal models on each device and coordinating them to complete autonomous "missions".

**Version**: 0.7.10 &nbsp;|&nbsp; **nCore port**: 1903 &nbsp;|&nbsp; **OAPI port**: 1919

---

## Architecture

Hub-and-spoke. One **nCore** (the hub) orchestrates many **agents** (spokes).

```
                    ┌─────────────────────────────────┐
                    │       nCore (port 1903)          │
                    │  orchestrator · registry · OAPI  │
                    │  missions · access · catalog     │
                    └──────┬──────┬──────┬─────┬──────┘
                           │      │      │     │
              ┌────────────┘      │      │     └────────────┐
              │                   │      │                   │
        agent_spark          agent_linux  agent_mac      agent_lms
        (DGX/GB10)           (amd64+CUDA) (Apple Silicon) (LM Studio)
        llama.cpp+CUDA       llama.cpp    llama.cpp+Metal  LM Studio CLI
```

### nCore modules
| Module | Purpose |
|---|---|
| server.py | HTTP server, all API routing (~60 endpoints), state persistence |
| orchestrator.py | Model planning, autoload, benchmark orchestration |
| mission.py | Autonomous mission execution — showrunner, flock, containers |
| registry.py | Node tracking, health status, heartbeat processing |
| access.py | Admission control — open / approve / allow / deny |
| auth.py | Token generation, SHA-256 hashing, HMAC verification |
| push.py | Push-mode poller — pings remote agents, forwards commands |
| oapi.py | OpenAI-compatible API on port 1919 |
| session.py | Web UI session management |
| catalog.py | Model catalog (loads model_catalog.json) |
| local_agent.py | Manages co-located agent via subprocess |
| ranking.py | Endpoint scoring for routing |
| state.py | Persistent state (state.json) |

### Agent types
| Agent | Hardware | Backend | Multi-GPU | Multi-model |
|---|---|---|---|---|
| agent_spark | DGX Spark / GB10 | llama.cpp (CUDA) | No (single unified) | No (1 at a time) |
| agent_linux | Generic amd64 + CUDA | llama.cpp (prebuilt) | Yes (per-device ports) | Yes (1 per device) |
| agent_mac | Apple Silicon | llama.cpp (Metal) | No (unified) | No (1 at a time) |
| agent_lms | Any (macOS/Linux/Win) | LM Studio | Via LM Studio | Via LM Studio |

All agents share the same internal structure:
- **run.py** — entry point, wires components, calls `link.start()`
- **setup.py** — first-time interactive setup (build → profile → register → service install)
- **commands.py** — command dispatcher
- **server.py** — llama-server lifecycle (or LM Studio API for agent_lms)
- **hardware.py** — zero-dependency hardware profiling
- **link.py** — transport layer (pull / push / local modes)
- **watchdog.py** — supervisor process
- **models_hf.py** — HuggingFace GGUF download & catalog (not in agent_lms)
- **gpu_cleanup.py** — kills LM Studio / Ollama to free GPU (not in agent_lms)

---

## Connection modes

### Pull mode
Agent initiates heartbeats to nCore. Used by remote agents.

1. Agent calls `POST /api/v1/register` with node_id, hostname, hardware
2. nCore returns a Bearer token (or queues for admin approval)
3. Agent heartbeats every 5s to `POST /api/v1/heartbeat`
4. nCore returns commands in heartbeat response

### Push mode
nCore polls agent. Used when the agent can't reach nCore directly.

1. Agent listens on a port and displays a pairing token
2. Admin posts to nCore `POST /api/v1/nodes/push` with agent address + pairing token
3. nCore contacts agent, exchanges tokens via HMAC
4. nCore polls `GET /api/v1/heartbeat` on agent every 5s (configurable 2–30s)

### Local mode
Agent runs on the same machine as nCore. Started via `POST /api/v1/local-agent` which spawns the agent subprocess with `CLUSTERFLOCK_LOCAL=1` environment. No cluster.json, no manual registration — nCore injects the token and node_id automatically.

---

## Access control

Controls which agents can join the cluster. Default mode: **approve**.

| Mode | Behavior |
|---|---|
| open | Accept all registrations immediately |
| approve | New agents go to a pending queue, admin must accept or reject |
| allow | Whitelist by node_id or hostname |
| deny | Blacklist by node_id or hostname |

Toggled via UI (shield button in command screen) or `GET/PUT /api/v1/access`.

---

## Health monitoring

Agents are tracked by last heartbeat time:

| Status | Condition |
|---|---|
| healthy | Last seen < 20s ago |
| stale | Last seen < 60s ago |
| dead | Last seen ≥ 60s ago |
| removed | After 600s with no heartbeat |

Default heartbeat interval: 5 seconds (remotely adjustable by nCore, clamped 2–30s).

---

## Watchdog

All agents run under `watchdog.py`, a supervisor process.

- Spawns `python3 -u run.py` as a child process
- Sets `CLUSTERFLOCK_HEALTH_FILE=/tmp/clusterflock_agent.alive`
- link.py touches the health file on each successful heartbeat
- Watchdog restarts if: child exits (after 3s delay) or health file stale > 45s (SIGTERM → 10s → SIGKILL)
- Rate limit: 5 restarts in 120s → 30s backoff before next restart
- Forwards SIGTERM/SIGINT to child for clean shutdown

---

## OAPI (OpenAI-compatible API)

Port **1919**. Proxies `/v1/chat/completions` and `/v1/models` across cluster endpoints.

Three routing modes:
| Mode | Behavior |
|---|---|
| fanout (default) | Dispatch prompt to all endpoints, showrunner synthesizes responses |
| speed | Route to single fastest endpoint |
| manual | Route to a specific chosen endpoint |

Constants: max queue 5, fanout timeout 60s, synthesis timeout 120s, default thinking power 60s.

Config: `PUT /api/oapi/config` with `{"mode": "speed"|"fanout"|"manual"}`.

---

## Missions

Autonomous AI execution via Docker containers. A **showrunner** (elected LLM) coordinates a **flock** of worker LLMs to complete a user's goal.

### Limits
| Setting | Value |
|---|---|
| Max missions (total) | 5 |
| Max concurrent (running) | 3 |
| Container CPUs | 4 |
| Container memory | 4g |
| Idle timeout | 2 hours |
| Max duration | 7 days |
| Compaction interval | Every 15 round-trips |

### Showrunner election
Score = **tier³ × ctx_bonus² × speed_bonus** — heavily favors intelligence over speed.

Quality tiers (from model parameter count): ≥27B → tier 3, ≥7B → tier 2, else → tier 1.

User can override with a pinned showrunner via API/UI.

### Flock
Showrunner receives a list of all loaded endpoints (excluding itself) and assigns each a three-word persona: name, specialty, experience (e.g. "Albert, programmer, junior"). Names must be unique. Flock roster is rebuilt at most once every 5 minutes.

Flock agents maintain conversation history within a mission. History is compressed when it exceeds 50% of context budget. When agents depart mid-mission, their active tasks are cancelled, marked failed, and the showrunner is notified to reassign.

### Generation limits
Role-based token and timeout budgets:

| Role | max_tokens | gen_timeout |
|---|---|---|
| showrunner | Full context (no cap) | 600s |
| worker (tier 3) | ctx / 3 | Scaled by tps, max 600s |
| worker (tier 2) | ctx / 4 | Scaled by tps, max 600s |
| worker (tier 1) | ctx / 6 | Scaled by tps, max 600s |
| utility | ctx / 8 | Max 300s |

Showrunner can override per-dispatch via `constraints.max_tokens`, `constraints.generation_timeout`, `constraints.no_gen_limit`.

### Context management
- Every 15 round-trips, the showrunner produces a compressed summary. This replaces raw transcript in subsequent prompts.
- If the showrunner's node goes offline, a new one is elected and bootstrapped from the latest summary.
- Mission text is versioned. Changes mid-execution are delivered as a diff with `mission_changed: true`.

### Container lifecycle
- Pre-baked Docker image (`cf-mission:latest`) built from ubuntu:24.04 with curl, wget, python3, pip, jq, git, nodejs, npm, build-essential. Container creation ~2s.
- Containers run on `mission-net` (isolated Docker bridge). Writable at `/home/mission/` and `/tmp/`.
- Containers persist until explicit deletion (not just stop).
- Missions are persisted to `missions.json` and restored on nCore restart (reconnects containers, resumes as paused).
- On delete: container stopped, removed, volume deleted.

### Showrunner actions
The showrunner controls the container via structured JSON actions. Key action types include:
- **run_command** — execute shell command in container
- **write_file** / **read_file** — file I/O in container
- **search** — grep in container (60 lines / 4000 chars)
- **dispatch** — send task to a flock agent
- **wait_for_flock** — block until all active flock tasks complete (default timeout 600s)
- **ask_user** — prompt user for input (pause or non-blocking, max 3 stacked)
- **status_update** — send progress message to UI
- **complete** — mark mission done (triggers self-verification gate first)

### Completion
First "complete" triggers a two-phase verification: showrunner reviews state.json + workspace listing before finalizing. Completion shows a modal overlay in the UI with an ACKNOWLEDGE button.

### Error handling
- Result delivery: 3 retries with exponential backoff (1s, 2s delays). On total failure, posts minimal error payload so orchestrator unblocks.
- Thinking model support: strips `<think>` tags, falls back to `reasoning_content` field.
- VL/vision models excluded from flock.
- Smart task matching: rates task complexity 1–3, warns if tier-1 model assigned complex task.

---

## Model management

### Autoload benchmark
`POST /api/v1/autoload/benchmark` with `{"target_tps": N}`.

- Discovers all GPU devices across cluster
- Tests VRAM fractions [0.25, 0.5, 0.75, 1.0] with 0.85 safety margin, 1.2× overhead multiplier
- For each fraction: picks largest model that fits, loads, benchmarks, evaluates against target tps
- Reverts if bigger model gives worse distance-to-target than previous best
- Sequential per-device processing

### HuggingFace import
UI has an IMPORT HF button. Parses `huggingface.co/org/model` URLs, resolves GGUF repos (tries `{repo}-GGUF` if needed), downloads with quant selector (default Q4_K_M, fallback chain: Q4_K_M → *Q4_* → *.gguf).

### VRAM safety
- Orchestrator: 1.2× overhead on file_size, 15% safety margin (budget = VRAM × 0.85)
- Agent: refuses load if free VRAM < 2048 MB
- Model ops (load/unload/unload_all) run sequentially in heartbeat loop to prevent concurrent OOM

### Agent-specific model settings
| Setting | spark | linux (GPU) | linux (CPU) | mac | lms |
|---|---|---|---|---|---|
| KV cache | q4_0 / q4_0 | q4_0 / q4_0 | f16 / f16 | f16 / f16 | LM Studio |
| Flash attention | on | on | off | on | LM Studio |
| Default context | 131072 | 131072 | 131072 | Auto (8k–131k) | From metadata |
| GPU layers | 9999 | 9999 | 0 | 9999 | LM Studio |
| llama-server port | 8080 | 8080+N (GPU), 8090 (CPU) | — | 8080 | 1234 |
| Allocation retry | 5× (reduce ctx 10%) | 5× (reduce ctx 10%) | — | No | N/A |

---

## Agent specifics

### agent_spark (DGX Spark / GB10)
- 128 GB unified memory, single Blackwell GPU
- Builds llama.cpp from source (CMake, GGML_CUDA=ON, flash attention + all quants)
- KV cache: q4_0 for both keys and values
- Bundled llama.cpp source in `llama_cpp/` subdirectory
- Models stored in `agents/agent_spark/models/`

### agent_linux (Generic amd64 + CUDA)
- Multi-GPU: each device runs independent llama-server on separate port (gpu0=8080, gpu1=8081, ..., cpu=8090)
- GPU pinning via `CUDA_VISIBLE_DEVICES`
- Prebuilt binaries in `build/{cuda12,cpu}/` — host needs only NVIDIA drivers
- CPU/RAM device: toggleable via `configure` command, persisted in cluster.json
- No model splitting — each model runs entirely on one device

### agent_mac (Apple Silicon)
- Unified memory, Metal backend
- KV cache: f16 (lossless)
- Auto context sizing: scales 8k–131k based on available memory
- Build via CMake with Metal enabled

### agent_lms (LM Studio)
- Wraps LM Studio CLI (`lms`) — no direct llama.cpp
- Port 1234 (LM Studio default)
- GPU assignment, KV cache, context handled by LM Studio
- Includes `deploy.py` for non-interactive deployment (profile → download → load → benchmark → register → systemd)
- `studio.py` handles server control, model management, catalog, inference, benchmarking

---

## Authentication

- Bearer tokens: 32-char urlsafe base64 (~256 bits). SHA-256 hash stored server-side, never the raw token.
- HMAC comparison (`hmac.compare_digest`) for constant-time verification.
- Pull mode: token returned at registration, agent stores in cluster.json.
- Push mode: mutual token exchange during pairing.
- Local mode: token injected via environment variable.

---

## Security boundaries (mission containers)

- `--network=mission-net` — isolated Docker bridge, no access to host network or other containers.
- Egress: DNS and HTTP/HTTPS to public internet only. Private ranges blocked (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16).
- No volume mounts to host filesystem. Storage is ephemeral (tmpfs + named volume for `/home/mission/`).
- Containers run with `--cap-drop=ALL --cap-add=NET_RAW,SYS_CHROOT`.
- All commands executed inside the container are logged for audit.
- Showrunner and agents never run host system commands.

---

## State persistence

nCore saves to `state.json`: auth tokens (hashed), access control lists/mode, push-mode node configs, cluster settings.

Missions saved to `missions.json`: created/stopped/paused/resumed/completed/deleted transitions + periodic watchdog saves. Restored on nCore restart (containers reconnected, missions resume as paused).

---

## Deployment

### Starting nCore
```bash
python3 nCore/run.py --host 0.0.0.0 --port 1903
```

### Starting an agent (remote)
```bash
# With watchdog (recommended):
python3 watchdog.py --port 1903

# First-time setup:
python3 run.py setup
```

### Starting a local agent (co-located with nCore)
```bash
curl -s -X POST http://127.0.0.1:1903/api/v1/local-agent \
  -H "Content-Type: application/json" \
  -d '{"agent_type": "agent_spark"}'
```

### Deploy via rsync
```bash
rsync -avz --exclude __pycache__ --exclude '*.pyc' --exclude .git \
  nCore/ user@<ncore-host>:~/ClusterFlock/nCore/

rsync -avz --exclude __pycache__ --exclude '*.pyc' --exclude models \
  --exclude build --exclude cluster.json \
  agents/agent_linux/ user@<agent-host>:~/ClusterFlock/agents/agent_linux/
```

---

## Prompts

Showrunner system prompt includes:
- GAN-style thinking framework — specific critiques, concrete suggestions, rethink and reassess, iterate towards best answer
- Requirement extraction from mission text
- Time awareness
- Pre-completion self-evaluation gate
