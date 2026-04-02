"""nCore HTTP server — the orchestrator's API surface.

All endpoints use JSON. Node-facing endpoints require Bearer token auth.
Admin endpoints (GET /api/v1/nodes, access control) are unauthenticated
for now — intended for local/operator use only.

Endpoints:
  POST /api/v1/register         Node registration — pull mode (returns token)
  POST /api/v1/heartbeat        Node heartbeat (pull mode, bearer auth)
  POST /api/v1/nodes/push       Pair with a push-mode agent by address
  GET  /api/v1/nodes            List all nodes + health
  GET  /api/v1/nodes/:id        Single node detail
  DELETE /api/v1/nodes/:id      Remove a node

  GET  /api/v1/health           Cluster health summary

  GET  /api/v1/access           Show access mode & lists
  PUT  /api/v1/access           Set access mode / allow / deny

  GET  /api/v1/tokens           List active tokens (metadata only)
  DELETE /api/v1/tokens/:node   Revoke tokens for a node
"""

import json
import mimetypes
import os
import re
import secrets
import time
import urllib.request
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

_WEB_ROOT = Path(__file__).parent / "web" / "public"

_IDENT_RE = re.compile(r'^[\w.@:\-]{1,255}$')

from registry import (
    all_nodes, get_node, register as reg_node,
    heartbeat as hb_node, remove as rm_node, node_count,
    push_configs, restore_push, start_reaper,
)
from auth import generate as gen_token, verify as verify_token, revoke_for_node, list_tokens
from access import (
    is_permitted, status as access_status, set_mode, allow, deny,
    remove as access_remove, enqueue, approve_node, reject_node,
    pending_list, is_pending,
)
import state
import auth as auth_mod
import access as access_mod
import push as push_mod
import orchestrator as orch_mod
import catalog as catalog_mod
import session as session_mod
import mission as mission_mod
import local_agent as local_agent_mod
from version import AGENT_VERSION as _EXPECTED_AGENT_VER
from version import APP_VERSION as _APP_VER

_VER_RE = re.compile(rb'\?v=\d+')
_INCLUDE_RE = re.compile(rb'<!--#include\s+file="([^"]+)"\s*-->')


