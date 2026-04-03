"""Docker container lifecycle, file I/O, and workspace tree."""

import base64
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
        "    build-essential && \\\n"
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
            "apt-get update -qq && apt-get install -y -qq curl wget python3 python3-pip jq git nodejs npm > /dev/null 2>&1 || true"
        )
    for c in setup_cmds:
        _docker_exec(["docker", "exec", cid, "bash", "-c", c], timeout=300)

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
