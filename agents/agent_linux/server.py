"""llama.cpp server management for Linux (amd64 + CUDA).

Manages multiple llama-server instances — one per device (GPU or CPU).
Each GPU gets its own server pinned via CUDA_VISIBLE_DEVICES.
CPU-only mode runs with --n-gpu-layers 0 for system RAM inference.

Prebuilt binaries are shipped in build/{cuda12,cuda11,cpu}/.
The host only needs NVIDIA drivers installed, not the CUDA toolkit.
All CUDA runtime libs are bundled alongside the binaries.
"""

import json
import os
import re
import signal
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

AGENT_DIR = Path(__file__).parent
PREBUILT_DIR = AGENT_DIR / "build"
LLAMA_CPP_DIR = AGENT_DIR / "llama_cpp"       # source tree (for build from source)
SOURCE_BUILD_DIR = LLAMA_CPP_DIR / "build"
MODELS_DIR = AGENT_DIR / "models"

# Port allocation: gpu0 = 8080, gpu1 = 8081, ..., cpu = 8090
_BASE_GPU_PORT = 8080
_CPU_PORT = 8090
DEFAULT_PORT = _BASE_GPU_PORT

# Active server instances: device_id → {proc, port, model_id, model_path}
_servers = {}


def _port_for_device(device_id):
    """Map device ID to port number."""
    if device_id == "cpu":
        return _CPU_PORT
    idx = int(device_id.replace("gpu", ""))
    return _BASE_GPU_PORT + idx


# ── CUDA version detection ───────────────────────────────────────────────

def _detect_cuda_version():
    """Detect CUDA version from the installed NVIDIA driver.

    Returns major version (e.g. 12, 11) or None if no driver found.
    """
    from hardware import _find_nvidia_smi
    nvsmi = _find_nvidia_smi()
    if not nvsmi:
        return None
    try:
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

    GPU devices: prefers build/cuda{ver}/ matching host CUDA driver.
    CPU device:  prefers build/cpu/ (no CUDA dependency).
    Falls back through available variants.
    """
    # CPU device prefers the CPU-only build (zero CUDA dependency)
    if device_id == "cpu":
        cpu_bin = PREBUILT_DIR / "cpu" / "llama-server"
        if cpu_bin.exists():
            return str(cpu_bin)
        # Fall through to CUDA binary (works with n_gpu_layers=0 if driver present)

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

    # Legacy flat layout (single binary in build/)
    flat = PREBUILT_DIR / "llama-server"
    if flat.exists():
        return str(flat)

    # Source build output
    for p in (SOURCE_BUILD_DIR / "bin" / "llama-server",
              SOURCE_BUILD_DIR / "bin" / "Release" / "llama-server"):
        if p.exists():
            return str(p)

    return None


def is_built():
    """Check if any llama-server binary is available."""
    return server_binary() is not None


# ── Build from source (fallback) ────────────────────────────────────────

def build(jobs=None):
    """Build llama.cpp from source with CUDA.

    For production, use build.sh to create prebuilt binaries for
    multiple CUDA versions. This is a single-variant fallback.
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

    print(f"[build] Configuring llama.cpp with CUDA...")
    cmake_args = [
        "cmake", "-B", str(build_dir), "-S", str(LLAMA_CPP_DIR),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DGGML_CUDA=ON",
        "-DGGML_CUDA_FA=ON",
        "-DGGML_CUDA_FA_ALL_QUANTS=ON",
        "-DLLAMA_CURL=ON",
    ]
    r = subprocess.run(cmake_args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cmake configure failed:\n{r.stderr}")

    print(f"[build] Building with {jobs} jobs...")
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


# ── Server lifecycle ─────────────────────────────────────────────────────

def start_server(model_path, *, device="gpu0", port=None, ctx_size=131072,
                 parallel=4, threads=None, host="127.0.0.1", extra_args=None,
                 _retry_count=0, _max_retries=5):
    """Start a llama-server instance pinned to a specific device.

    Args:
        model_path: Path to GGUF model file.
        device: "gpu0", "gpu1", ..., "gpuN", or "cpu".
        port: Override port (default: auto-assigned from device).
        ctx_size: Context window size.
        parallel: Parallel inference slots.
        threads: CPU threads (auto for GPU, fixed 4 for CPU device).
        host: Listen address.
        extra_args: Additional CLI arguments.
    """
    if port is None:
        port = _port_for_device(device)

    binary = server_binary(device)
    if not binary:
        raise RuntimeError("llama-server not found. Run build.sh or build() first.")
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"Model not found: {model_path}")

    # Stop existing server on this device if any
    stop_server(device)

    is_cpu = (device == "cpu")

    if is_cpu:
        # ── LINUX-ONLY: CPU/RAM device ──
        # Runs model entirely in system RAM, no GPU offload.
        # Fixed 4 CPU cores as specified in requirements.
        n_gpu_layers = 0
        threads = 4
        flash_attn = "off"
        cache_type_k = "f16"
        cache_type_v = "f16"
    else:
        # GPU device: full offload, flash attention, quantized KV cache.
        # Absolutely no model splitting — entire model on one GPU.
        n_gpu_layers = 9999
        if threads is None:
            threads = max(1, (os.cpu_count() or 4) // 2)
        flash_attn = "on"
        cache_type_k = "q4_0"
        cache_type_v = "q4_0"

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
    if is_cpu:
        env["CUDA_VISIBLE_DEVICES"] = ""
    else:
        gpu_idx = int(device.replace("gpu", ""))
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_idx)
    # Bundled shared libs (CUDA runtime, cuBLAS, etc.) — host needs only the driver
    bin_dir = str(Path(binary).parent)
    env["LD_LIBRARY_PATH"] = bin_dir + ":" + env.get("LD_LIBRARY_PATH", "")

    tag = "CPU/RAM" if is_cpu else device.upper()
    print(f"[server] Starting llama-server [{tag}] on {host}:{port}")
    print(f"[server]   Model: {model_path}")
    print(f"[server]   Context: {ctx_size}, GPU layers: {n_gpu_layers}")
    if is_cpu:
        print(f"[server]   CPU-only mode (4 threads, system RAM)")
    else:
        print(f"[server]   Flash Attention: {flash_attn}, KV: {cache_type_k}")

    proc = subprocess.Popen(cmd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Scale load timeout with model size (CPU loads are slower)
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
                                ctx_size=new_ctx, parallel=parallel,
                                threads=threads, host=host,
                                extra_args=extra_args,
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
            # Kill any orphaned llama-server on this device's port
            port = _CPU_PORT if device == "cpu" else (_BASE_GPU_PORT + int(device.replace("gpu", "")))
            _kill_port(port)
    else:
        for dev in list(_servers.keys()):
            stop_server(dev)
        if not _servers:
            # Kill orphans on all known ports
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
    """Kill any process listening on the given TCP port."""
    try:
        out = subprocess.check_output(
            ["fuser", f"{port}/tcp"], stderr=subprocess.DEVNULL, timeout=5
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
    """Chat completion against a specific server port."""
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
        "id": data.get("id", "chatcmpl-linux"),
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
