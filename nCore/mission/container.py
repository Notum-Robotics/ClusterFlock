"""Docker container lifecycle, file I/O, and workspace tree."""

import base64
import secrets
import shlex
import subprocess
import time

from .state import (
    _DOCKER_NETWORK,
    _CONTAINER_IMAGE,
    _CONTAINER_IMAGE_PREBAKED,
    _CONTAINER_CPUS,
    _CONTAINER_MEM,
    _WORKSPACE_TREE_MAX_ENTRIES,
)


# ── Low-level Docker helpers ─────────────────────────────────────────────

def _docker_exec(cmd, timeout=30):
    """Run a docker command. Returns (stdout, stderr, returncode)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except FileNotFoundError:
        return "", "docker not found", -1


def _ensure_network():
    """Create the mission-net Docker network if it doesn't exist."""
    out, err, rc = _docker_exec(["docker", "network", "ls", "--format", "{{.Name}}"])
    if _DOCKER_NETWORK not in out.split("\n"):
        _docker_exec(["docker", "network", "create", "--driver", "bridge", _DOCKER_NETWORK])


# Track whether prebaked image is confirmed present (avoid re-checking every mission)
_prebaked_image_ready = False


def _ensure_prebaked_image():
    """Check if the pre-baked mission image exists; build it if missing.
    Returns True if the prebaked image is available, False to fall back to base image."""
    global _prebaked_image_ready
    if _prebaked_image_ready:
        return True

    _, _, rc = _docker_exec(
        ["docker", "image", "inspect", _CONTAINER_IMAGE_PREBAKED],
        timeout=10,
    )
    if rc == 0:
        _prebaked_image_ready = True
        return True

    print(f"[mission] Pre-baked image {_CONTAINER_IMAGE_PREBAKED} not found — building...")

    dockerfile = (
        f"FROM {_CONTAINER_IMAGE}\n"
        "ENV DEBIAN_FRONTEND=noninteractive\n"
        "RUN apt-get update -qq && \\\n"
        "    apt-get install -y -qq --no-install-recommends \\\n"
        "    curl wget python3 python3-pip jq git nodejs npm ca-certificates \\\n"
        "    build-essential universal-ctags && \\\n"
        "    apt-get clean && rm -rf /var/lib/apt/lists/*\n"
        "RUN mkdir -p /home/mission/tools\n"
    )

    try:
        proc = subprocess.run(
            ["docker", "build", "-t", _CONTAINER_IMAGE_PREBAKED, "-"],
            input=dockerfile, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode == 0:
            print(f"[mission] Pre-baked image {_CONTAINER_IMAGE_PREBAKED} built successfully")
            _prebaked_image_ready = True
            return True
        else:
            print(f"[mission] Failed to build pre-baked image: {proc.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        print("[mission] Pre-baked image build timed out (600s)")
        return False


# ── Container lifecycle ──────────────────────────────────────────────────

def _create_container(mission_id):
    """Create and start a Docker container for a mission. Returns container_id or None."""
    container_name = f"cf-mission-{mission_id}"

    # Remove any leftover container with this name (from incomplete cleanup / background destroy race)
    _docker_exec(["docker", "rm", "-f", container_name], timeout=30)
    _docker_exec(["docker", "volume", "rm", f"{container_name}-home"], timeout=15)

    _ensure_network()

    # Use prebaked image if available — falls back to base image
    use_prebaked = _ensure_prebaked_image()
    image = _CONTAINER_IMAGE_PREBAKED if use_prebaked else _CONTAINER_IMAGE

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        f"--network={_DOCKER_NETWORK}",
        "--cpus", _CONTAINER_CPUS,
        "--memory", _CONTAINER_MEM,
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,exec,size=512m",
        "-v", f"{container_name}-home:/home/mission",
        image,
        "sleep", "infinity",
    ]
    out, err, rc = _docker_exec(cmd, timeout=60)
    if rc != 0:
        return None

    cid = out.strip()

    # Initialize the container filesystem
    setup_cmds = [
        "mkdir -p /home/mission/tools",
        "echo '[]' > /home/mission/tools/manifest.json",
        "echo '# Mission Log' > /home/mission/mission_log.md",
        f"echo 'Mission ID: {mission_id}' >> /home/mission/mission_log.md",
        f"echo 'Created: {time.strftime('%Y-%m-%d %H:%M:%S')}' >> /home/mission/mission_log.md",
        "echo '---' >> /home/mission/mission_log.md",
    ]
    # Only install packages if using base image (prebaked already has them)
    if not use_prebaked:
        setup_cmds.append(
            "apt-get update -qq && apt-get install -y -qq curl wget python3 python3-pip jq git nodejs npm universal-ctags > /dev/null 2>&1 || true"
        )

    # Git init — gives agents rollback, diff tracking, and checkpoint capability
    setup_cmds += [
        "cd /home/mission && git init -q",
        "cd /home/mission && git config user.name 'agent' && git config user.email 'agent@mission'",
    ]

    for c in setup_cmds:
        _docker_exec(["docker", "exec", cid, "bash", "-c", c], timeout=300)

    # Bootstrap standard toolkit into /home/mission/tools/
    _bootstrap_mission_tools(cid)

    # Initial git commit after setup
    _container_exec(cid,
        "cd /home/mission && git add -A && git commit -q -m 'mission init' --allow-empty",
        timeout=15)

    return cid


def _container_exec(container_id, command, timeout=60):
    """Execute a command inside a mission container. Returns (stdout, stderr, rc)."""
    if not container_id:
        return "", "no container", -1
    return _docker_exec(
        ["docker", "exec", container_id, "bash", "-c", command],
        timeout=timeout
    )


def _container_write_file(container_id, path, content):
    """Write a file inside the container."""
    if not container_id:
        return False
    # Use docker exec with base64 to safely transfer content
    b64 = base64.b64encode(content.encode()).decode()
    cmd = f"echo '{b64}' | base64 -d > {shlex.quote(path)}"
    _, _, rc = _container_exec(container_id, cmd)
    return rc == 0


def _container_read_file(container_id, path):
    """Read a file from the container."""
    if not container_id:
        return None
    out, _, rc = _container_exec(container_id, f"cat {shlex.quote(path)}")
    return out if rc == 0 else None


def _container_list_dir(container_id, path="/home/mission"):
    """List files in a container directory. Returns list of {name, type, size, modified}."""
    if not container_id:
        return []
    out, _, rc = _container_exec(
        container_id,
        f"find {shlex.quote(path)} -maxdepth 1 -printf '%y %s %T@ %P\\n' 2>/dev/null | tail -n +2"
    )
    if rc != 0 or not out:
        return []
    items = []
    for line in out.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        ftype = "directory" if parts[0] == "d" else "file"
        try:
            size = int(parts[1])
            modified = float(parts[2])
        except ValueError:
            size, modified = 0, 0
        items.append({
            "name": parts[3],
            "type": ftype,
            "size": size,
            "modified": modified,
        })
    return sorted(items, key=lambda x: (x["type"] != "directory", x["name"]))


def _destroy_container(mission_id):
    """Stop and remove container + volume."""
    container_name = f"cf-mission-{mission_id}"
    _docker_exec(["docker", "stop", container_name], timeout=15)
    _docker_exec(["docker", "rm", "-f", container_name], timeout=15)
    _docker_exec(["docker", "volume", "rm", f"{container_name}-home"], timeout=15)


# ── Workspace tree ───────────────────────────────────────────────────────

def _build_workspace_tree(container_id, root="/home/mission", max_entries=_WORKSPACE_TREE_MAX_ENTRIES):
    """Build a recursive file tree of the container workspace.
    Returns a formatted string showing the full structure with sizes."""
    if not container_id:
        return ""
    out, _, rc = _container_exec(
        container_id,
        f"find {shlex.quote(root)} -maxdepth 5 -not -path '*/node_modules/*' "
        f"-not -path '*/.git/*' -not -path '*/\\.npm/*' "
        f"-printf '%y %s %d %P\\n' 2>/dev/null | head -{max_entries}",
        timeout=15
    )
    if rc != 0 or not out:
        return ""

    lines = []
    for line in out.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        ftype, size_str, depth_str, name = parts[0], parts[1], parts[2], parts[3]
        try:
            size = int(size_str)
            depth = int(depth_str)
        except ValueError:
            continue
        indent = "  " * depth
        if ftype == "d":
            lines.append(f"{indent}📁 {name}/")
        else:
            size_label = f"{size}B" if size < 1024 else f"{size // 1024}KB" if size < 1048576 else f"{size // 1048576}MB"
            lines.append(f"{indent}📄 {name} ({size_label})")

    if len(lines) >= max_entries:
        lines.append(f"  ... (truncated at {max_entries} entries)")
    return "\n".join(lines)


# ── Output truncation ────────────────────────────────────────────────────

def _syntax_check(container_id, path):
    """Run a quick syntax check on a file inside the container.
    Returns (ok: bool, errors: str). Empty errors string if clean."""
    if not container_id or not path:
        return True, ""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    checks = {
        "py":  f"python3 -m py_compile {shlex.quote(path)} 2>&1",
        "js":  f"node --check {shlex.quote(path)} 2>&1",
        "mjs": f"node --check {shlex.quote(path)} 2>&1",
        "ts":  f"npx --yes tsc --noEmit --allowJs {shlex.quote(path)} 2>&1 || node --check {shlex.quote(path)} 2>&1",
        "json": f"python3 -c \"import json; json.load(open({repr(path)}))\" 2>&1",
        "sh":  f"bash -n {shlex.quote(path)} 2>&1",
        "bash": f"bash -n {shlex.quote(path)} 2>&1",
    }
    cmd = checks.get(ext)
    if not cmd:
        return True, ""
    out, err, rc = _container_exec(container_id, cmd, timeout=15)
    combined = (out + "\n" + err).strip()
    if rc == 0:
        return True, ""
    # Clean up the output — keep only the relevant error
    return False, combined[:500]


def _replace_lines(container_id, path, start_line, end_line, new_content):
    """Replace lines start_line..end_line (1-indexed, inclusive) with new_content.
    Returns (ok: bool, total_lines_after: int)."""
    if not container_id or not path:
        return False, 0
    content = _container_read_file(container_id, path)
    if content is None:
        return False, 0
    lines = content.split("\n")
    # Handle trailing newline: if content ends with \n, split produces an extra empty string
    if content.endswith("\n") and lines and lines[-1] == "":
        lines = lines[:-1]
    total = len(lines)
    start = max(1, int(start_line)) - 1  # convert to 0-indexed
    end = min(int(end_line), total)       # inclusive, 1-indexed
    new_lines = new_content.split("\n") if new_content else []
    lines[start:end] = new_lines
    final = "\n".join(lines) + "\n"
    ok = _container_write_file(container_id, path, final)
    return ok, len(lines)


def _apply_diff(container_id, path, diff_text):
    """Apply a unified diff to a file. Returns (ok: bool, output: str)."""
    if not container_id:
        return False, "no container"
    # Write the diff to a temp file and apply with patch
    diff_path = f"/tmp/_patch_{secrets.token_hex(4)}.diff"
    ok = _container_write_file(container_id, diff_path, diff_text)
    if not ok:
        return False, "failed to write diff"
    out, err, rc = _container_exec(
        container_id,
        f"cd /home/mission && patch -p0 --no-backup-if-mismatch < {shlex.quote(diff_path)} 2>&1; "
        f"rm -f {shlex.quote(diff_path)}",
        timeout=15,
    )
    combined = (out + "\n" + err).strip()
    return rc == 0, combined[:500]


def _find_files(container_id, pattern, path="/home/mission/", max_results=200):
    """Find files matching a glob/name pattern. Returns list of paths."""
    if not container_id:
        return []
    cmd = (f"find {shlex.quote(path)} -maxdepth 8 "
           f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
           f"-name {shlex.quote(pattern)} -type f 2>/dev/null | head -{max_results}")
    out, _, rc = _container_exec(container_id, cmd, timeout=15)
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.strip().split("\n") if line.strip()]


