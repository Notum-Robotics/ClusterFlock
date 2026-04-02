"""Hardware profiling: GPU, CPU, RAM, disk. Zero external dependencies."""

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
    for d in ("/usr/sbin", "/usr/local/sbin", "/usr/lib/nvidia/bin"):
        c = os.path.join(d, "nvidia-smi")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _gpu_probe():
    # NVIDIA via nvidia-smi (ships with drivers)
    _nvsmi = _find_nvidia_smi()
    if _nvsmi:
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
                    # Unified memory GPUs (GB10/DGX Spark, Jetson) report [N/A]
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
            if gpus:
                return gpus
        except Exception:
            pass

    # Apple Silicon — unified memory acts as VRAM
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return _apple_silicon()

    # Jetson / Tegra — unified memory, nvidia-smi may be absent or return nothing
    jetson = _jetson_gpu()
    if jetson:
        return jetson

    # Fallback — lspci for specialty devices
    if shutil.which("lspci"):
        try:
            out = subprocess.check_output(
                ["lspci"], timeout=5, text=True, stderr=subprocess.DEVNULL
            )
            found = []
            for line in out.splitlines():
                if any(k in line for k in ("VGA", "3D", "Display")):
                    found.append({"name": line.split(": ", 1)[-1],
                                  "vram_total_mb": 0, "vram_free_mb": 0})
            if found:
                return found
        except Exception:
            pass

    return []


def _jetson_gpu():
    """Detect NVIDIA Jetson (Orin, Xavier, etc.) with unified memory."""
    if platform.system() != "Linux":
        return None
    try:
        model = Path("/proc/device-tree/model").read_text().strip("\x00\n")
    except Exception:
        return None
    if "jetson" not in model.lower() and "orin" not in model.lower():
        return None
    total, free = _mem_info()
    if total <= 0:
        return None
    return [{
        "name": model,
        "unified": True,
        "vram_total_mb": total,
        "vram_free_mb": free,
        "utilization_pct": round((1 - free / total) * 100, 1) if total else 0,
    }]


def _apple_silicon():
    total, free = _mem_info()
    try:
        out = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            timeout=5, stderr=subprocess.DEVNULL,
        )
        displays = json.loads(out).get("SPDisplaysDataType", [])
        name = displays[0].get("sppci_model", "Apple Silicon GPU") if displays else "Apple Silicon GPU"
    except Exception:
        name = "Apple Silicon GPU"
    return [{
        "name": name, "unified": True,
        "vram_total_mb": total,
        "vram_free_mb": free,
        "utilization_pct": round((1 - free / total) * 100, 1) if total else 0,
    }]


# ── Memory ───────────────────────────────────────────────────────────────────

def _mem_info():
    """(total_mb, available_mb) via OS-native reads."""
    sysname = platform.system()

    if sysname == "Linux":
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total, avail

    if sysname == "Darwin":
        total = int(subprocess.check_output(
            ["sysctl", "-n", "hw.memsize"], timeout=5, text=True
        ).strip()) // (1024 ** 2)
        out = subprocess.check_output(["vm_stat"], timeout=5, text=True)
        ps = 16384
        m = re.search(r"page size of (\d+)", out)
        if m:
            ps = int(m.group(1))
        pages = 0
        for key in ("Pages free", "Pages inactive", "Pages purgeable", "Pages speculative"):
            m = re.search(rf"{key}:\s+(\d+)", out)
            if m:
                pages += int(m.group(1))
        return total, (pages * ps) // (1024 ** 2)

    if sysname == "Windows":
        try:
            out = subprocess.check_output(
                ["wmic", "OS", "get", "TotalVisibleMemorySize,FreePhysicalMemory", "/value"],
                timeout=5, text=True, stderr=subprocess.DEVNULL,
            )
            v = dict(re.findall(r"(\w+)=(\d+)", out))
            return int(v.get("TotalVisibleMemorySize", 0)) // 1024, \
                   int(v.get("FreePhysicalMemory", 0)) // 1024
        except Exception:
            pass

    return 0, 0


def _cpu_pct():
    try:
        return round(min(os.getloadavg()[0] / (os.cpu_count() or 1) * 100, 100), 1)
    except (OSError, AttributeError):
        pass
    # Windows fallback
    try:
        out = subprocess.check_output(
            ["wmic", "cpu", "get", "LoadPercentage", "/value"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        )
        m = re.search(r"LoadPercentage=(\d+)", out)
        return float(m.group(1)) if m else 0.0
    except Exception:
        return 0.0


# ── System ───────────────────────────────────────────────────────────────────

def system():
    return _cached("sys", _sys_probe)


def _sys_probe():
    total, free = _mem_info()
    disk = shutil.disk_usage("/")
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "hostname": platform.node(),
        "cpu_count": os.cpu_count() or 1,
        "cpu_pct": _cpu_pct(),
        "ram_total_mb": total,
        "ram_free_mb": free,
        "ram_pct": round((1 - free / total) * 100, 1) if total else 0,
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
    }


# ── Convenience ──────────────────────────────────────────────────────────────

def snapshot():
    return {"gpu": gpu(), "system": system()}


def profile():
    """Static hardware capacity — call once at startup before models load."""
    gpus = gpu()
    total_ram, _ = _mem_info()
    disk = shutil.disk_usage("/")
    return {
        "gpu": [
            {k: g[k] for k in ("name", "vram_total_mb") if k in g}
            | ({"unified": True} if g.get("unified") else {})
            for g in gpus
        ],
        "system": {
            "os": platform.system(),
            "arch": platform.machine(),
            "hostname": platform.node(),
            "cpu_count": os.cpu_count() or 1,
            "ram_total_mb": total_ram,
            "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        },
    }


def live_metrics():
    """Fresh utilization numbers — safe to call while models are loaded."""
    gpus = gpu()
    total_ram, free_ram = _mem_info()
    disk = shutil.disk_usage("/")
    return {
        "gpu": [
            {"vram_free_mb": g.get("vram_free_mb", 0),
             "utilization_pct": g.get("utilization_pct", 0)}
            for g in gpus
        ],
        "system": {
            "cpu_pct": _cpu_pct(),
            "ram_free_mb": free_ram,
            "ram_pct": round((1 - free_ram / total_ram) * 100, 1) if total_ram else 0,
            "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        },
    }



