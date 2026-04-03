"""Hardware profiling — unified for macOS (Apple Silicon) and Linux (NVIDIA).

macOS:  system_profiler + sysctl + vm_stat
Linux:  nvidia-smi + /proc

Public API (identical on all platforms):
    gpu()           → list of GPU dicts
    system()        → system info dict
    live_metrics()  → current metrics for heartbeat
    profile()       → full hardware profile
    snapshot()      → alias for profile()
"""

import json
import os
import platform
import re
import shutil
import subprocess
import time

_IS_DARWIN = platform.system() == "Darwin"

_cache = {}
_TTL = 5


def _cached(key, fn):
    now = time.time()
    if key in _cache and now - _cache[key][1] < _TTL:
        return _cache[key][0]
    v = fn()
    _cache[key] = (v, now)
    return v


# ═══════════════════════════════════════════════════════════════════════════
#  GPU detection
# ═══════════════════════════════════════════════════════════════════════════

def gpu():
    return _cached("gpu", _gpu_probe)


# ── macOS (Apple Silicon / Metal) ────────────────────────────────────────

def _sysctl(key):
    """Read a sysctl value (macOS)."""
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", key], timeout=5, text=True, stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except Exception:
        return ""


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


def _gpu_probe_darwin():
    """Detect Apple Silicon GPU via system_profiler."""
    total_mb, free_mb = _mem_info()
    gpu_name = _apple_gpu_name()
    if not gpu_name:
        gpu_name = _sysctl("machdep.cpu.brand_string") or "Apple Silicon"
    return [{
        "name": gpu_name,
        "unified": True,
        "vram_total_mb": total_mb,
        "vram_free_mb": free_mb,
        "utilization_pct": 0,
    }]


# ── Linux (NVIDIA) ──────────────────────────────────────────────────────

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


def _tegrastats_gpu_util():
    """Read GPU utilization from tegrastats (Jetson / Tegra devices).

    tegrastats outputs a continuous stream like:
        RAM 3469/7620MB ... GR3D_FREQ 45% ...
    We grab one line via a short timeout and parse GR3D_FREQ.
    Returns an int (0-100) or None on failure.
    """
    try:
        out = subprocess.check_output(
            ["timeout", "1", "tegrastats", "--interval", "500"],
            timeout=3, text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.strip().splitlines()[:1]:
            m = re.search(r'GR3D_FREQ\s+(\d+)%', line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _gpu_probe_linux():
    """Detect NVIDIA GPUs via nvidia-smi."""
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
                # Unified memory GPUs (e.g. DGX Spark GB10) report [N/A]
                if mem_total == "[N/A]" or mem_free == "[N/A]":
                    total, free = _mem_info()
                    util_pct = int(util) if util != "[N/A]" else None
                    # Tegra/Jetson: nvidia-smi reports [N/A] for utilization,
                    # fall back to tegrastats GR3D_FREQ
                    if util_pct is None:
                        util_pct = _tegrastats_gpu_util() or 0
                    gpus.append({
                        "name": name, "unified": True,
                        "vram_total_mb": total,
                        "vram_free_mb": free,
                        "utilization_pct": util_pct,
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


def _gpu_probe():
    if _IS_DARWIN:
        return _gpu_probe_darwin()
    return _gpu_probe_linux()


# ═══════════════════════════════════════════════════════════════════════════
#  Memory info
# ═══════════════════════════════════════════════════════════════════════════

def _mem_info():
    """(total_mb, available_mb) — platform-dispatched."""
    if _IS_DARWIN:
        return _mem_info_darwin()
    return _mem_info_linux()


def _mem_info_darwin():
    """(total_mb, available_mb) for macOS."""
    total_bytes = _sysctl("hw.memsize")
    total_mb = int(total_bytes) // (1024 * 1024) if total_bytes else 0
    free_mb = _vm_stat_free_mb()
    if free_mb == 0:
        free_mb = total_mb // 2
    return total_mb, free_mb


def _vm_stat_free_mb():
    """Parse vm_stat to get approximate free memory in MB (macOS)."""
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
        free = pages.get("Pages free", 0)
        inactive = pages.get("Pages inactive", 0)
        speculative = pages.get("Pages speculative", 0)
        purgeable = pages.get("Pages purgeable", 0)
        available_pages = free + inactive + speculative + purgeable
        return (available_pages * page_size) // (1024 * 1024)
    except Exception:
        return 0


def _mem_info_linux():
    """(total_mb, available_mb) via /proc/meminfo."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total, avail
    except Exception:
        return 0, 0


# ═══════════════════════════════════════════════════════════════════════════
#  CPU usage
# ═══════════════════════════════════════════════════════════════════════════

def _cpu_pct():
    if _IS_DARWIN:
        return _cpu_pct_darwin()
    return _cpu_pct_linux()


def _cpu_pct_darwin():
    """Get CPU usage percentage on macOS."""
    try:
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


def _cpu_pct_linux():
    """Get CPU usage percentage via /proc/stat."""
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


# ═══════════════════════════════════════════════════════════════════════════
#  System info & public API
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
#  Platform detection helpers
# ═══════════════════════════════════════════════════════════════════════════

def is_apple_silicon():
    """Detect if running on Apple Silicon."""
    return platform.machine() == "arm64" and _IS_DARWIN


def is_dgx_spark():
    """Detect if running on DGX Spark (GB10 / Blackwell)."""
    if _IS_DARWIN:
        return False
    gpus = gpu()
    for g in gpus:
        name = g.get("name", "").lower()
        if "gb10" in name or "dgx" in name or "blackwell" in name:
            return True
    nvsmi = _find_nvidia_smi()
    if nvsmi:
        try:
            out = subprocess.check_output(
                [nvsmi, "--query-gpu=compute_cap", "--format=csv,noheader"],
                timeout=5, text=True, stderr=subprocess.DEVNULL,
            )
            cap = out.strip()
            if cap and float(cap) >= 10.0:
                return True
        except Exception:
            pass
    return False


def detect_platform():
    """Return a string identifying the platform type for display purposes."""
    if is_apple_silicon():
        return "mac"
    if is_dgx_spark():
        return "spark"
    if not _IS_DARWIN and _find_nvidia_smi():
        return "linux-cuda"
    if not _IS_DARWIN:
        return "linux-cpu"
    return "unknown"
