# OpenAI-Compatible API (OAPI)

OpenAI-compatible chat completions API for the ClusterFlock cluster. Runs on port **1919** as a separate HTTP server, independently from nCore's main API (port 1903) and the mission system.

Fan-outs user prompts to all available cluster endpoints, uses a showrunner to synthesize a final answer, and returns a standard OpenAI-compatible response.

---

## Architecture

- `nCore/oapi.py` — self-contained server, launches as a background daemon thread when nCore starts
- Shares `registry` and `orchestrator` modules (read-only access to node/endpoint state)
- Does **not** depend on `mission.py`, `session.py`, or any mission code
- Uses `ranking.py` for showrunner election and endpoint selection (shared with other nCore modules)
- Has its own conversation/session management (in-memory, LRU eviction)

### Web UI
- `nCore/web/public/oapi.html` — chat UI served from the main nCore server (port 1903)
- Connected to app via nav tabs (MISSION / COMMAND / STATUS / **OAPI**)

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/v1/models` | List available models (OpenAI format) |
| POST | `/v1/chat/completions` | Chat completions (non-streaming) |
| GET | `/api/oapi/status` | Queue depth, showrunner info, endpoint list, config |
| GET | `/api/oapi/config` | Current mode, thinking power, max_tokens, available models |
| PUT | `/api/oapi/config` | Update mode, thinking power, max_tokens, manual model |
| GET | `/api/oapi/conversations` | List all conversations (newest first) |
| GET | `/api/oapi/conversations/:id` | Get full conversation with all turns |
| DELETE | `/api/oapi/conversations` | Clear all conversation history |
| DELETE | `/api/oapi/conversations/:id` | Delete one conversation |

### `GET /v1/models`
Returns `clusterflock` as the primary virtual model, plus all individual endpoint models currently loaded.

```json
{
  "object": "list",
  "data": [
    {"id": "clusterflock", "object": "model", "owned_by": "clusterflock"},
    {"id": "qwen/qwen3-30b-a3b", "object": "model", "owned_by": "clusterflock"}
  ]
}
```

### `POST /v1/chat/completions`
Non-streaming. Accepts standard OpenAI request format.

**Request:**
```json
{
  "model": "clusterflock",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is an AU in astronomy?"}
  ],
  "_conversation_id": "optional-existing-conv-id"
}
```

Forwarded sampling parameters: `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `max_tokens`.

**Response:**
```json
{
  "id": "chatcmpl-abc123def456",
  "object": "chat.completion",
  "created": 1711612800,
  "model": "clusterflock",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "An AU is a unit of distance..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 25, "completion_tokens": 42, "total_tokens": 67},
  "_clusterflock": {
    "mode": "fanout",
    "endpoints_queried": 6,
    "endpoints_responded": 4,
    "reuse_rounds": 2,
    "thinking_power": 60,
    "showrunner": "qwen/qwen3-30b-a3b",
    "showrunner_host": "ncore-host",
    "event_log": [
      {"t": 1711612800.1, "msg": "showrunner: qwen/qwen3-30b-a3b on ncore-host"},
      {"t": 1711612800.2, "msg": "endpoints: 4 ready, thinking_power: 60s"},
      {"t": 1711612800.3, "msg": "dispatched to mistral-7b on ai"},
      {"t": 1711612802.5, "msg": "response from mistral-7b on ai (245 chars)"},
      {"t": 1711612830.0, "msg": "round 1 complete — 30s remaining, re-using endpoints"},
      {"t": 1711612860.0, "msg": "collected 6 responses from 8 dispatches over 2 round(s)"}
    ]
  },
  "_conversation_id": "a1b2c3d4e5f6g7h8"
}
```

**Error codes:**
- `400` — invalid JSON, missing messages, or malformed message objects
- `429` — queue full (`rate_limit_exceeded`)
- `503` — no showrunner available or all endpoints timed out

---

## Routing modes

Three modes, set via `PUT /api/oapi/config`:

### Fanout (default)
Dispatches the prompt to **all** available endpoints, collects responses, then the showrunner synthesizes a single authoritative answer.

### Speed
Routes to the single fastest endpoint whose context window fits the prompt. No synthesis — returns the raw response.

### Manual
Routes to a specific user-selected model. Configured by setting `manual_model` (either `"model_name"` or `"model_name@hostname"`).

