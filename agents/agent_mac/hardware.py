"""Hardware profiling for Apple Silicon Macs.

Optimized for M-series chips with unified memory.
Zero external dependencies — uses sysctl + system_profiler.
"""

import json
import os
import platform
import re
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


# ── GPU (Apple Silicon = unified memory) ─────────────────────────────────────

def gpu():
    return _cached("gpu", _gpu_probe)


def _sysctl(key):
    """Read a sysctl value."""
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", key], timeout=5, text=True, stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except Exception:
        return ""


def _gpu_probe():
    """Detect Apple Silicon GPU via system_profiler."""
    total_mb, free_mb = _mem_info()

    # Get GPU/chip name from system_profiler
    gpu_name = _apple_gpu_name()
    if not gpu_name:
        gpu_name = _sysctl("machdep.cpu.brand_string") or "Apple Silicon"

    return [{
        "name": gpu_name,
        "unified": True,
        "vram_total_mb": total_mb,
        "vram_free_mb": free_mb,
        "utilization_pct": 0,  # macOS doesn't expose GPU util easily
    }]


def _apple_gpu_name():
    """Get the Apple GPU name from system_profiler SPDisplaysDataType."""
    try:
        out = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            timeout=10, text=True, stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
        displays = data.get("SPDisplaysDataType", [])
        for d in displays:
            name = d.get("sppci_model", "")
            if name:
                return name
    except Exception:
        pass
    return ""


def _mem_info():
    """(total_mb, available_mb) for macOS."""
    # Total memory via sysctl
    total_bytes = _sysctl("hw.memsize")
    total_mb = int(total_bytes) // (1024 * 1024) if total_bytes else 0

    # Available memory via vm_stat
    free_mb = _vm_stat_free_mb()
    if free_mb == 0:
        # Fallback: rough estimate
        free_mb = total_mb // 2

    return total_mb, free_mb


def _vm_stat_free_mb():
    """Parse vm_stat to get approximate free memory in MB."""
    try:
        out = subprocess.check_output(
            ["vm_stat"], timeout=5, text=True, stderr=subprocess.DEVNULL,
        )
        pages = {}
        for line in out.splitlines():
            m = re.match(r'^(.+?):\s+(\d+)', line)
            if m:
                pages[m.group(1).strip()] = int(m.group(2))

        page_size = int(_sysctl("vm.pagesize") or "16384")

        # Free + inactive + speculative ≈ available
        free = pages.get("Pages free", 0)
        inactive = pages.get("Pages inactive", 0)
        speculative = pages.get("Pages speculative", 0)
        purgeable = pages.get("Pages purgeable", 0)

        available_pages = free + inactive + speculative + purgeable
        return (available_pages * page_size) // (1024 * 1024)
    except Exception:
        return 0


# ── System ───────────────────────────────────────────────────────────────────

def _cpu_pct():
    """Get CPU usage percentage on macOS."""
    try:
        # Use top in logging mode for a single sample
        out = subprocess.check_output(
            ["top", "-l", "1", "-n", "0", "-s", "0"],
            timeout=10, text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "CPU usage" in line:
                m = re.search(r'(\d+\.?\d*)% idle', line)
                if m:
                    return round(100 - float(m.group(1)), 1)
        return 0
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


def is_apple_silicon():
    """Detect if running on Apple Silicon."""
    return platform.machine() == "arm64" and platform.system() == "Darwin"
