"""llama.cpp server management — unified for all platforms.

Manages llama-server instances with platform-aware defaults:
  macOS:  Metal GPU acceleration, f16 KV cache, DYLD_LIBRARY_PATH
  Linux:  CUDA multi-GPU, q4_0 KV cache, LD_LIBRARY_PATH, CUDA_VISIBLE_DEVICES

Multi-device model (from agent_linux):
  Each GPU gets its own server instance pinned via CUDA_VISIBLE_DEVICES.
  On macOS/unified-memory, a single device "gpu0" covers the whole SoC.
  Port allocation: gpu0=8080, gpu1=8081, ..., cpu=8090.
"""

import json
import os
import platform
import re
import signal
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

_IS_DARWIN = platform.system() == "Darwin"

AGENT_DIR = Path(__file__).parent
PREBUILT_DIR = AGENT_DIR / "build"
LLAMA_CPP_DIR = AGENT_DIR / "llama_cpp"
SOURCE_BUILD_DIR = LLAMA_CPP_DIR / "build"
MODELS_DIR = AGENT_DIR / "models"

# Port allocation: gpu0 = 8080, gpu1 = 8081, ..., cpu = 8090
_BASE_GPU_PORT = 8080
_CPU_PORT = 8090
DEFAULT_PORT = _BASE_GPU_PORT

# Active server instances: device_id → {proc, port, host, model_id, model_path}
_servers = {}


def _port_for_device(device_id):
    """Map device ID to port number."""
    if device_id == "cpu":
        return _CPU_PORT
    idx = int(device_id.replace("gpu", ""))
    return _BASE_GPU_PORT + idx


# ── CUDA version detection (Linux only) ─────────────────────────────────