class Handler(BaseHTTPRequestHandler):
    # Suppress default stderr logging per request
    def log_message(self, fmt, *args):
        pass

    # ── Routing ──────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/api/v1/health":
            self._health()
        elif self.path == "/api/v1/nodes":
            self._list_nodes()
        elif self.path.startswith("/api/v1/nodes/") and "/jobs/" in self.path:
            self._node_job_status()
        elif self.path.startswith("/api/v1/nodes/"):
            self._get_node()
        elif self.path == "/api/v1/access":
            self._json(200, access_status())
        elif self.path == "/api/v1/tokens":
            self._json(200, {"tokens": list_tokens()})
        elif self.path == "/api/v1/pending":
            self._json(200, {"pending": pending_list()})
        elif self.path.startswith("/api/v1/tasks/"):
            self._get_task()
        elif self.path == "/api/v1/catalog":
            self._get_catalog()
        elif self.path == "/api/v1/lock":
            self._json(200, {"locked": orch_mod.is_locked()})
        elif self.path == "/api/v1/config":
            self._json(200, {"locked": orch_mod.is_locked(), "tight_pack": orch_mod.is_tight_pack(),
                             "heartbeat_interval": push_mod.get_interval()})
        elif self.path == "/api/v1/sessions":
            self._list_sessions()
        elif self.path.startswith("/api/v1/sessions/"):
            self._get_session()
        elif self.path == "/api/v1/autoload/plan":
            self._autoload_plan()
        elif self.path == "/api/v1/autoload/downloads":
            self._autoload_downloads()
        elif self.path == "/api/v1/autoload/status":
            self._autoload_status()
        elif self.path == "/api/v1/autoload/benchmark/status":
            self._benchmark_autoload_status()
        elif self.path == "/api/v1/local-agent":
            self._get_local_agent()
        elif self.path == "/api/v1/missions":
            self._list_missions()
        elif self.path.startswith("/api/v1/missions/") and self.path.split("?", 1)[0].endswith("/log"):
            self._get_mission_log()
        elif self.path.startswith("/api/v1/missions/") and self.path.split("?", 1)[0].endswith("/flock"):
            self._get_mission_flock()
        elif self.path.startswith("/api/v1/missions/") and self.path.split("?", 1)[0].endswith("/context"):
            self._get_mission_context()
        elif self.path.startswith("/api/v1/missions/") and self.path.split("?", 1)[0].endswith("/files"):
            self._get_mission_files()
        elif self.path.startswith("/api/v1/missions/") and self.path.split("?", 1)[0].endswith("/download"):
            self._download_mission_dir()
        elif self.path.startswith("/api/v1/missions/") and self.path.split("?", 1)[0].endswith("/result"):
            self._get_mission_result()
        elif self.path.startswith("/api/v1/missions/") and "/file?" in self.path:
            self._get_mission_file_content()
        elif self.path.startswith("/api/v1/missions/"):
            self._get_mission()
        else:
            self._serve_static()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.path == "/api/v1/register":
            self._register()
        elif self.path == "/api/v1/heartbeat":
            self._heartbeat()
        elif self.path == "/api/v1/nodes/push":
            self._push_add()
        elif self.path.startswith("/api/v1/pending/") and self.path.endswith("/approve"):
            self._approve_node()
        elif self.path.startswith("/api/v1/pending/") and self.path.endswith("/reject"):
            self._reject_node()
        elif self.path == "/api/v1/prompt":
            self._prompt()
        elif self.path.startswith("/api/v1/results/"):
            self._post_result()
        elif self.path.startswith("/api/v1/nodes/") and self.path.endswith("/download_and_load"):
            self._node_download_and_load()
        elif self.path.startswith("/api/v1/nodes/") and self.path.endswith("/load"):
            self._node_load()
        elif self.path.startswith("/api/v1/nodes/") and self.path.endswith("/unload"):
            self._node_unload()
        elif self.path.startswith("/api/v1/nodes/") and self.path.endswith("/configure"):
            self._node_configure()
        elif self.path.startswith("/api/v1/nodes/") and self.path.endswith("/restart"):
            self._node_restart()
        elif self.path == "/api/v1/lock":
            self._set_lock()
        elif self.path == "/api/v1/config":
            self._set_config()
        elif self.path == "/api/v1/autoload":
            self._autoload_execute()
        elif self.path == "/api/v1/autoload/benchmark":
            self._benchmark_autoload_execute()
        elif self.path == "/api/v1/local-agent":
            self._start_local_agent()
        elif self.path == "/api/v1/sessions":
            self._create_session()
        elif self.path.startswith("/api/v1/sessions/") and self.path.endswith("/activate"):
            self._activate_session()
        elif self.path.startswith("/api/v1/sessions/") and self.path.endswith("/pause"):
            self._pause_session()
        elif self.path.startswith("/api/v1/sessions/") and self.path.endswith("/resume"):
            self._resume_session()
        elif self.path.startswith("/api/v1/sessions/") and self.path.endswith("/complete"):
            self._complete_session()
        elif self.path == "/api/v1/missions":
            self._start_mission()
        elif self.path.startswith("/api/v1/missions/") and self.path.endswith("/pause"):
            self._pause_mission()
        elif self.path.startswith("/api/v1/missions/") and self.path.endswith("/resume"):
            self._resume_mission()
        elif self.path.startswith("/api/v1/missions/") and self.path.endswith("/stop"):
            self._stop_mission()
        elif self.path.startswith("/api/v1/missions/") and self.path.endswith("/respond"):
            self._respond_to_prompt()
        elif self.path.startswith("/api/v1/missions/") and self.path.endswith("/exec"):
            self._mission_exec()
        elif self.path == "/api/v1/gc":
            self._gc_containers()
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path == "/api/v1/access":
            self._set_access()
        elif self.path.startswith("/api/v1/missions/") and self.path.endswith("/showrunner"):
            self._set_showrunner()
        elif self.path.startswith("/api/v1/sessions/"):
            self._update_session()
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path == "/api/v1/local-agent":
            self._stop_local_agent()
        elif self.path.startswith("/api/v1/missions/"):
            self._delete_mission()
        elif self.path.startswith("/api/v1/sessions/"):
            self._delete_session()
        elif self.path.startswith("/api/v1/nodes/"):
            self._delete_node()
        elif self.path.startswith("/api/v1/tokens/"):
            self._revoke_token()
        else:
            self._json(404, {"error": "not found"})

    # ── Node registration ────────────────────────────────────────────────

    def _register(self):
        body = self._body()
        if body is None:
            return

        node_id = body.get("node_id", "")
        hostname = body.get("hostname", "")
        if not node_id:
            return self._json(400, {"error": "node_id required"})
        if not _IDENT_RE.match(node_id):
            return self._json(400, {"error": "invalid node_id"})
        if hostname and not _IDENT_RE.match(hostname):
            return self._json(400, {"error": "invalid hostname"})

        permitted = is_permitted(node_id, hostname)
        if permitted == "pending":
            enqueue(node_id, hostname, hardware=body.get("hardware"))
            _persist()
            _log(f"pending     {node_id} ({hostname}) — awaiting approval")
            return self._json(202, {"status": "pending", "node_id": node_id})
        if not permitted:
            return self._json(403, {"error": "node not permitted (access policy)"})

        token = gen_token(node_id, label=hostname or node_id)
        node = reg_node(node_id, hostname, hardware=body.get("hardware"), token=token)
        _persist()
        _log(f"registered  {node_id} ({hostname})")
        self._json(200, {"node_id": node_id, "token": token})

    # ── Heartbeat ────────────────────────────────────────────────────────

    def _heartbeat(self):
        node_id = self._require_auth()
        if node_id is None:
            return

        body = self._body()
        if body is None:
            return

        ok = hb_node(
            node_id,
            hostname=body.get("hostname", ""),
            metrics=body.get("metrics"),
            endpoints=body.get("endpoints"),
            hardware=body.get("hardware"),
            agent_version=body.get("agent_version"),
            downloaded=body.get("downloaded"),
            agent_type=body.get("agent_type"),
            cpu_ram_enabled=body.get("cpu_ram_enabled"),
            activity=body.get("activity"),
        )
        # Track autoload progress from endpoints
        orch_mod.autoload_check_heartbeat(node_id, body.get("endpoints"))
        if not ok:
            # Token valid but node unknown (e.g. nCore restarted) — re-register
            hostname = body.get("hostname", "")
            reg_node(node_id, hostname, hardware=body.get("hardware"))
            hb_node(node_id, hostname=hostname,
                    metrics=body.get("metrics"),
                    endpoints=body.get("endpoints"),
                    hardware=body.get("hardware"),
                    agent_version=body.get("agent_version"),
                    downloaded=body.get("downloaded"),
                    agent_type=body.get("agent_type"),
                    cpu_ram_enabled=body.get("cpu_ram_enabled"),
                    activity=body.get("activity"))
            _log(f"auto-readmit {node_id} ({hostname})")

        # Drain any pending orchestrator commands for this node
        commands = orch_mod.drain(node_id)
        self._json(200, {"ok": True, "commands": commands,
                         "heartbeat_interval": push_mod.get_interval()})

    # ── Push-mode agent pairing ─────────────────────────────────────────

    def _push_add(self):
        """Pair with a push-mode agent and register it."""
        body = self._body()
        if body is None:
            return
        address = body.get("address", "")
        if not address:
            return self._json(400, {"error": "address required"})
        if not address.startswith(("http://", "https://")):
            address = "http://" + address

        orch_token = secrets.token_urlsafe(32)
        try:
            pair_body = json.dumps({
                "orchestrator_token": orch_token,
            }).encode()
            req = urllib.request.Request(
                f"{address}/api/v1/pair", data=pair_body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", str(e))
            except Exception:
                err = str(e)
            return self._json(502, {"error": f"pairing failed: {err}"})
        except Exception as e:
            return self._json(502, {"error": f"cannot reach agent: {e}"})

        node_id = data.get("node_id", "")
        hostname = data.get("hostname", "")
        if not node_id:
            return self._json(502, {"error": "agent returned no node_id"})
        if not _IDENT_RE.match(node_id):
            return self._json(502, {"error": "agent returned invalid node_id"})
        if hostname and not _IDENT_RE.match(hostname):
            return self._json(502, {"error": "agent returned invalid hostname"})
        if not is_permitted(node_id, hostname):
            return self._json(403, {"error": "node not permitted (access policy)"})

        reg_node(node_id, hostname, hardware=data.get("hardware"),
                 conn_mode="push", address=address, orchestrator_token=orch_token)
        _persist()
        _log(f"paired push  {node_id} ({hostname}) @ {address}")
        self._json(200, {"node_id": node_id, "hostname": hostname})

    # ── Node list / detail ───────────────────────────────────────────────

    def _health(self):
        nodes = all_nodes()
        counts = {"healthy": 0, "stale": 0, "dead": 0}
        outdated = 0
        for n in nodes:
            counts[n.get("status", "dead")] += 1
            av = n.get("agent_version")
            if av and av != _EXPECTED_AGENT_VER:
                outdated += 1
        la = local_agent_mod.status()
        self._json(200, {
            "status": "ok",
            "nodes_total": len(nodes),
            **counts,
            "outdated": outdated,
            "expected_agent_version": _EXPECTED_AGENT_VER,
            "uptime": time.time() - _start_time,
            "local_agent": la["agent_type"] if la["running"] else None,
        })

    def _list_nodes(self):
        nodes = all_nodes()
        for n in nodes:
            n.pop("token", None)
            n.pop("orchestrator_token", None)
            av = n.get("agent_version")
            n["version_ok"] = (av == _EXPECTED_AGENT_VER) if av else None
        self._json(200, {"nodes": nodes, "expected_agent_version": _EXPECTED_AGENT_VER})

    def _get_node(self):
        nid = self.path.rsplit("/", 1)[-1]
        node = get_node(nid)
        if not node:
            return self._json(404, {"error": "node not found"})
        node.pop("token", None)
        node.pop("orchestrator_token", None)
        self._json(200, node)

    def _delete_node(self):
        nid = self.path.rsplit("/", 1)[-1]
        node = get_node(nid)
        if rm_node(nid):
            revoke_for_node(nid)
            _persist()
            # Notify push-mode agent to unpair
            if node and node.get("conn_mode") == "push":
                addr = node.get("address", "")
                tok = node.get("orchestrator_token", "")
                if addr and tok:
                    try:
                        req = urllib.request.Request(
                            f"{addr}/api/v1/unpair", data=b"{}",
                            headers={"Content-Type": "application/json",
                                     "Authorization": f"Bearer {tok}"},
                            method="POST",
                        )
                        urllib.request.urlopen(req, timeout=3)
                    except Exception:
                        pass  # best-effort
            _log(f"removed     {nid}")
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "node not found"})

    # ── Access control ───────────────────────────────────────────────────

    def _set_access(self):
        body = self._body()
        if body is None:
            return

        if "mode" in body:
            try:
                set_mode(body["mode"])
            except ValueError as e:
                return self._json(400, {"error": str(e)})

        for ident in body.get("allow", []):
            allow(ident)
        for ident in body.get("deny", []):
            deny(ident)
        for ident in body.get("remove", []):
            access_remove(ident)

        _persist()
        self._json(200, access_status())

    # ── Token management ─────────────────────────────────────────────────

    def _revoke_token(self):
        nid = self.path.rsplit("/", 1)[-1]
        count = revoke_for_node(nid)
        _persist()
        self._json(200, {"revoked": count})

    # ── Pending approval ─────────────────────────────────────────────────

    def _approve_node(self):
        # /api/v1/pending/<node_id>/approve
        parts = self.path.split("/")
        nid = parts[4] if len(parts) >= 6 else ""
        if not nid or not _IDENT_RE.match(nid):
            return self._json(400, {"error": "invalid node_id"})
        approve_node(nid)
        _persist()
        _log(f"approved    {nid}")
        self._json(200, {"ok": True, "node_id": nid})

    def _reject_node(self):
        # /api/v1/pending/<node_id>/reject
        parts = self.path.split("/")
        nid = parts[4] if len(parts) >= 6 else ""
        if not nid or not _IDENT_RE.match(nid):
            return self._json(400, {"error": "invalid node_id"})
        if reject_node(nid):
            _persist()
            _log(f"rejected    {nid}")
            self._json(200, {"ok": True, "node_id": nid})
        else:
            self._json(404, {"error": "node not in pending queue"})

    # ── Helpers ──────────────────────────────────────────────────────────

    def _require_auth(self):
        """Extract and verify Bearer token. Returns node_id or None (sends 401)."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._json(401, {"error": "missing bearer token"})
            return None
        token = auth[7:]
        node_id = verify_token(token)
        if node_id is None:
            self._json(401, {"error": "invalid token"})
            return None
        return node_id

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "invalid JSON"})
            return None

    def _json(self, code, data):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ── Orchestrator endpoints ────────────────────────────────────────────

    def _prompt(self):
        body = self._body()
        if body is None:
            return
        text = body.get("prompt", "").strip()
        if not text:
            return self._json(400, {"error": "prompt text required"})
        task_id, expected = orch_mod.broadcast_prompt(text)
        _log(f"prompt      task={task_id} endpoints={expected}")
        self._json(200, {"task_id": task_id, "expected": expected})

    def _get_task(self):
        tid = self.path.rsplit("/", 1)[-1]
        task = orch_mod.get_task(tid)
        if task is None:
            return self._json(404, {"error": "task not found"})
        self._json(200, task)

    def _post_result(self):
        """Agent posts prompt result back: POST /api/v1/results/<task_id>"""
        tid = self.path.rsplit("/", 1)[-1]
        node_id = self._require_auth()
        if node_id is None:
            return
        body = self._body()
        if body is None:
            return
        # Look up hostname for display
        node = get_node(node_id)
        hostname = node.get("hostname", "") if node else ""
        ok = orch_mod.record_result(tid, body, node_id=node_id, hostname=hostname)
        if not ok:
            return self._json(404, {"error": "task not found"})
        self._json(200, {"ok": True})

    # ── Catalog & model management ────────────────────────────────────

    def _get_catalog(self):
        self._json(200, {"models": catalog_mod.get_catalog()})

    def _node_load(self):
        """POST /api/v1/nodes/:id/load — load a model on a node."""
        parts = self.path.split("/")
        # /api/v1/nodes/<id>/load  → parts = ['', 'api', 'v1', 'nodes', '<id>', 'load']
        nid = parts[4] if len(parts) >= 6 else ""
        if not nid or not _IDENT_RE.match(nid):
            return self._json(400, {"error": "invalid node_id"})
        body = self._body()
        if body is None:
            return
        model_id = body.get("model_id", "")
        if not model_id:
            return self._json(400, {"error": "model_id required"})
        node = get_node(nid)
        if not node:
            return self._json(404, {"error": "node not found"})
        cmd = {"action": "load", "model_id": model_id}
        gpu_idx = body.get("gpu_idx")
        if gpu_idx is not None:
            cmd["gpu_idx"] = gpu_idx
        ctx = body.get("context_length")
        if ctx is not None:
            cmd["context_length"] = ctx
        address = node.get("address")
        token = node.get("orchestrator_token")
        if not address or not token:
            orch_mod.enqueue(nid, cmd)
            _log(f"load        {nid} ← {model_id} (queued for pull-mode)")
            return self._json(200, {"ok": True, "queued": True,
                                     "node_id": nid, "model_id": model_id})
        try:
            data = json.dumps(cmd).encode()
            req = urllib.request.Request(
                f"{address}/api/v1/command",
                data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            _log(f"load        {nid} ← {model_id}")
            self._json(200, {"ok": True, "node_id": nid, "model_id": model_id})
        except Exception as e:
            _log(f"load        {nid} ← {model_id} FAIL {e}")
            self._json(502, {"error": str(e)})

    def _node_unload(self):
        """POST /api/v1/nodes/:id/unload — unload a model on a node."""
        parts = self.path.split("/")
        nid = parts[4] if len(parts) >= 6 else ""
        if not nid or not _IDENT_RE.match(nid):
            return self._json(400, {"error": "invalid node_id"})
        body = self._body()
        if body is None:
            return
        model_id = body.get("model_id", "")
        if not model_id:
            return self._json(400, {"error": "model_id required"})
        node = get_node(nid)
        if not node:
            return self._json(404, {"error": "node not found"})
        cmd = {"action": "unload", "model_id": model_id}
        address = node.get("address")
        token = node.get("orchestrator_token")
        if not address or not token:
            orch_mod.enqueue(nid, cmd)
            _log(f"unload      {nid} ← {model_id} (queued for pull-mode)")
            return self._json(200, {"ok": True, "queued": True,
                                     "node_id": nid, "model_id": model_id})
        try:
            data = json.dumps(cmd).encode()
            req = urllib.request.Request(
                f"{address}/api/v1/command",
                data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            _log(f"unload      {nid} ← {model_id}")
            self._json(200, {"ok": True, "node_id": nid, "model_id": model_id})
        except Exception as e:
            _log(f"unload      {nid} ← {model_id} FAIL {e}")
            self._json(502, {"error": str(e)})
        self._json(200, {"ok": True, "node_id": nid, "model_id": model_id})

    def _node_configure(self):
        """POST /api/v1/nodes/:id/configure — send configuration to agent.

        LINUX-ONLY: used to toggle CPU/RAM device from the nCore UI.
        The agent persists the setting and adjusts its device pool.
        """
        parts = self.path.split("/")
        nid = parts[4] if len(parts) >= 6 else ""
        if not nid or not _IDENT_RE.match(nid):
            return self._json(400, {"error": "invalid node_id"})
        body = self._body()
        if body is None:
            return
        node = get_node(nid)
        if not node:
            return self._json(404, {"error": "node not found"})

        cmd = {"action": "configure"}
        if "cpu_ram_enabled" in body:
            cmd["cpu_ram_enabled"] = bool(body["cpu_ram_enabled"])

        address = node.get("address")
        token = node.get("orchestrator_token")
        if not address or not token:
            orch_mod.enqueue(nid, cmd)
            _log(f"configure   {nid} (queued for pull-mode)")
            return self._json(200, {"ok": True, "queued": True,
                                     "node_id": nid})
        try:
            data = json.dumps(cmd).encode()
            req = urllib.request.Request(
                f"{address}/api/v1/command",
                data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            _log(f"configure   {nid}")
            self._json(200, {"ok": True, "node_id": nid})
        except Exception as e:
            _log(f"configure   {nid} FAIL {e}")
            self._json(502, {"error": str(e)})

    def _node_restart(self):
        """POST /api/v1/nodes/:id/restart — restart agent (requires watchdog)."""
        parts = self.path.split("/")
        nid = parts[4] if len(parts) >= 6 else ""
        if not nid or not _IDENT_RE.match(nid):
            return self._json(400, {"error": "invalid node_id"})
        node = get_node(nid)
        if not node:
            return self._json(404, {"error": "node not found"})
        cmd = {"action": "restart_agent"}
        address = node.get("address")
        token = node.get("orchestrator_token")
        if not address or not token:
            orch_mod.enqueue(nid, cmd)
            _log(f"restart     {nid} (queued for pull-mode)")
            return self._json(200, {"ok": True, "queued": True, "node_id": nid})
        try:
            data = json.dumps(cmd).encode()
            req = urllib.request.Request(
                f"{address}/api/v1/command",
                data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            _log(f"restart     {nid}")
            self._json(200, {"ok": True, "node_id": nid})
        except Exception as e:
            _log(f"restart     {nid} FAIL {e}")
            self._json(502, {"error": str(e)})

    def _node_download_and_load(self):
        """POST /api/v1/nodes/:id/download_and_load — direct proxy to agent."""
        parts = self.path.split("/")
        nid = parts[4] if len(parts) >= 6 else ""
        if not nid or not _IDENT_RE.match(nid):
            return self._json(400, {"error": "invalid node_id"})
        body = self._body()
        if body is None:
            return
        model_id = body.get("model_id", "")
        if not model_id:
            return self._json(400, {"error": "model_id required"})
        node = get_node(nid)
        if not node:
            return self._json(404, {"error": "node not found"})
        cmd = {"action": "download_and_load", "model_id": model_id}
        gpu_idx = body.get("gpu_idx")
        if gpu_idx is not None:
            cmd["gpu_idx"] = gpu_idx
        ctx = body.get("context_length")
        if ctx is not None:
            cmd["context_length"] = ctx
        address = node.get("address")
        token = node.get("orchestrator_token")
        if not address or not token:
            # Pull-mode: enqueue command for agent to pick up via heartbeat
            orch_mod.enqueue(nid, cmd)
            _log(f"dl+load     {nid} ← {model_id} (queued for pull-mode)")
            return self._json(200, {"ok": True, "queued": True,
                                     "node_id": nid, "model_id": model_id})
        try:
            data = json.dumps(cmd).encode()
            req = urllib.request.Request(
                f"{address}/api/v1/command",
                data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            _log(f"dl+load     {nid} ← {model_id} job={result.get('job_id','')}")
            self._json(200, result)
        except Exception as e:
            _log(f"dl+load     {nid} ← {model_id} FAIL {e}")
            self._json(502, {"error": str(e)})

    def _node_job_status(self):
        """GET /api/v1/nodes/:id/jobs/:job_id — proxy to agent."""
        parts = self.path.split("/")
        # /api/v1/nodes/<nid>/jobs/<job_id>
        if len(parts) < 7:
            return self._json(400, {"error": "invalid path"})
        nid = parts[4]
        job_id = parts[6]
        node = get_node(nid)
        if not node:
            return self._json(404, {"error": "node not found"})
        address = node.get("address")
        token = node.get("orchestrator_token")
        if not address or not token:
            return self._json(400, {"error": "node not reachable"})
        try:
            req = urllib.request.Request(
                f"{address}/api/v1/jobs/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            self._json(200, result)
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            self._json(e.code, {"error": body_text or str(e)})
        except Exception as e:
            self._json(502, {"error": str(e)})
    # ── Local agent management ─────────────────────────────────────────────

    def _get_local_agent(self):
        """GET /api/v1/local-agent — status + available agents."""
        self._json(200, local_agent_mod.status())

    def _start_local_agent(self):
        """POST /api/v1/local-agent — start the local agent."""
        body = self._body()
        if body is None:
            return
        agent_type = body.get("agent_type", "agent")
        ok, err = local_agent_mod.start_agent(agent_type, ncore_port=_ncore_port)
        if not ok:
            return self._json(409, {"error": err})
        _persist()
        _log("local-agent▶ started")
        self._json(200, local_agent_mod.status())

    def _stop_local_agent(self):
        """DELETE /api/v1/local-agent — stop the running local agent."""
        ok, err = local_agent_mod.stop_agent()
        if not ok:
            return self._json(409, {"error": err})
        _persist()
        _log("local-agent■ stopped")
        self._json(200, local_agent_mod.status())

    def _set_lock(self):
        """POST /api/v1/lock — toggle or set model lock."""
        body = self._body()
        if body is None:
            return
        if "locked" in body:
            orch_mod.set_locked(body["locked"])
        else:
            orch_mod.set_locked(not orch_mod.is_locked())
        _persist()
        _log(f"lock        {'ON' if orch_mod.is_locked() else 'OFF'}")
        self._json(200, {"locked": orch_mod.is_locked()})

    def _set_config(self):
        """POST /api/v1/config — update cluster config (tight_pack, locked, heartbeat_interval)."""
        body = self._body()
        if body is None:
            return
        if "locked" in body:
            orch_mod.set_locked(body["locked"])
        if "tight_pack" in body:
            orch_mod.set_tight_pack(body["tight_pack"])
        if "heartbeat_interval" in body:
            push_mod.set_interval(body["heartbeat_interval"])
            import registry
            hb = push_mod.get_interval()
            registry.STALE_AFTER = hb * 4
            registry.DEAD_AFTER = hb * 12
        _persist()
        self._json(200, {"locked": orch_mod.is_locked(), "tight_pack": orch_mod.is_tight_pack(),
                         "heartbeat_interval": push_mod.get_interval()})

    def _autoload_plan(self):
        """GET /api/v1/autoload/plan — preview autoload steps (clean_slate matches execution)."""
        plan = orch_mod.plan_autoload(clean_slate=True)
        self._json(200, {"steps": plan, "count": len(plan), "tight_pack": orch_mod.is_tight_pack()})

    def _autoload_downloads(self):
        """GET /api/v1/autoload/downloads — deduplicated list of all downloaded models."""
        models = orch_mod.all_downloads()
        self._json(200, {"models": models, "count": len(models)})

    def _autoload_execute(self):
        """POST /api/v1/autoload — plan and enqueue all autoload steps."""
        body = self._body() or {}
        priorities = body.get("priorities") if isinstance(body, dict) else None
        plan, count = orch_mod.execute_autoload(priorities=priorities)
        self._json(200, {"steps": plan, "count": count, "tight_pack": orch_mod.is_tight_pack()})

    def _autoload_status(self):
        """GET /api/v1/autoload/status — poll autoload progress."""
        state = orch_mod.autoload_status()
        if state is None:
            self._json(200, {"status": "idle", "steps": []})
        else:
            self._json(200, state)

    def _benchmark_autoload_execute(self):
        """POST /api/v1/autoload/benchmark — start benchmark autoload."""
        body = self._body() or {}
        target_tps = body.get("target_tps", 50)
        if not isinstance(target_tps, (int, float)) or target_tps <= 0:
            target_tps = 50
        result = orch_mod.execute_benchmark_autoload(target_tps=target_tps)
        status_code = 200 if "ok" in result else 409
        self._json(status_code, result)

    def _benchmark_autoload_status(self):
        """GET /api/v1/autoload/benchmark/status — poll benchmark autoload progress."""
        state = orch_mod.benchmark_autoload_status()
        if state is None:
            self._json(200, {"status": "idle", "devices": [], "log": []})
        else:
            self._json(200, state)

    # ── Mission endpoints ─────────────────────────────────────────────────

    def _list_missions(self):
        """GET /api/v1/missions"""
        self._json(200, {"missions": mission_mod.list_missions()})

    def _get_mission(self):
        """GET /api/v1/missions/:id"""
        mid = self.path.rsplit("/", 1)[-1]
        m = mission_mod.get_mission(mid)
        if not m:
            return self._json(404, {"error": "mission not found"})
        self._json(200, m)

    def _start_mission(self):
        """POST /api/v1/missions — start a mission."""
        body = self._body()
        if body is None:
            return
        mission_id = body.get("mission_id", "").strip()
        mission_text = body.get("mission_text", "").strip()
        if not mission_id:
            return self._json(400, {"error": "mission_id required"})
        if not mission_text:
            return self._json(400, {"error": "mission_text required"})

        # Also create/update the session
        session_mod.create(mission_id)
        session_mod.set_mission_text(mission_id, mission_text)
        session_mod.activate(mission_id)

        sr_override = body.get("showrunner_override")  # {node_id, model} or None
        m, err = mission_mod.start_mission(mission_id, mission_text, showrunner_override=sr_override)
        if err:
            return self._json(409, {"error": err})
        _log(f"mission▶    {mission_id}")
        self._json(200, m)

    def _pause_mission(self):
        """POST /api/v1/missions/:id/pause"""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        m, err = mission_mod.pause_mission(mid)
        if err:
            return self._json(404 if "not found" in err else 409, {"error": err})
        _log(f"mission⏸    {mid}")
        self._json(200, m)

    def _resume_mission(self):
        """POST /api/v1/missions/:id/resume"""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        m, err = mission_mod.resume_mission(mid)
        if err:
            return self._json(404 if "not found" in err else 409, {"error": err})
        _log(f"mission▶    {mid}")
        self._json(200, m)

    def _stop_mission(self):
        """POST /api/v1/missions/:id/stop"""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        m, err = mission_mod.stop_mission(mid)
        if err:
            return self._json(404 if "not found" in err else 409, {"error": err})
        _log(f"mission■    {mid}")
        self._json(200, m)

    def _set_showrunner(self):
        """PUT /api/v1/missions/:id/showrunner — set or clear showrunner override."""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        body = self._body()
        if body is None:
            return
        node_id = body.get("node_id")
        model = body.get("model")
        m, err = mission_mod.set_showrunner_override(mid, node_id=node_id, model=model)
        if err:
            code = 404 if "not found" in err else 409
            return self._json(code, {"error": err})
        _log(f"mission☆    {mid} showrunner={'auto' if not node_id else model}")
        self._json(200, m)

    def _delete_mission(self):
        """DELETE /api/v1/missions/:id"""
        mid = self.path.rsplit("/", 1)[-1]
        if mission_mod.delete_mission(mid):
            _log(f"mission✗    {mid}")
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "mission not found"})

    def _gc_containers(self):
        """POST /api/v1/gc — Run container garbage collection."""
        result = mission_mod.gc_containers()
        _log(f"gc          removed={len(result['removed'])} kept={len(result['kept'])}")
        self._json(200, result)
    def _get_mission_log(self):
        """GET /api/v1/missions/:id/log"""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        # Parse query params from URL
        query = {}
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
            for p in qs.split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    query[k] = urllib.parse.unquote(v)
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 100))
        level = query.get("level")
        agent = query.get("agent")
        log = mission_mod.get_mission_log(mid, offset=offset, limit=limit, level=level, agent=agent)
        if log is None:
            return self._json(404, {"error": "mission not found"})
        self._json(200, log)

    def _get_mission_flock(self):
        """GET /api/v1/missions/:id/flock"""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        flock = mission_mod.get_mission_flock(mid)
        if flock is None:
            return self._json(404, {"error": "mission not found"})
        self._json(200, flock)

    def _get_mission_context(self):
        """GET /api/v1/missions/:id/context — debug: view Showrunner prompt."""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        ctx = mission_mod.get_showrunner_context(mid)
        if ctx is None:
            return self._json(404, {"error": "mission not found"})
        self._json(200, {"context": ctx})

    def _get_mission_files(self):
        """GET /api/v1/missions/:id/files — list container files."""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        query = {}
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
            for p in qs.split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    query[k] = urllib.parse.unquote(v)
        path = query.get("path", "/home/mission")
        files = mission_mod.get_container_files(mid, path)
        if files is None:
            return self._json(404, {"error": "mission not found or no container"})
        self._json(200, {"files": files, "path": path})

    def _get_mission_file_content(self):
        """GET /api/v1/missions/:id/file?path=..."""
        parts_path = self.path.split("/")
        mid = parts_path[4] if len(parts_path) >= 6 else ""
        query = {}
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
            for p in qs.split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    query[k] = urllib.parse.unquote(v)
        fpath = query.get("path", "")
        if not fpath:
            return self._json(400, {"error": "path required"})
        content = mission_mod.get_container_file(mid, fpath)
        if content is None:
            return self._json(404, {"error": "file not found"})
        self._json(200, {"path": fpath, "content": content})

    def _download_mission_dir(self):
        """GET /api/v1/missions/:id/download?path=... — zip and download a container directory."""
        import subprocess as _sp
        parts_path = self.path.split("/")
        mid = parts_path[4] if len(parts_path) >= 6 else ""
        query = {}
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
            for p in qs.split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    query[k] = urllib.parse.unquote(v)
        dir_path = query.get("path", "/home/mission")
        # Validate path — must be under /home/mission
        if not dir_path.startswith("/home/mission"):
            return self._json(400, {"error": "path must be under /home/mission"})

        container_id = mission_mod.get_container_id(mid)
        if not container_id:
            return self._json(404, {"error": "mission not found or no container"})

        # Create tar.gz inside container, then docker cp it out.
        # Archive goes to /var/tmp (overlay FS, visible to docker cp).
        # NOT /tmp (tmpfs, invisible to docker cp) and NOT /home/mission
        # (causes "file changed as we read it" when archiving itself).
        import shlex
        arc_name = "/var/tmp/.cf_download.tar.gz"
        parent = os.path.dirname(dir_path) or "/"
        leaf = os.path.basename(dir_path) or "mission"
        tar_cmd = f"tar czf {arc_name} -C {shlex.quote(parent)} {shlex.quote(leaf)}"
        _, err, rc = mission_mod._container_exec(container_id, tar_cmd, timeout=120)
        if rc != 0:
            return self._json(500, {"error": f"tar failed (rc={rc}): {err}"})

        try:
            # docker cp to stdout wraps the file in an outer tar layer
            proc = _sp.Popen(
                ["docker", "cp", f"{container_id}:{arc_name}", "-"],
                stdout=_sp.PIPE, stderr=_sp.PIPE
            )
            data = proc.stdout.read()
            proc.wait()
            if not data:
                return self._json(500, {"error": "docker cp returned empty data"})

            # Extract the inner .tar.gz from the outer tar wrapper
            import tarfile, io
            try:
                outer = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
                inner_member = None
                for m in outer.getmembers():
                    if m.name.endswith(".tar.gz"):
                        inner_member = m
                        break
                if inner_member:
                    zip_bytes = outer.extractfile(inner_member).read()
                else:
                    zip_bytes = data  # fallback: send raw
                outer.close()
            except Exception as e:
                return self._json(500, {"error": f"tar extraction failed: {e}"})

            if not zip_bytes:
                return self._json(500, {"error": "archive is empty"})

            filename = f"{leaf}.tar.gz"
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)
        finally:
            mission_mod._container_exec(container_id, f"rm -f {arc_name}", timeout=5)

    def _get_mission_result(self):
        """GET /api/v1/missions/:id/result — serve result.html as raw HTML."""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        content = mission_mod.get_container_file(mid, "/home/mission/result.html")
        if content is None:
            return self._json(404, {"error": "no result page"})
        html_bytes = content.encode("utf-8", errors="replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def _respond_to_prompt(self):
        """POST /api/v1/missions/:id/respond — user answers a Showrunner prompt."""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        body = self._body()
        if body is None:
            return
        prompt_id = body.get("prompt_id", "")
        response_text = body.get("response", "")
        if not prompt_id:
            return self._json(400, {"error": "prompt_id required"})
        m, err = mission_mod.respond_to_prompt(mid, prompt_id, response_text)
        if err:
            return self._json(404, {"error": err})
        self._json(200, m)

    def _mission_exec(self):
        """POST /api/v1/missions/:id/exec — execute command in container."""
        parts = self.path.split("/")
        mid = parts[4] if len(parts) >= 6 else ""
        body = self._body()
        if body is None:
            return
        command = body.get("command", "").strip()
        if not command:
            return self._json(400, {"error": "command required"})
        result, err = mission_mod.exec_in_container(mid, command)
        if err:
            return self._json(404, {"error": err})
        self._json(200, result)

    # ── Session endpoints ─────────────────────────────────────────────────

    def _create_session(self):
        """POST /api/v1/sessions — create (or reclaim) a session."""
        body = self._body()
        if body is None:
            return
        session_id = body.get("id", "").strip() or None
        session, err = session_mod.create(session_id)
        if err:
            return self._json(409, {"error": err})
        _persist()
        _log(f"session+    {session['id']}")
        self._json(200, session)

    def _list_sessions(self):
        """GET /api/v1/sessions"""
        sessions = session_mod.list_all()
        self._json(200, {
            "sessions": sessions,
            "max_concurrent": session_mod.get_max_concurrent(),
        })

    def _get_session(self):
        """GET /api/v1/sessions/:id"""
        sid = self.path.rsplit("/", 1)[-1]
        session = session_mod.get(sid)
        if not session:
            return self._json(404, {"error": "session not found"})
        self._json(200, session)

    def _update_session(self):
        """PUT /api/v1/sessions/:id — update mission text."""
        sid = self.path.rsplit("/", 1)[-1]
        body = self._body()
        if body is None:
            return
        text = body.get("mission_text")
        if text is None:
            return self._json(400, {"error": "mission_text required"})
        session, err = session_mod.set_mission_text(sid, text)
        if err:
            return self._json(404, {"error": err})
        _persist()
        _log(f"session~    {sid} v{session['mission_version']}")
        self._json(200, session)

    def _delete_session(self):
        """DELETE /api/v1/sessions/:id"""
        sid = self.path.rsplit("/", 1)[-1]
        if session_mod.delete(sid):
            _persist()
            _log(f"session-    {sid}")
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "session not found"})

    def _activate_session(self):
        """POST /api/v1/sessions/:id/activate"""
        parts = self.path.split("/")
        sid = parts[4] if len(parts) >= 6 else ""
        session, err = session_mod.activate(sid)
        if err:
            code = 404 if "not found" in err else 409
            return self._json(code, {"error": err})
        _persist()
        _log(f"session▶    {sid}")
        self._json(200, session)

    def _pause_session(self):
        """POST /api/v1/sessions/:id/pause"""
        parts = self.path.split("/")
        sid = parts[4] if len(parts) >= 6 else ""
        session, err = session_mod.pause(sid)
        if err:
            code = 404 if "not found" in err else 409
            return self._json(code, {"error": err})
        _persist()
        _log(f"session⏸    {sid}")
        self._json(200, session)

    def _resume_session(self):
        """POST /api/v1/sessions/:id/resume"""
        parts = self.path.split("/")
        sid = parts[4] if len(parts) >= 6 else ""
        session, err = session_mod.resume(sid)
        if err:
            code = 404 if "not found" in err else 409
            return self._json(code, {"error": err})
        _persist()
        _log(f"session▶    {sid}")
        self._json(200, session)

    def _complete_session(self):
        """POST /api/v1/sessions/:id/complete"""
        parts = self.path.split("/")
        sid = parts[4] if len(parts) >= 6 else ""
        session, err = session_mod.complete(sid)
        if err:
            code = 404 if "not found" in err else 409
            return self._json(code, {"error": err})
        _persist()
        _log(f"session✓    {sid}")
        self._json(200, session)

    # ── Static file serving ───────────────────────────────────────────────

    def _serve_static(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/mission.html"

        safe = os.path.normpath(path.lstrip("/"))
        if safe.startswith("..") or os.path.isabs(safe):
            return self._send_error(403)

        fp = _WEB_ROOT / safe
        if not fp.is_file():
            return self._send_error(404)
        if fp.suffix == '.part':
            return self._send_error(403)

        try:
            fp.resolve().relative_to(_WEB_ROOT.resolve())
        except ValueError:
            return self._send_error(403)

        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        data = fp.read_bytes()
        # Inject current APP_VERSION into HTML so CSS/JS refs get cache-busted
        if fp.suffix == ".html":
            data = _VER_RE.sub(f"?v={_APP_VER}".encode(), data)
            # Process SSI-style includes: <!--#include file="footer.part"-->
            def _replace_include(m):
                inc_name = m.group(1).decode()
                inc_path = fp.parent / inc_name
                try:
                    inc_path.resolve().relative_to(_WEB_ROOT.resolve())
                    return inc_path.read_bytes()
                except (ValueError, FileNotFoundError):
                    return b'<!-- include not found -->'
            data = _INCLUDE_RE.sub(_replace_include, data)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, code):
        body = f"<h1>{code}</h1>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Persistence helper ───────────────────────────────────────────────────

def _persist():
    la_status = local_agent_mod.status()
    local_agent_data = la_status["agent_type"] if la_status["running"] else None
    try:
        import oapi as oapi_mod
        oapi_cfg = oapi_mod.get_oapi_config()
    except Exception:
        oapi_cfg = None
    state.save(auth_mod.dump(), access_mod.dump(), push_configs(),
               locked=orch_mod.is_locked(), tight_pack=orch_mod.is_tight_pack(),
               session_data=session_mod.dump(), local_agent=local_agent_data,
               oapi_config=oapi_cfg, heartbeat_interval=push_mod.get_interval())


# ── Module-level ─────────────────────────────────────────────────────────

_start_time = time.time()
_ncore_port = 1903


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def serve(host="0.0.0.0", port=8080):
    global _start_time, _ncore_port
    _start_time = time.time()
    _ncore_port = port

    # Restore persisted state
    auth_data, access_data, push_data, locked, tight_pack, session_data, local_agent_type, oapi_config, hb_interval = state.load()
    if auth_data:
        auth_mod.load(auth_data)
    if access_data:
        access_mod.load(access_data)
    if push_data:
        restore_push(push_data)
    if session_data:
        session_mod.load(session_data)
    orch_mod.set_locked(locked)
    orch_mod.set_tight_pack(tight_pack)

    # Fetch model catalog from HuggingFace + builtins (non-blocking thread)
    import threading
    threading.Thread(target=catalog_mod.refresh, daemon=True).start()

    push_mod.start(interval=hb_interval)

    def _on_reap(nid, hostname):
        revoke_for_node(nid)
        _persist()
        _log(f"reaped      {nid[:12]} ({hostname}) — dead too long")

    start_reaper(on_remove=_on_reap)

    # Start OAPI server on background thread (port 1919)
    import oapi as oapi_mod
    if oapi_config:
        oapi_mod.load_oapi_config(oapi_config)
    oapi_mod.start_background(host=host, port=1919)

    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    _log(f"nCore listening on {host}:{port}")
    _log(f"access mode: {access_mod.mode()}")
    _log(f"tokens loaded: {len(auth_mod.list_tokens())}")
    _log(f"push nodes: {len(push_data or {})}")
    _log(f"model lock: {'ON' if locked else 'OFF'}")

    # Auto-start persisted local agent (if any)
    if local_agent_type:
        def _deferred_start():
            time.sleep(1)  # let server start listening first
            ok, err = local_agent_mod.start_agent(local_agent_type, ncore_port=port)
            if ok:
                _log(f"local-agent▶ {local_agent_type} (auto-start)")
            else:
                _log(f"local-agent✗ {local_agent_type}: {err}")
        import threading as _th
        _th.Thread(target=_deferred_start, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
        _persist()
        httpd.server_close()


if __name__ == "__main__":
    serve(port=1903)