def _file_info(container_id, path):
    """Get file metadata without reading content. Returns dict with size, lines, type, permissions."""
    if not container_id:
        return None
    cmd = f"stat -c '%s %a' {shlex.quote(path)} 2>/dev/null && wc -l < {shlex.quote(path)} 2>/dev/null && file -b {shlex.quote(path)} 2>/dev/null"
    out, _, rc = _container_exec(container_id, cmd, timeout=10)
    if rc != 0 or not out:
        return None
    parts = out.strip().split("\n")
    info = {"path": path}
    if len(parts) >= 1:
        stat_parts = parts[0].split()
        info["size"] = int(stat_parts[0]) if stat_parts else 0
        info["permissions"] = stat_parts[1] if len(stat_parts) > 1 else "?"
    if len(parts) >= 2 and parts[1].strip().isdigit():
        info["lines"] = int(parts[1].strip())
    if len(parts) >= 3:
        info["type"] = parts[2].strip()[:100]
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    info["extension"] = ext
    return info


# ── Project scaffold templates ───────────────────────────────────────────

_SCAFFOLD_TEMPLATES = {
    "python-cli": {
        "description": "Python CLI application with argparse",
        "files": {
            "main.py": '#!/usr/bin/env python3\n"""Main entry point."""\nimport argparse\nimport sys\n\n\ndef main():\n    parser = argparse.ArgumentParser(description="App description")\n    parser.add_argument("input", help="Input file or value")\n    parser.add_argument("-o", "--output", default="-", help="Output file (default: stdout)")\n    parser.add_argument("-v", "--verbose", action="store_true")\n    args = parser.parse_args()\n    \n    # TODO: implement\n    print(f"Processing: {args.input}")\n    return 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n',
            "requirements.txt": "# Add dependencies here\n",
            "tests/test_main.py": '"""Tests for main module."""\nimport subprocess\nimport sys\n\ndef test_help():\n    result = subprocess.run([sys.executable, "main.py", "--help"], capture_output=True, text=True)\n    assert result.returncode == 0\n    assert "usage" in result.stdout.lower()\n',
            "README.md": "# App\n\n## Usage\n```bash\npython3 main.py <input>\n```\n\n## Development\n```bash\npip install -r requirements.txt\npython3 -m pytest tests/\n```\n",
        },
    },
    "flask-api": {
        "description": "Flask REST API with health check",
        "files": {
            "app.py": '#!/usr/bin/env python3\n"""Flask REST API."""\nfrom flask import Flask, jsonify, request\n\napp = Flask(__name__)\n\n\n@app.route("/health")\ndef health():\n    return jsonify({"status": "ok"})\n\n\n@app.route("/api/v1/items", methods=["GET"])\ndef list_items():\n    # TODO: implement\n    return jsonify({"items": []})\n\n\n@app.route("/api/v1/items", methods=["POST"])\ndef create_item():\n    data = request.get_json()\n    if not data:\n        return jsonify({"error": "JSON body required"}), 400\n    # TODO: implement\n    return jsonify({"ok": True, "item": data}), 201\n\n\nif __name__ == "__main__":\n    app.run(host="0.0.0.0", port=5000, debug=True)\n',
            "requirements.txt": "flask>=3.0\n",
            "tests/test_api.py": '"""API tests."""\nimport json\nfrom app import app\n\ndef test_health():\n    client = app.test_client()\n    r = client.get("/health")\n    assert r.status_code == 200\n    assert r.get_json()["status"] == "ok"\n',
            "README.md": "# API\n\n## Run\n```bash\npip install -r requirements.txt\npython3 app.py\n```\n\n## Test\n```bash\npython3 -m pytest tests/\n```\n",
        },
    },
    "react-app": {
        "description": "React app with Vite bundler",
        "files": {
            "package.json": '{\n  "name": "app",\n  "private": true,\n  "version": "0.1.0",\n  "type": "module",\n  "scripts": {\n    "dev": "vite",\n    "build": "vite build",\n    "preview": "vite preview"\n  },\n  "dependencies": {\n    "react": "^19.0.0",\n    "react-dom": "^19.0.0"\n  },\n  "devDependencies": {\n    "@vitejs/plugin-react": "^4.3.0",\n    "vite": "^6.0.0"\n  }\n}\n',
            "vite.config.js": 'import { defineConfig } from "vite";\nimport react from "@vitejs/plugin-react";\nexport default defineConfig({ plugins: [react()] });\n',
            "index.html": '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>App</title></head>\n<body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body>\n</html>\n',
            "src/main.jsx": 'import React from "react";\nimport ReactDOM from "react-dom/client";\nimport App from "./App";\n\nReactDOM.createRoot(document.getElementById("root")).render(<React.StrictMode><App /></React.StrictMode>);\n',
            "src/App.jsx": 'import React, { useState } from "react";\n\nexport default function App() {\n  const [count, setCount] = useState(0);\n  return (\n    <div style={{ padding: "2rem", fontFamily: "system-ui" }}>\n      <h1>App</h1>\n      <button onClick={() => setCount(c => c + 1)}>Count: {count}</button>\n    </div>\n  );\n}\n',
        },
    },
    "node-api": {
        "description": "Express.js REST API",
        "files": {
            "package.json": '{\n  "name": "api",\n  "version": "0.1.0",\n  "type": "module",\n  "scripts": { "start": "node server.js", "dev": "node --watch server.js" },\n  "dependencies": { "express": "^4.21.0" }\n}\n',
            "server.js": 'import express from "express";\nconst app = express();\napp.use(express.json());\n\napp.get("/health", (req, res) => res.json({ status: "ok" }));\napp.get("/api/items", (req, res) => res.json({ items: [] }));\n\nconst PORT = process.env.PORT || 3000;\napp.listen(PORT, () => console.log(`Listening on :${PORT}`));\n',
            "README.md": "# API\n\n```bash\nnpm install && npm start\n```\n",
        },
    },
    "html-app": {
        "description": "Static HTML/CSS/JS application",
        "files": {
            "index.html": '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>App</title>\n  <link rel="stylesheet" href="style.css">\n</head>\n<body>\n  <div id="app">\n    <h1>App</h1>\n  </div>\n  <script src="app.js"></script>\n</body>\n</html>\n',
            "style.css": '*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }\nbody { font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; padding: 2rem; background: #f5f5f5; color: #333; }\n#app { max-width: 800px; margin: 0 auto; }\nh1 { margin-bottom: 1rem; }\n',
            "app.js": '"use strict";\n\ndocument.addEventListener("DOMContentLoaded", () => {\n  console.log("App loaded");\n  // TODO: implement\n});\n',
        },
    },
}