def _detect_cuda_version():
    """Detect CUDA version from NVIDIA driver. Returns major version or None."""
    if _IS_DARWIN:
        return None
    try:
        from hardware import _find_nvidia_smi
        nvsmi = _find_nvidia_smi()
        if not nvsmi:
            return None
        out = subprocess.check_output(
            [nvsmi], timeout=5, text=True, stderr=subprocess.DEVNULL,
        )
        m = re.search(r'CUDA Version:\s*(\d+)', out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


# ── Binary selection ─────────────────────────────────────────────────────

def server_binary(device_id="gpu0"):
    """Return path to the best available llama-server binary.

    macOS: flat build/ directory (single Metal binary).
    Linux GPU: build/cuda{ver}/ matching host CUDA driver.
    Linux CPU: build/cpu/ (no CUDA dependency).
    Falls back through available variants.
    """
    if _IS_DARWIN:
        # macOS: single binary in flat build/ or source build
        prebuilt = PREBUILT_DIR / "llama-server"
        if prebuilt.exists():
            return str(prebuilt)
        src = SOURCE_BUILD_DIR / "bin" / "llama-server"
        if src.exists():
            return str(src)
        return None

    # Linux: CPU device prefers CPU-only build
    if device_id == "cpu":
        cpu_bin = PREBUILT_DIR / "cpu" / "llama-server"
        if cpu_bin.exists():
            return str(cpu_bin)

    # Match host CUDA driver version
    cuda_ver = _detect_cuda_version()
    if cuda_ver:
        exact = PREBUILT_DIR / f"cuda{cuda_ver}" / "llama-server"
        if exact.exists():
            return str(exact)

    # Try available CUDA builds, newest first
    for v in (12, 11):
        candidate = PREBUILT_DIR / f"cuda{v}" / "llama-server"
        if candidate.exists():
            return str(candidate)

    # Flat layout fallback
    flat = PREBUILT_DIR / "llama-server"
    if flat.exists():
        return str(flat)

    # Source build
    for p in (SOURCE_BUILD_DIR / "bin" / "llama-server",
              SOURCE_BUILD_DIR / "bin" / "Release" / "llama-server"):
        if p.exists():
            return str(p)

    return None


def is_built():
    """Check if any llama-server binary is available."""
    return server_binary() is not None


# ── Build from source ────────────────────────────────────────────────────

def build(jobs=None):
    """Build llama.cpp from source with platform-appropriate flags.

    macOS:  Metal + arm64 optimizations.
    Linux:  CUDA + Flash Attention kernels.
    """
    if not LLAMA_CPP_DIR.exists():
        raise RuntimeError(
            f"llama.cpp source not found at {LLAMA_CPP_DIR}\n"
            "Clone:  git clone --depth 1 https://github.com/ggerganov/llama.cpp.git llama_cpp"
        )

    build_dir = SOURCE_BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)

    if jobs is None:
        jobs = max(1, os.cpu_count() or 4)

    if _IS_DARWIN:
        print("[build] Configuring llama.cpp with Metal (Apple Silicon)...")
        cmake_args = [
            "cmake", "-B", str(build_dir), "-S", str(LLAMA_CPP_DIR),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DGGML_METAL=ON",
            "-DLLAMA_CURL=OFF",
            "-DLLAMA_OPENSSL=OFF",
            "-DCMAKE_OSX_ARCHITECTURES=arm64",
        ]
    else:
        print("[build] Configuring llama.cpp with CUDA...")
        cmake_args = [
            "cmake", "-B", str(build_dir), "-S", str(LLAMA_CPP_DIR),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DGGML_CUDA=ON",
            "-DGGML_CUDA_FA=ON",
            "-DGGML_CUDA_FA_ALL_QUANTS=ON",
            "-DGGML_CUDA_GRAPHS=ON",
            "-DLLAMA_CURL=ON",
        ]

    r = subprocess.run(cmake_args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmake configure failed:\n{r.stderr}")

    print(f"[build] Building with {jobs} parallel jobs...")
    r = subprocess.run(
        ["cmake", "--build", str(build_dir), "--config", "Release",
         "-j", str(jobs), "--target", "llama-server"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"cmake build failed:\n{r.stderr}")

    if not server_binary():
        raise RuntimeError("Build completed but llama-server not found")
    print(f"[build] ✓ llama-server: {server_binary()}")


# ── Context auto-detection (macOS unified memory) ────────────────────────

def _auto_context_size(model_path):
    """Pick a context size based on available memory and model size.

    Primarily useful on macOS unified memory where GPU/CPU share RAM.
    """
    from hardware import _mem_info
    _, free_mb = _mem_info()
    model_size_mb = os.path.getsize(model_path) / (1024 * 1024)
    available_for_ctx = (free_mb - model_size_mb * 1.2) * 0.7
    if available_for_ctx > 20000:
        return 131072
    elif available_for_ctx > 8000:
        return 65536
    elif available_for_ctx > 4000:
        return 32768
    elif available_for_ctx > 2000:
        return 16384
    else:
        return 8192


# ── Server lifecycle ─────────────────────────────────────────────────────

def start_server(model_path, *, device="gpu0", port=None, ctx_size=None,
                 n_gpu_layers=9999, parallel=4, threads=None,
                 flash_attn="on", cache_type_k=None, cache_type_v=None,
                 host="127.0.0.1", extra_args=None,
                 _retry_count=0, _max_retries=5):
    """Start a llama-server instance pinned to a specific device.

    Platform-aware defaults:
      macOS:  f16 KV cache, auto context from memory, Metal offload.
      Linux GPU: q4_0 KV cache, 131072 context, CUDA_VISIBLE_DEVICES.
      Linux CPU: no GPU offload, f16 KV cache, 4 threads.
    """
    if port is None:
        port = _port_for_device(device)

    binary = server_binary(device)
    if not binary:
        raise RuntimeError("llama-server not found. Run build.sh or build() first.")
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"Model not found: {model_path}")

    stop_server(device)

    is_cpu = (device == "cpu")

    # Platform-aware defaults
    if is_cpu:
        n_gpu_layers = 0
        threads = 4
        flash_attn = "off"
        if cache_type_k is None:
            cache_type_k = "f16"
        if cache_type_v is None:
            cache_type_v = "f16"
        if ctx_size is None:
            ctx_size = 32768
    elif _IS_DARWIN:
        if threads is None:
            threads = max(1, (os.cpu_count() or 4) // 2)
        if cache_type_k is None:
            cache_type_k = "f16"
        if cache_type_v is None:
            cache_type_v = "f16"
        if ctx_size is None:
            ctx_size = _auto_context_size(model_path)
    else:
        # Linux GPU
        if threads is None:
            threads = max(1, (os.cpu_count() or 4) // 2)
        if cache_type_k is None:
            cache_type_k = "q4_0"
        if cache_type_v is None:
            cache_type_v = "q4_0"
        if ctx_size is None:
            ctx_size = 131072

    cmd = [
        binary,
        "--model", str(model_path),
        "--port", str(port),
        "--host", host,
        "--ctx-size", str(ctx_size),
        "--n-gpu-layers", str(n_gpu_layers),
        "--parallel", str(parallel),
        "--threads", str(threads),
        "--flash-attn", flash_attn,
        "--cache-type-k", cache_type_k,
        "--cache-type-v", cache_type_v,
        "--metrics",
        "--cont-batching",
    ]
    if extra_args:
        cmd.extend(extra_args)

    # Environment
    env = os.environ.copy()
    bin_dir = str(Path(binary).parent)

    if _IS_DARWIN:
        env["DYLD_LIBRARY_PATH"] = bin_dir + ":" + env.get("DYLD_LIBRARY_PATH", "")
    else:
        env["LD_LIBRARY_PATH"] = bin_dir + ":" + env.get("LD_LIBRARY_PATH", "")
        if is_cpu:
            env["CUDA_VISIBLE_DEVICES"] = ""
        else:
            gpu_idx = int(device.replace("gpu", ""))
            env["CUDA_VISIBLE_DEVICES"] = str(gpu_idx)

    # Logging
    if is_cpu:
        tag = "CPU/RAM"
    elif _IS_DARWIN:
        tag = "Metal"
    else:
        tag = device.upper()
    print(f"[server] Starting llama-server [{tag}] on {host}:{port}")
    print(f"[server]   Model: {model_path}")
    print(f"[server]   Context: {ctx_size}, GPU layers: {n_gpu_layers}")
    print(f"[server]   Flash Attention: {flash_attn}, KV: {cache_type_k}")
    if is_cpu:
        print(f"[server]   CPU-only mode ({threads} threads, system RAM)")

    proc = subprocess.Popen(cmd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Scale load timeout with model size
    model_file = Path(model_path)
    shard_match = re.search(r'(\d{5})-of-(\d{5})', model_file.name)
    if shard_match:
        model_size_gb = sum(
            f.stat().st_size for f in model_file.parent.glob("*.gguf")
        ) / (1024**3)
    else:
        model_size_gb = model_file.stat().st_size / (1024**3)
    per_gb = 15 if is_cpu else 8
    load_timeout = max(120, int(model_size_gb * per_gb))
    print(f"[server]   Load timeout: {load_timeout}s (~{model_size_gb:.1f} GB)")

    if not _wait_for_server(port, host, timeout=load_timeout, proc=proc):
        if proc.poll() is not None:
            _, stderr = proc.communicate(timeout=5)
            err_text = stderr.decode(errors='replace')[-500:] if stderr else ""
            if err_text:
                print(f"[server] stderr: {err_text}")
        _kill_proc(proc)
        # Retry with 10% smaller context on allocation failure
        if _retry_count < _max_retries:
            new_ctx = int(ctx_size * 0.9)
            print(f"[server] ↻ Retry {_retry_count + 1}/{_max_retries}: "
                  f"reducing context {ctx_size} → {new_ctx}")
            return start_server(model_path, device=device, port=port,
                                ctx_size=new_ctx, n_gpu_layers=n_gpu_layers,
                                parallel=parallel, threads=threads,
                                flash_attn=flash_attn,
                                cache_type_k=cache_type_k,
                                cache_type_v=cache_type_v,
                                host=host, extra_args=extra_args,
                                _retry_count=_retry_count + 1,
                                _max_retries=_max_retries)
        raise RuntimeError(f"llama-server [{tag}] failed to start after "
                           f"{_max_retries} retries (final ctx={ctx_size})")

    _servers[device] = {
        "proc": proc, "port": port, "host": host,
        "model_id": None, "model_path": str(model_path),
    }
    print(f"[server] ✓ [{tag}] ready on {host}:{port}")
    return proc


def stop_server(device=None):
    """Stop server(s). If device is None, stop all."""
    if device is not None:
        info = _servers.pop(device, None)
        if info:
            _kill_proc(info["proc"])
            tag = "CPU/RAM" if device == "cpu" else device.upper()
            print(f"[server] [{tag}] stopped")
        else:
            port = _port_for_device(device)
            _kill_port(port)
    else:
        for dev in list(_servers.keys()):
            stop_server(dev)
        if not _servers:
            for p in range(_BASE_GPU_PORT, _BASE_GPU_PORT + 8):
                _kill_port(p)
            _kill_port(_CPU_PORT)


def _kill_proc(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _kill_port(port):
    """Kill any process listening on a TCP port."""
    if _IS_DARWIN:
        # macOS: use lsof
        try:
            out = subprocess.check_output(
                ["lsof", "-ti", f":{port}"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            for pid_str in out.splitlines():
                pid = int(pid_str.strip())
                os.kill(pid, signal.SIGTERM)
            if out:
                time.sleep(2)
                print(f"[server] Killed orphaned process on port {port}")
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            pass
    else:
        # Linux: use fuser
        try:
            out = subprocess.check_output(
                ["fuser", f"{port}/tcp"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            for pid_str in out.split():
                pid = int(pid_str)
                os.kill(pid, signal.SIGTERM)
            if out:
                time.sleep(2)
                print(f"[server] Killed orphaned process on port {port}")
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            pass


def _wait_for_server(port, host="127.0.0.1", timeout=120, proc=None):
    """Poll health endpoint until server is ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            req = urllib.request.Request(f"http://{host}:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                if data.get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ── Query helpers ────────────────────────────────────────────────────────

def server_running(device=None, port=None, host="127.0.0.1"):
    """Check if a server is responding."""
    if device is not None:
        p = port or _port_for_device(device)
        return _health_check(p, host)
    if port is not None:
        return _health_check(port, host)
    return any(_health_check(s["port"], s.get("host", host))
               for s in _servers.values())


def _health_check(port, host="127.0.0.1"):
    try:
        req = urllib.request.Request(f"http://{host}:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("status") == "ok"
    except Exception:
        return False


def active_devices():
    """Return dict of running device_id → {port, model_id, model_path}."""
    alive = {}
    for d, info in list(_servers.items()):
        if info["proc"].poll() is None:
            alive[d] = {k: v for k, v in info.items() if k != "proc"}
        else:
            _servers.pop(d, None)
    return alive


def server_pid(device="gpu0"):
    """Return the PID of a running server, or None."""
    info = _servers.get(device)
    if info and info["proc"].poll() is None:
        return info["proc"].pid
    return None


def get_server_context(device="gpu0", port=None, host="127.0.0.1"):
    """Get the actual per-slot context size from a running server.

    Returns the n_ctx value from /slots, or 0 if unavailable.
    """
    if port is None:
        port = _port_for_device(device)
    try:
        data = api_call("GET", "/slots", port=port, host=host, timeout=5)
        if isinstance(data, list) and data:
            return data[0].get("n_ctx", 0)
    except Exception:
        pass
    return 0


def loaded_models(device=None, port=None, host="127.0.0.1"):
    """List models from a server (OpenAI compatible)."""
    if port is None and device is not None:
        port = _port_for_device(device)
    if port is None:
        port = DEFAULT_PORT
    try:
        data = api_call("GET", "/v1/models", port=port, host=host, timeout=10)
        return data.get("data", [])
    except Exception:
        return []


# ── Inference API ────────────────────────────────────────────────────────

def api_call(method, path, body=None, port=DEFAULT_PORT, host="127.0.0.1",
             timeout=300):
    """Make an API call to a llama-server instance."""
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"llama-server {e.code}: {detail or e.reason}") from None


def complete(messages, model=None, *, max_tokens=-1, temperature=0.7,
             top_p=None, frequency_penalty=None, presence_penalty=None,
             stop=None,
             port=DEFAULT_PORT, host="127.0.0.1", generation_timeout=300):
    """Chat completion against a specific server port.

    Returns full OpenAI-compatible response dict with tokens_per_sec added.
    """
    body = {"messages": messages, "max_tokens": max_tokens,
            "temperature": temperature}
    if model:
        body["model"] = model
    if top_p is not None:
        body["top_p"] = top_p
    if frequency_penalty is not None:
        body["frequency_penalty"] = frequency_penalty
    if presence_penalty is not None:
        body["presence_penalty"] = presence_penalty
    if stop is not None:
        body["stop"] = stop

    t0 = time.time()
    data = api_call("POST", "/v1/chat/completions", body,
                    port=port, host=host, timeout=generation_timeout)
    elapsed = time.time() - t0

    choice = data.get("choices", [{}])[0]
    usage = data.get("usage", {})
    completion_tokens = usage.get("completion_tokens", 0)
    tps = completion_tokens / elapsed if elapsed > 0 else 0

    msg = choice.get("message", {})
    return {
        "id": data.get("id", "chatcmpl-agent"),
        "object": "chat.completion",
        "created": int(t0),
        "model": data.get("model", ""),
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": choice.get("finish_reason", "stop")}],
        "usage": usage,
        "tokens_per_sec": round(tps, 1),
        "elapsed_sec": round(elapsed, 2),
        "completion_tokens": completion_tokens,
    }


def benchmark(model=None, port=DEFAULT_PORT, host="127.0.0.1"):
    """Quick benchmark against a loaded model."""
    messages = [{"role": "user",
                 "content": "Write a detailed explanation of how neural networks learn through backpropagation."}]
    result = complete(messages, model, max_tokens=512, temperature=0.0,
                      port=port, host=host, generation_timeout=120)
    return {
        "tokens_per_sec": result.get("tokens_per_sec", 0),
        "completion_tokens": result.get("completion_tokens", 0),
        "elapsed_sec": result.get("elapsed_sec", 0),
    }


def metrics(port=DEFAULT_PORT, host="127.0.0.1"):
    """Fetch Prometheus metrics from a server instance."""
    try:
        req = urllib.request.Request(f"http://{host}:{port}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode()
    except Exception:
        return ""