---

## Fanout: how it works

### Phase 1: Dispatch
1. Elect a showrunner (best model, tier ≥ 2, highest composite score)
2. Gather all ready endpoints across the cluster
3. Dispatch the **full conversation** to ALL endpoints simultaneously
4. For endpoints with smaller context windows, truncate conversation history (always preserve system prompt + last user message)

### Phase 2: Collect with endpoint re-use
- Poll for responses within the **thinking power** window (default 60s)
- As each response arrives, feed it to the showrunner for a brief evaluation (1–2 sentences: errors, strengths, gaps)
- **Endpoint re-use**: When all endpoints respond before the thinking power deadline, dispatch to all endpoints again for additional perspectives. Continues until < 5s remain. Each round adds more data for synthesis.

### Phase 3: Synthesis
- Send the showrunner a synthesis prompt containing:
  - The original system prompt and conversation
  - All collected endpoint responses (truncated to 4000 chars each)
  - The showrunner's own evaluations from Phase 2
- Showrunner system prompt instructs: evaluate all responses, correct errors, produce one authoritative answer. Do not mention endpoints or the synthesis process.
- Response returned in OpenAI-compatible format

### Showrunner failover
If the showrunner fails during synthesis:
1. Re-elect a new showrunner (exclude failed node)
2. Pass accumulated knowledge (all endpoint responses + evaluations) to the new showrunner
3. New showrunner performs synthesis with full context

If no showrunner available after failover → return the best single-endpoint response (ranked by composite score).

---

## Showrunner election

Uses `ranking.py`. Score = **tps × tier² × ctx_bonus** where:
- `tier` = 1–3 based on model parameter count (≥27B → 3, ≥7B → 2, else → 1)
- `ctx_bonus` = `1.0 + log₂(ctx / 4096) × 0.25`
- Minimum tier 2 required (7B+)

The showrunner is re-elected automatically when the current one goes offline or its model is unloaded. Election is independent from the mission system's showrunner.

---

## Context truncation

When conversation exceeds a model's context window:
- **Always preserved**: system prompt (first message) + last user message
- **Trimmed**: middle conversation history from oldest, keeping most recent turns
- **Budget**: 60% of model's loaded context_length (estimated at 4 chars/token)

---

## Concurrency

- Requests are serialized — one active request at a time via a processing lock
- Up to 5 requests can be queued (excess returns HTTP 429)
- Each request holds the lock for the entire fan-out + synthesis cycle

---

## Conversations

In-memory conversation store with LRU eviction.

- Auto-titled from first user message (first 80 chars)
- Max 50 conversations stored (oldest evicted)
- Max 50 turns per conversation (oldest trimmed)
- Pass `_conversation_id` in request to continue an existing conversation
- Omit to start a new one (server generates the ID)

---

## Configuration

| Setting | Default | Description |
|---|---|---|
| Port | 1919 | OAPI server port |
| Max queue | 5 | Queued requests before 429 |
| Fanout timeout | 60s | Max wait for endpoint responses |
| Synthesis timeout | 120s | Max wait for showrunner synthesis |
| Thinking power | 60s | Collection window for fanout mode (10–300s) |
| Max tokens | 0 | Global token limit (0 = no limit, per-request overrides) |
| Max conversations | 50 | Stored conversations (LRU eviction) |
| Max history turns | 50 | Turns per conversation |
| Reuse min gap | 5s | Minimum time remaining to dispatch another re-use round |

Thinking power and max_tokens are configurable at runtime via `PUT /api/oapi/config` and persisted to state.json.

---

## What's not supported

- Streaming (`stream: true`)
- Function calling / tools
- Multiple choices (`n > 1`)
- Embeddings / legacy completions endpoint
- Image / multimodal inputs

---

## Example usage

### curl
```bash
curl http://localhost:1919/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "clusterflock",
    "messages": [{"role": "user", "content": "Explain quantum entanglement"}]
  }'
```

### Python (openai library)
```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:1919/v1", api_key="unused")
response = client.chat.completions.create(
    model="clusterflock",
    messages=[{"role": "user", "content": "Explain quantum entanglement"}]
)
print(response.choices[0].message.content)
```

### Any OpenAI-compatible client
Point the base URL to `http://<ncore-host>:1919/v1` with any API key (authentication is not required).