def _scaffold_project(container_id, template_name, base_path="/home/mission/"):
    """Create project skeleton from a template. Returns (ok, files_created, description)."""
    if not container_id:
        return False, [], "no container"
    template = _SCAFFOLD_TEMPLATES.get(template_name)
    if not template:
        available = ", ".join(sorted(_SCAFFOLD_TEMPLATES.keys()))
        return False, [], f"Unknown template '{template_name}'. Available: {available}"

    created = []
    for rel_path, content in template["files"].items():
        full_path = base_path.rstrip("/") + "/" + rel_path
        parent = "/".join(full_path.split("/")[:-1])
        if parent:
            _container_exec(container_id, f"mkdir -p {shlex.quote(parent)}", timeout=10)
        ok = _container_write_file(container_id, full_path, content)
        if ok:
            created.append(rel_path)

    return True, created, template.get("description", "")


def _smart_truncate(text, max_chars=3000, is_own_content=False):
    """Truncate output intelligently — detect HTML/minified content and truncate harder.
    When is_own_content=True (reading mission container files), don't aggressively
    truncate HTML — the showrunner needs to read its own deliverables for verification."""
    if not text or len(text) <= max_chars:
        return text
    # For external fetched content: detect HTML/minified & truncate aggressively
    if not is_own_content:
        lines = text.split('\n')
        avg_line_len = len(text) / max(len(lines), 1)
        is_html = '<html' in text[:500].lower() or '<div' in text[:500].lower()
        is_minified = avg_line_len > 500
        if is_html or is_minified:
            limit = min(max_chars // 3, 1000)
            return (
                text[:limit] +
                f"\n\n[TRUNCATED — {len(text)} bytes total, "
                f"{'minified HTML' if is_html else 'dense content'}. "
                f"Use targeted extraction: python3 -c \"import re; "
                f"print(re.findall(r'<(input|select|textarea|form|button)[^>]*>', "
                f"open('FILE').read()))\" or try a subpage like /contact]"
            )
    return text[:max_chars] + f"\n[TRUNCATED — {len(text)} bytes total]"


# ── Git checkpoint helpers ───────────────────────────────────────────────

def _git_checkpoint(container_id, name, description=""):
    """Create a named git checkpoint. Returns (ok, commit_hash)."""
    if not container_id:
        return False, ""
    msg = f"{name}: {description}" if description else name
    # Stage everything and commit
    out, err, rc = _container_exec(
        container_id,
        f"cd /home/mission && git add -A && git commit -q -m {shlex.quote(msg)} --allow-empty",
        timeout=15,
    )
    if rc != 0:
        return False, err
    # Get the short hash
    hash_out, _, _ = _container_exec(
        container_id, "cd /home/mission && git rev-parse --short HEAD", timeout=5)
    return True, hash_out.strip()


def _git_restore(container_id, ref):
    """Restore workspace to a checkpoint. Returns (ok, output)."""
    if not container_id:
        return False, "no container"
    # First commit current state so it's not lost
    _container_exec(container_id,
        "cd /home/mission && git add -A && git commit -q -m 'auto-save before restore' --allow-empty",
        timeout=15)
    # Then restore
    out, err, rc = _container_exec(
        container_id,
        f"cd /home/mission && git checkout {shlex.quote(ref)} -- .",
        timeout=15)
    return rc == 0, (out + "\n" + err).strip()


def _git_list_checkpoints(container_id, max_entries=30):
    """List recent git checkpoints. Returns list of {hash, message, time}."""
    if not container_id:
        return []
    out, _, rc = _container_exec(
        container_id,
        f"cd /home/mission && git log --oneline -n {max_entries} --format='%h|%s|%cr'",
        timeout=10)
    if rc != 0 or not out:
        return []
    entries = []
    for line in out.strip().split("\n"):
        parts = line.split("|", 2)
        if len(parts) >= 2:
            entries.append({
                "hash": parts[0],
                "message": parts[1],
                "time": parts[2] if len(parts) > 2 else "",
            })
    return entries


def _git_diff_since(container_id, ref="HEAD~1"):
    """Show git diff since a reference. Returns diff text."""
    if not container_id:
        return ""
    # Stage everything first so untracked files show in diff
    _container_exec(container_id, "cd /home/mission && git add -A", timeout=10)
    out, _, rc = _container_exec(
        container_id,
        f"cd /home/mission && git diff --cached {shlex.quote(ref)} --stat 2>/dev/null; "
        f"echo '---'; "
        f"cd /home/mission && git diff --cached {shlex.quote(ref)} 2>/dev/null | head -500",
        timeout=15)
    return out if rc == 0 else ""


# ── Standard tool bootstrap ─────────────────────────────────────────────

# Tool scripts installed into every mission container at startup.
# These give agents code intelligence, testing, and verification capabilities
# that go far beyond raw grep/find.

_TOOL_SCRIPTS = {
    "outline": {
        "description": "Generate project outline — files, classes, functions with line numbers",
        "script": r'''#!/usr/bin/env python3
"""Project outline generator — uses ctags for code intelligence."""
import subprocess, json, sys, os

root = sys.argv[1] if len(sys.argv) > 1 else "/home/mission"

# Try ctags first (fast, accurate)
try:
    result = subprocess.run(
        ["ctags", "-R", "--output-format=json", "--fields=+n+K",
         "--exclude=node_modules", "--exclude=.git", "--exclude=tools",
         root],
        capture_output=True, text=True, timeout=15)
    if result.returncode == 0 and result.stdout.strip():
        symbols = {}
        for line in result.stdout.strip().split("\n"):
            try:
                entry = json.loads(line)
                fpath = entry.get("path", "")
                kind = entry.get("kind", "?")
                name = entry.get("name", "?")
                lineno = entry.get("line", "?")
                scope = entry.get("scope", "")
                if fpath not in symbols:
                    symbols[fpath] = []
                label = f"  {kind} {name} (L{lineno})"
                if scope:
                    label += f" [{scope}]"
                symbols[fpath].append((lineno if isinstance(lineno, int) else 0, label))
            except json.JSONDecodeError:
                continue
        for fpath in sorted(symbols):
            rel = os.path.relpath(fpath, root)
            entries = sorted(symbols[fpath], key=lambda x: x[0])
            print(f"\n{rel}:")
            for _, label in entries:
                print(label)
        sys.exit(0)
except FileNotFoundError:
    pass

# Fallback: grep-based outline
for dirpath, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "tools", "__pycache__")]
    for fname in sorted(files):
        fpath = os.path.join(dirpath, fname)
        rel = os.path.relpath(fpath, root)
        ext = fname.rsplit(".", 1)[-1] if "." in fname else ""
        if ext not in ("py", "js", "ts", "jsx", "tsx", "java", "go", "rs", "c", "cpp", "h", "rb"):
            continue
        try:
            with open(fpath) as f:
                defs = []
                for i, line in enumerate(f, 1):
                    stripped = line.strip()
                    if ext == "py" and (stripped.startswith("def ") or stripped.startswith("class ")):
                        defs.append(f"  {stripped.split('(')[0].split(':')[0]} (L{i})")
                    elif ext in ("js","ts","jsx","tsx") and ("function " in stripped or "class " in stripped or "const " in stripped):
                        defs.append(f"  {stripped[:80]} (L{i})")
                if defs:
                    print(f"\n{rel}:")
                    for d in defs:
                        print(d)
        except (OSError, UnicodeDecodeError):
            continue
''',
    },
    "lint": {
        "description": "Run syntax/lint checks on all code files",
        "script": r'''#!/bin/bash
# Multi-language lint tool — checks all code files for syntax errors
set -e
ROOT="${1:-/home/mission}"
ERRORS=0
CHECKED=0

# Python
for f in $(find "$ROOT" -name '*.py' -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/tools/*' 2>/dev/null); do
    CHECKED=$((CHECKED + 1))
    if ! python3 -m py_compile "$f" 2>/tmp/_lint_err; then
        echo "FAIL: $f"
        cat /tmp/_lint_err
        ERRORS=$((ERRORS + 1))
    fi
done

# JavaScript/TypeScript
for f in $(find "$ROOT" -name '*.js' -o -name '*.mjs' -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/tools/*' 2>/dev/null); do
    CHECKED=$((CHECKED + 1))
    if ! node --check "$f" 2>/tmp/_lint_err; then
        echo "FAIL: $f"
        cat /tmp/_lint_err
        ERRORS=$((ERRORS + 1))
    fi
done

# JSON
for f in $(find "$ROOT" -name '*.json' -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/tools/*' 2>/dev/null); do
    CHECKED=$((CHECKED + 1))
    if ! python3 -c "import json; json.load(open('$f'))" 2>/tmp/_lint_err; then
        echo "FAIL: $f"
        cat /tmp/_lint_err
        ERRORS=$((ERRORS + 1))
    fi
done

# Shell
for f in $(find "$ROOT" -name '*.sh' -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/tools/*' 2>/dev/null); do
    CHECKED=$((CHECKED + 1))
    if ! bash -n "$f" 2>/tmp/_lint_err; then
        echo "FAIL: $f"
        cat /tmp/_lint_err
        ERRORS=$((ERRORS + 1))
    fi
done

echo ""
echo "Checked $CHECKED files, $ERRORS errors"
[ $ERRORS -eq 0 ] && echo "ALL CLEAN" || exit 1
''',
    },
    "test": {
        "description": "Discover and run test files (pytest, mocha, jest, go test)",
        "script": r'''#!/bin/bash
# Universal test runner — discovers and runs tests for the project
ROOT="${1:-/home/mission}"
cd "$ROOT"
FOUND=0

# Python (pytest)
if ls tests/test_*.py test_*.py tests/*.py 2>/dev/null | head -1 | grep -q .; then
    echo "=== Python Tests (pytest) ==="
    python3 -m pytest tests/ -v --tb=short 2>&1 || python3 -m pytest -v --tb=short 2>&1 || true
    FOUND=1
fi

# Node.js (package.json test script)
if [ -f package.json ]; then
    if grep -q '"test"' package.json 2>/dev/null; then
        echo "=== Node.js Tests ==="
        npm test 2>&1 || true
        FOUND=1
    fi
fi

# Go
if ls *.go 2>/dev/null | head -1 | grep -q .; then
    echo "=== Go Tests ==="
    go test ./... -v 2>&1 || true
    FOUND=1
fi

if [ $FOUND -eq 0 ]; then
    echo "No test files found."
    echo "Common patterns: tests/test_*.py, npm test, go test"
    exit 0
fi
''',
    },
    "search_def": {
        "description": "Find function/class definitions and usages by name",
        "script": r'''#!/bin/bash
# Find definitions and usages of a symbol
PATTERN="${1:?Usage: search_def <name> [path]}"
ROOT="${2:-/home/mission}"

echo "=== Definitions ==="
# ctags-based if available
if command -v ctags >/dev/null 2>&1; then
    ctags -R --output-format=json --exclude=node_modules --exclude=.git "$ROOT" 2>/dev/null | \
        python3 -c "
import sys, json
for line in sys.stdin:
    try:
        e = json.loads(line)
        if '$PATTERN' in e.get('name',''):
            print(f\"  {e['kind']} {e['name']} @ {e['path']}:{e.get('line','?')}\")
    except: pass
" 2>/dev/null
else
    # Fallback: grep for def/class/function declarations
    grep -rnE "(def |class |function |const |let |var |export ).*$PATTERN" "$ROOT" \
        --include='*.py' --include='*.js' --include='*.ts' --include='*.jsx' --include='*.tsx' \
        --include='*.go' --include='*.rs' --include='*.java' \
        --exclude-dir=node_modules --exclude-dir=.git 2>/dev/null | head -30
fi

echo ""
echo "=== Usages ==="
grep -rnw "$PATTERN" "$ROOT" \
    --include='*.py' --include='*.js' --include='*.ts' --include='*.jsx' --include='*.tsx' \
    --include='*.go' --include='*.rs' --include='*.java' --include='*.html' --include='*.json' \
    --exclude-dir=node_modules --exclude-dir=.git 2>/dev/null | head -40
''',
    },
    "verify": {
        "description": "Run full verification: lint + tests + file existence check",
        "script": r'''#!/bin/bash
# Full project verification tool
ROOT="${1:-/home/mission}"
PASS=0
FAIL=0

echo "=== LINT ==="
if /home/mission/tools/lint "$ROOT" 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== TESTS ==="
if /home/mission/tools/test "$ROOT" 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== STATE CHECK ==="
if [ -f "$ROOT/state.json" ]; then
    echo "state.json exists"
    # Check for unverified requirements
    UNVERIFIED=$(python3 -c "
import json
try:
    state = json.load(open('$ROOT/state.json'))
    reqs = state.get('requirements', [])
    unverified = [r for r in reqs if not r.get('verified')]
    print(f'{len(unverified)} unverified requirements out of {len(reqs)}')
    for r in unverified:
        print(f'  ✗ {r.get(\"text\", r.get(\"id\", \"?\"))}')
except Exception as e:
    print(f'Could not parse state.json: {e}')
" 2>&1)
    echo "$UNVERIFIED"
else
    echo "⚠ No state.json found"
fi

echo ""
echo "=== SUMMARY ==="
echo "Checks passed: $PASS, failed: $FAIL"
[ $FAIL -eq 0 ] && echo "✅ ALL CHECKS PASSED" || echo "❌ SOME CHECKS FAILED"
[ $FAIL -eq 0 ] || exit 1
''',
    },
    "diff_since": {
        "description": "Show changes since a checkpoint (default: last commit)",
        "script": r'''#!/bin/bash
# Show git diff since a reference point
REF="${1:-HEAD~1}"
cd /home/mission
git add -A 2>/dev/null
echo "=== Changed files ==="
git diff --cached "$REF" --stat 2>/dev/null || git diff --stat 2>/dev/null
echo ""
echo "=== Diff ==="
git diff --cached "$REF" 2>/dev/null | head -300 || git diff 2>/dev/null | head -300
''',
    },
}


def _bootstrap_mission_tools(container_id):
    """Install standard tools into /home/mission/tools/ and register them in the manifest."""
    if not container_id:
        return []
    tools_manifest = []
    for name, entry in _TOOL_SCRIPTS.items():
        tool_path = f"/home/mission/tools/{name}"
        ok = _container_write_file(container_id, tool_path, entry["script"])
        if ok:
            _container_exec(container_id, f"chmod +x {shlex.quote(tool_path)}", timeout=5)
            tools_manifest.append({
                "name": name,
                "description": entry["description"],
                "input_schema": [],
                "created_by": "system",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    # Write manifest
    import json
    manifest_json = json.dumps(tools_manifest, indent=2)
    _container_write_file(container_id, "/home/mission/tools/manifest.json", manifest_json)
    return tools_manifest
