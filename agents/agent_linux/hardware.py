"""Hardware profiling for Linux agent.

Generic Linux (amd64 + CUDA) — 0 to N NVIDIA GPUs.
Zero external dependencies — uses nvidia-smi + /proc.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import time

_cache = {}
_TTL = 5


def _cached(key, fn):
    now = time.time()
    if key in _cache and now - _cache[key][1] < _TTL:
        return _cache[key][0]
    v = fn()
    _cache[key] = (v, now)
    return v


# ── GPU ──────────────────────────────────────────────────────────────────────

def gpu():
    return _cached("gpu", _gpu_probe)


def _find_nvidia_smi():
    """Locate nvidia-smi, checking PATH and common system locations."""
    p = shutil.which("nvidia-smi")
    if p:
        return p
    for d in ("/usr/sbin", "/usr/local/sbin", "/usr/lib/nvidia/bin",
              "/usr/bin", "/usr/local/bin"):
        c = os.path.join(d, "nvidia-smi")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _gpu_probe():
    _nvsmi = _find_nvidia_smi()
    if not _nvsmi:
        return []
    try:
        out = subprocess.check_output(
            [_nvsmi,
             "--query-gpu=name,memory.total,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        )
        gpus = []
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 4:
                name = p[0]
                mem_total = p[1]
                mem_free = p[2]
                util = p[3]
                # Unified memory GPUs report [N/A] for memory
                if mem_total == "[N/A]" or mem_free == "[N/A]":
                    total, free = _mem_info()
                    gpus.append({
                        "name": name, "unified": True,
                        "vram_total_mb": total,
                        "vram_free_mb": free,
                        "utilization_pct": int(util) if util != "[N/A]" else 0,
                    })
                else:
                    gpus.append({
                        "name": name,
                        "vram_total_mb": int(mem_total),
                        "vram_free_mb": int(mem_free),
                        "utilization_pct": int(util),
                    })
        return gpus
    except Exception:
        return []


def _mem_info():
    """(total_mb, available_mb) via OS reads."""
    if platform.system() == "Linux":
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total, avail
    return 0, 0


# ── System ───────────────────────────────────────────────────────────────────

def _cpu_pct():
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        vals = [int(x) for x in line.split()[1:]]
        idle = vals[3]
        total = sum(vals)
        time.sleep(0.1)
        with open("/proc/stat") as f:
            line = f.readline()
        vals2 = [int(x) for x in line.split()[1:]]
        idle2 = vals2[3]
        total2 = sum(vals2)
        d_idle = idle2 - idle
        d_total = total2 - total
        if d_total == 0:
            return 0
        return round((1 - d_idle / d_total) * 100, 1)
    except Exception:
        return 0


def _disk_free_gb():
    try:
        st = os.statvfs("/")
        return round(st.f_bavail * st.f_frsize / (1024**3), 1)
    except Exception:
        return 0


def system():
    return _cached("system", _system_probe)


def _system_probe():
    total, avail = _mem_info()
    return {
        "hostname": platform.node(),
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count() or 0,
        "ram_total_mb": total,
        "ram_free_mb": avail,
        "disk_free_gb": _disk_free_gb(),
    }


# ── Live Metrics ────────────────────────────────────────────────────────────

def live_metrics():
    """Return current hardware metrics for heartbeat payload."""
    return {
        "gpu": gpu(),
        "system": {
            "cpu_pct": _cpu_pct(),
            "ram_free_mb": _mem_info()[1],
        },
    }


def profile():
    """Full one-time hardware profile."""
    return {
        "gpu": gpu(),
        "system": system(),
    }


def snapshot():
    """Alias for profile(), used during setup for VRAM inspection."""
    return profile()


