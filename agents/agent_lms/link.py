"""nCore transport layer — pure connectivity, zero domain knowledge.

Callback-driven: the caller provides payload_fn() and command_fn(cmd).
link.py never imports hardware, studio, or commands.

Public surface:
    start(config, *, payload_fn, command_fn, port=1903)
    mode()      → "pull" | "push" | "local" | None
    connected() → bool
"""

import hmac
import json
import os
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CONFIG = Path(__file__).parent / "cluster.json"
_HB_SEC = 5
_MIN_HB = 2
_MAX_HB = 30

# Async job tracking (download_and_load)
_jobs = {}          # {job_id: {"state": ..., "error": ..., "started": ...}}
_job_lock = threading.Lock()
_job_seq = 0


def _start_async_job(command_fn, body):
    """Run a download_and_load command in the background, return job_id."""
    global _job_seq
    with _job_lock:
        _job_seq += 1
        job_id = f"dl-{int(time.time())}-{_job_seq}"
        _jobs[job_id] = {"state": "downloading", "error": None, "started": time.time()}
        # Clean up finished jobs older than 1 hour
        cutoff = time.time() - 3600
        for k in [k for k, v in _jobs.items() if v["state"] in ("done", "error") and v.get("started", 0) < cutoff]:
            del _jobs[k]

    mid = body.get("model_id", "")
    print(f"[job] {job_id} started: download_and_load {mid}")

    def run():
        try:
            command_fn(body)
            with _job_lock:
                _jobs[job_id]["state"] = "done"
            print(f"[job] {job_id} done")
        except Exception as e:
            with _job_lock:
                _jobs[job_id]["state"] = "error"
                _jobs[job_id]["error"] = str(e)
            print(f"[job] {job_id} error: {e}")

    threading.Thread(target=run, daemon=True).start()
    return job_id


_MAX_BACKOFF = 60

_mode = None
_connected = False
_health_file = os.environ.get("CLUSTERFLOCK_HEALTH_FILE")


def _touch_health():
    """Touch the watchdog health file (if running under watchdog)."""
    if _health_file:
        try:
            Path(_health_file).touch()
        except OSError:
            pass


def mode():
    return _mode


def connected():
    return _connected


# ── Public entry ─────────────────────────────────────────────────────────

def start(config, *, payload_fn, command_fn, port=1903):
    """Connect to nCore and run forever.

    payload_fn()    → dict   heartbeat / pair payload
    command_fn(cmd) → dict | None   handle orchestrator command
    """
    # Local mode: nCore started us with env vars — skip negotiation
    if os.environ.get("CLUSTERFLOCK_LOCAL") == "1":
        _local(config, payload_fn, command_fn)
        return

    m = config.get("mode")
    if m == "pull":
        _pull(config, payload_fn, command_fn)
    elif m == "push":
        _push(config, payload_fn, command_fn, port)
    else:
        _negotiate(config, payload_fn, command_fn, port)


# ── Helpers ──────────────────────────────────────────────────────────────

