"""GPU cleanup: detect and shut down LM Studio and Ollama on macOS, freeing memory.

Ensures unified memory is fully available before llama.cpp takes over.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.request


# ── LM Studio ───────────────────────────────────────────────────────────────

def _lms_path():
    """Locate the lms CLI binary."""
    p = shutil.which("lms")
    if p:
        return p
    for candidate in [
        os.path.expanduser("~/.lmstudio/bin/lms"),
        os.path.expanduser("~/.cache/lm-studio/bin/lms"),
        "/Applications/LM Studio.app/Contents/Resources/bin/lms",
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _lms_run(args, timeout=30):
    """Run an lms CLI command, return stdout."""
    lms = _lms_path()
    if not lms:
        return None
    try:
        r = subprocess.run(
            [lms] + args,
            timeout=timeout, capture_output=True, text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _lms_ps():
    """List loaded models in LM Studio via `lms ps --json`."""
    out = _lms_run(["ps", "--json"])
    if not out:
        return []
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []


def _lms_unload_all():
    """Unload all models from LM Studio."""
    models = _lms_ps()
    for m in models:
        mid = m.get("identifier") or m.get("modelKey") or m.get("id")
        if mid:
            _lms_run(["unload", mid], timeout=30)
    return len(models)


def _kill_lmstudio():
    """Terminate LM Studio server and GUI processes on macOS."""
    _lms_run(["server", "stop"], timeout=15)
    time.sleep(1)

    killed = 0
    for proc_name in ["LM Studio", "lm-studio"]:
        try:
            r = subprocess.run(
                ["pkill", "-f", proc_name],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                killed += 1
        except Exception:
            pass
    return killed


def cleanup_lmstudio():
    """Full LM Studio cleanup."""
    lms = _lms_path()
    if not lms:
        return {"installed": False, "skipped": True}

    result = {"installed": True}
    models = _lms_ps()
    result["models_found"] = len(models)
    if models:
        names = [m.get("identifier") or m.get("modelKey") or "?" for m in models]
        print(f"[cleanup] LM Studio: {len(models)} model(s) loaded: {', '.join(names)}")
        count = _lms_unload_all()
        print(f"[cleanup] Unloaded {count} model(s)")
        result["models_unloaded"] = count
    else:
        print("[cleanup] LM Studio: no models loaded")
        result["models_unloaded"] = 0

    killed = _kill_lmstudio()
    result["processes_killed"] = killed
    if killed:
        print(f"[cleanup] LM Studio processes terminated")
    else:
        print("[cleanup] LM Studio: no running processes found")

    return result


# ── Ollama ──────────────────────────────────────────────────────────────────

def _ollama_path():
    """Locate the ollama binary."""
    return shutil.which("ollama")


def _ollama_ps():
    """List running models in Ollama."""
    ollama = _ollama_path()
    if not ollama:
        return []
    try:
        r = subprocess.run(
            [ollama, "ps"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return []
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2:
            return []
        models = []
        for line in lines[1:]:
            parts = line.split()
            if parts:
                models.append({"name": parts[0]})
        return models
    except Exception:
        return []


def _ollama_unload_all():
    """Unload all models from Ollama."""
    models = _ollama_ps()
    for m in models:
        name = m.get("name", "")
        if not name:
            continue
        try:
            data = json.dumps({"model": name, "keep_alive": 0}).encode()
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass
    return len(models)


def _kill_ollama():
    """Terminate Ollama on macOS."""
    killed = 0
    # Try launchctl (macOS standard)
    try:
        subprocess.run(
            ["launchctl", "stop", "com.ollama.ollama"],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass

    for proc_name in ["Ollama", "ollama serve", "ollama"]:
        try:
            r = subprocess.run(
                ["pkill", "-f", proc_name],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                killed += 1
        except Exception:
            pass
    return killed


def cleanup_ollama():
    """Full Ollama cleanup."""
    ollama = _ollama_path()
    if not ollama:
        return {"installed": False, "skipped": True}

    result = {"installed": True}
    models = _ollama_ps()
    result["models_found"] = len(models)
    if models:
        names = [m.get("name", "?") for m in models]
        print(f"[cleanup] Ollama: {len(models)} model(s) running: {', '.join(names)}")
        count = _ollama_unload_all()
        print(f"[cleanup] Unloaded {count} model(s)")
        result["models_unloaded"] = count
    else:
        print("[cleanup] Ollama: no models running")
        result["models_unloaded"] = 0

    killed = _kill_ollama()
    result["processes_killed"] = killed
    if killed:
        print(f"[cleanup] Ollama processes terminated")
    else:
        print("[cleanup] Ollama: no running processes found")

    return result


# ── Public ──────────────────────────────────────────────────────────────────

def cleanup_gpu():
    """Clean up ALL competing inference servers (LM Studio + Ollama).

    Call this before starting llama.cpp to ensure memory is fully available.
    Returns summary dict.
    """
    print("[cleanup] Checking for competing inference servers...")
    summary = {
        "lmstudio": cleanup_lmstudio(),
        "ollama": cleanup_ollama(),
    }

    time.sleep(2)
    print("[cleanup] Cleanup complete")
    return summary