def _save(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def _post(url, data, token=None, timeout=10):
    body = json.dumps(data).encode()
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fmt_duration(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h = secs // 3600
    m = (secs % 3600) // 60
    return f"{h}h {m}m"


def _apply_hb_interval(interval):
    """Apply heartbeat interval from nCore response (clamped to 2–30s)."""
    global _HB_SEC
    if interval is None:
        return
    try:
        val = int(interval)
        val = max(_MIN_HB, min(_MAX_HB, val))
        if val != _HB_SEC:
            print(f"\n  [heartbeat] interval changed: {_HB_SEC}s → {val}s")
            _HB_SEC = val
    except (TypeError, ValueError):
        pass


# ── Local mode ───────────────────────────────────────────────────────────

def _local(config, payload_fn, command_fn):
    """Run as a local agent — nCore on same machine, no auth negotiation."""
    global _mode, _connected
    _mode = "local"

    ncore_port = os.environ.get("CLUSTERFLOCK_NCORE_PORT", "1903")
    token = os.environ.get("CLUSTERFLOCK_LOCAL_TOKEN", "")
    node_id = os.environ.get("CLUSTERFLOCK_NODE_ID", config.get("node_id", ""))
    address = f"http://127.0.0.1:{ncore_port}"

    # Override config for the pull loop
    config = dict(config)
    config["address"] = address
    config["token"] = token
    config["mode"] = "pull"
    config["node_id"] = node_id

    W = 58
    ver = config.get("agent_version", "?")
    print(f"\n{'─' * W}")
    print(f"  nNode · {node_id} (local) v{ver}")
    print(f"  nCore · {address} (local, every {_HB_SEC}s)")
    print(f"{'─' * W}")

    _connected = True
    _pull(config, payload_fn, command_fn)


# ── Negotiation ──────────────────────────────────────────────────────────

def _negotiate(config, payload_fn, command_fn, port):
    global _mode
    address = config.get("address", "")

    if address:
        backoff = 5
        while True:
            print(f"Connecting to {address}...")
            try:
                token = _try_register(config)
            except KeyboardInterrupt:
                print("\nAborted.")
                return
            if token:
                config["mode"] = "pull"
                config["token"] = token
                _save(config)
                _mode = "pull"
                _pull(config, payload_fn, command_fn)
                return
            print(f"  nCore unreachable — retry in {backoff}s (Ctrl+C to abort)")
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                print("\nAborted.")
                return
            backoff = min(backoff * 2, _MAX_BACKOFF)
    else:
        ans = input("\nRun local server for nCore to connect to? [y/N] ").strip().lower()
        if ans == "y":
            config["mode"] = "push"
            config["listen_port"] = port
            _save(config)
            _mode = "push"
            _push(config, payload_fn, command_fn, port)
        else:
            print("No connection established.")


def _try_register(config):
    url = f"{config['address']}/api/v1/register"
    body = json.dumps({
        "node_id": config.get("node_id", ""),
        "hostname": config.get("hostname", ""),
        "hardware": config.get("hardware"),
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("token"):
                return data["token"]
    except urllib.error.HTTPError as e:
        if e.code == 202:
            # Pending admin approval — poll until accepted
            print("  Awaiting admin approval on nCore dashboard...")
            while True:
                time.sleep(_HB_SEC)
                try:
                    req = urllib.request.Request(
                        url, data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp2:
                        data = json.loads(resp2.read())
                        if data.get("token"):
                            print("  Approved!")
                            return data["token"]
                except urllib.error.HTTPError as e2:
                    if e2.code == 202:
                        continue  # still pending
                    if e2.code == 403:
                        print("  Rejected by admin.")
                        return None
                    print(f"  {e2}")
                    return None
                except Exception as e2:
                    print(f"  {e2}")
                    return None
        else:
            print(f"  HTTP {e.code}: {e.reason}")
            return None
    except Exception as e:
        print(f"  {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════
#  PULL — outbound heartbeat loop, no server
# ═════════════════════════════════════════════════════════════════════════

def _pull(config, payload_fn, command_fn):
    global _mode, _connected
    _mode = "pull"
    _connected = False
    address = config.get("address", "")
    token = config.get("token", "")
    node_id = config.get("node_id", "")
    hostname = config.get("hostname", "")

    if not address:
        print("No cluster address configured.")
        return

    W = 58
    ver = config.get("agent_version", "?")
    print(f"\n{'─' * W}")
    print(f"  nNode · {node_id} ({hostname}) v{ver}")
    print(f"  nCore · {address} (every {_HB_SEC}s)")
    print(f"{'─' * W}")

    # If no token yet (nCore was down during setup), register first
    if not token:
        reg_backoff = 5
        while not token:
            print(f"  No token — attempting registration with {address}...")
            try:
                t = _try_register(config)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                return
            if t:
                token = t
                config["token"] = token
                _save(config)
                print("  ✓ Registered")
                break
            print(f"  ✗ nCore unreachable — retry in {reg_backoff}s")
            try:
                time.sleep(reg_backoff)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                return
            reg_backoff = min(reg_backoff * 2, _MAX_BACKOFF)

    beats = 0
    fails = 0
    backoff = 1
    t0 = time.time()

    while True:
        try:
            payload = payload_fn()
            resp = _post(f"{address}/api/v1/heartbeat", payload, token)
            _connected = True
            _touch_health()
            backoff = 1
            beats += 1

            # Compact stats line
            metrics = payload.get("metrics", {})
            gpus = metrics.get("gpu", [])
            sys_m = metrics.get("system", {})
            n_models = len(payload.get("endpoints", []))
            ctx_tokens = payload.get("context_tokens", 0)
            gpu_pct = gpus[0].get("utilization_pct", 0) if gpus else 0
            vram_free = gpus[0].get("vram_free_mb", 0) if gpus else 0
            cpu_pct = sys_m.get("cpu_pct", 0)
            ram_free = sys_m.get("ram_free_mb", 0)
            up = _fmt_duration(time.time() - t0)

            ctx_str = f"{ctx_tokens // 1000}K" if ctx_tokens >= 1000 else str(ctx_tokens)
            line = (f"  \u25cf \u2191{beats} | {n_models} model(s) {ctx_str} ctx | "
                    f"GPU {gpu_pct}% VRAM {vram_free:,}MB | "
                    f"CPU {cpu_pct}% RAM {ram_free:,}MB | {up}")
            print(f"\r{line:<{W}}", end="", flush=True)

            for cmd in resp.get("commands", []):
                print()  # newline before command output
                act = cmd.get("action", "")
                if act == "restart_agent":
                    print("  [link] restart_agent command received — exiting for watchdog restart")
                    os._exit(0)
                elif act in ("load", "unload", "unload_all"):
                    # Serialize model ops — concurrent loads cause OOM
                    _run_cmd(cmd, command_fn, address, token)
                else:
                    threading.Thread(
                        target=_run_cmd,
                        args=(cmd, command_fn, address, token),
                        daemon=True,
                    ).start()

            # Accept remote heartbeat interval from nCore
            _apply_hb_interval(resp.get("heartbeat_interval"))
        except KeyboardInterrupt:
            print(f"\n\n  Stopped after {beats} heartbeats ({fails} failed).\n")
            return
        except Exception as e:
            _connected = False
            fails += 1
            print(f"\n  ✗ {e} — retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
            continue
        time.sleep(_HB_SEC)


def _run_cmd(cmd, command_fn, address, token):
    try:
        result = command_fn(cmd)
        if result is not None and cmd.get("task_id"):
            _post(f"{address}/api/v1/results/{cmd['task_id']}",
                  result, token, timeout=cmd.get("ttl", 120))
    except Exception as e:
        print(f"[cmd:{cmd.get('action')}] {e}")


# ═════════════════════════════════════════════════════════════════════════
#  PUSH — inbound HTTP server, orchestrator polls us
# ═════════════════════════════════════════════════════════════════════════

_lock = threading.Lock()
_ctx = {}          # populated once in _push()


def _push(config, payload_fn, command_fn, port):
    global _mode, _connected
    _mode = "push"
    orch_token = config.get("orchestrator_token")

    with _lock:
        _ctx.update(
            paired=bool(orch_token),
            orch_token=orch_token,
            config=config,
            payload_fn=payload_fn,
            command_fn=command_fn,
        )

    _connected = bool(orch_token)
    port = config.get("listen_port", port)
    tag = "resumed" if orch_token else "awaiting pairing"
    print(f"[push] listening 0.0.0.0:{port} ({tag})")

    # Background thread to keep health file fresh while server is alive
    def _push_health_loop():
        while True:
            _touch_health()
            time.sleep(_HB_SEC)

    if _health_file:
        threading.Thread(target=_push_health_loop, daemon=True).start()

    HTTPServer.allow_reuse_address = True
    httpd = HTTPServer(("0.0.0.0", port), _PushHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.server_close()


class _PushHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    # ── Routing ──────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/api/v1/heartbeat":
            return self._heartbeat()
        if self.path.startswith("/api/v1/jobs/"):
            return self._job_status()
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/v1/pair":
            return self._pair()
        if self.path == "/api/v1/command":
            return self._command()
        self._reply(404, {"error": "not found"})

    # ── Pair ─────────────────────────────────────────────────────────

    def _pair(self):
        body = self._body()
        if body is None:
            return
        with _lock:
            if _ctx["paired"]:
                return self._reply(409, {"error": "already paired"})
        tok = body.get("orchestrator_token", "")
        if not tok:
            return self._reply(400, {"error": "orchestrator_token required"})

        global _connected
        with _lock:
            _ctx["orch_token"] = tok
            _ctx["paired"] = True
            cfg = _ctx["config"]
        _connected = True
        if cfg:
            cfg["orchestrator_token"] = tok
            _save(cfg)
        print("[push] paired with orchestrator")
        self._reply(200, _ctx["payload_fn"]())

    # ── Heartbeat ────────────────────────────────────────────────────

    def _heartbeat(self):
        if not self._auth():
            return
        _touch_health()
        self._reply(200, _ctx["payload_fn"]())

    # ── Command ──────────────────────────────────────────────────────

    def _command(self):
        if not self._auth():
            return
        body = self._body()
        if body is None:
            return
        if body.get("action") == "restart_agent":
            self._reply(200, {"ok": True, "restarting": True})
            print("[push] restart_agent command received — exiting for watchdog restart")
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)),
                             daemon=True).start()
            return
        # download_and_load: run async, return job_id immediately
        if body.get("action") == "download_and_load":
            job_id = _start_async_job(_ctx["command_fn"], body)
            self._reply(200, {"ok": True, "job_id": job_id})
            return
        try:
            result = _ctx["command_fn"](body)
            self._reply(200, result if result is not None else {"ok": True})
        except ValueError as e:
            self._reply(400, {"error": str(e)})
        except Exception as e:
            self._reply(500, {"error": str(e)})

    # ── Job status ───────────────────────────────────────────────────

    def _job_status(self):
        if not self._auth():
            return
        job_id = self.path.rsplit("/", 1)[-1]
        with _job_lock:
            job = _jobs.get(job_id)
        if job is None:
            return self._reply(404, {"error": "job not found"})
        self._reply(200, dict(job))

    # ── Auth ─────────────────────────────────────────────────────────

    def _auth(self):
        with _lock:
            if not _ctx.get("paired"):
                self._reply(403, {"error": "not paired"})
                return False
            expected = _ctx.get("orch_token")
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Bearer ") or not hmac.compare_digest(hdr[7:], expected):
            self._reply(401, {"error": "invalid token"})
            return False
        return True

    # ── HTTP plumbing ────────────────────────────────────────────────

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except (json.JSONDecodeError, ValueError):
            self._reply(400, {"error": "invalid JSON"})
            return None

    def _reply(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
