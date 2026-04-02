"""llama.cpp server management for DGX Spark.

Wraps llama-server binary with Spark-optimized defaults:
  - NVFP4 KV cache quantization (--cache-type-k q4_0 --cache-type-v q4_0)
  - Flash Attention enabled (--flash-attn on)
  - Full GPU offload (--n-gpu-layers 9999)
  - CUDA graphs enabled (environment variable)
  - OpenAI-compatible API on configurable port
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

# Default port for llama-server (OpenAI-compatible API)
DEFAULT_PORT = 8080
AGENT_DIR = Path(__file__).parent
LLAMA_CPP_DIR = AGENT_DIR / "llama_cpp"
SOURCE_BUILD_DIR = LLAMA_CPP_DIR / "build"
PREBUILT_DIR = AGENT_DIR / "build"          # prebuilt binaries shipped with repo
MODELS_DIR = AGENT_DIR / "models"

# Running server process
_server_proc = None


def server_binary():
    """Return path to llama-server binary, or None if not found.

    Checks prebuilt build/ first, then source-built llama_cpp/build/bin/.
    """
    # 1. Prebuilt (shipped in repo for easy deployment)
    prebuilt = PREBUILT_DIR / "llama-server"
    if prebuilt.exists():
        return str(prebuilt)
    # 2. Source build
    src = SOURCE_BUILD_DIR / "bin" / "llama-server"
    if src.exists():
        return str(src)
    alt = SOURCE_BUILD_DIR / "bin" / "Release" / "llama-server"
    if alt.exists():
        return str(alt)
    return None


def is_built():
    """Check if llama-server has been compiled."""
    return server_binary() is not None


def build(jobs=None):
    """Build llama.cpp with CUDA + Spark optimizations.

    Configured for DGX Spark (GB10 / Blackwell):
      - GGML_CUDA=ON
      - Flash Attention enabled
      - Native CUDA architecture detection (sm_100 for GB10)
      - CUDA graphs enabled
    """
    if not LLAMA_CPP_DIR.exists():
        raise RuntimeError(f"llama.cpp source not found at {LLAMA_CPP_DIR}")

    build_dir = SOURCE_BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)

    # Detect number of CPU cores for parallel build
    if jobs is None:
        try:
            jobs = os.cpu_count() or 4
        except Exception:
            jobs = 4

    print(f"[build] Configuring llama.cpp with CUDA (Spark-optimized)...")
    cmake_args = [
        "cmake",
        "-B", str(build_dir),
        "-S", str(LLAMA_CPP_DIR),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DGGML_CUDA=ON",
        "-DGGML_CUDA_FA=ON",              # Flash Attention CUDA kernels
        "-DGGML_CUDA_FA_ALL_QUANTS=ON",   # All quant types for flash attention
        "-DGGML_CUDA_GRAPHS=ON",          # CUDA graphs for reduced launch overhead
        "-DLLAMA_CURL=ON",                # Enable curl for HF downloads
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
        raise RuntimeError("Build completed but llama-server binary not found")
    print(f"[build] ✓ llama-server built: {server_binary()}")


def start_server(model_path, *, port=DEFAULT_PORT, ctx_size=131072,
                 n_gpu_layers=9999, parallel=4, threads=None,
                 flash_attn="on", cache_type_k="q4_0", cache_type_v="q4_0",
                 host="127.0.0.1", extra_args=None,
                 _retry_count=0, _max_retries=5):
    """Start llama-server with Spark-optimized settings.

    Args:
        model_path: Path to GGUF model file.
        port: API port (default 8080).
        ctx_size: Context window size.
        n_gpu_layers: Layers to offload to GPU (9999 = all).
        parallel: Number of parallel inference slots.
        threads: CPU threads (auto-detected if None).
        flash_attn: Flash Attention mode ("on", "off", "auto").
        cache_type_k: KV cache quantization for keys (q4_0 for NVFP4-like).
        cache_type_v: KV cache quantization for values.
        host: Listen address.
        extra_args: Additional CLI arguments.

    Returns:
        Process object for the running server.
    """
    global _server_proc

    binary = server_binary()
    if not binary:
        raise RuntimeError("llama-server not built. Run build() first.")

    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"Model not found: {model_path}")

    # Stop any existing server
    stop_server()

    if threads is None:
        threads = max(1, (os.cpu_count() or 4) // 2)

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

    # Environment optimizations for DGX Spark
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = env.get("CUDA_VISIBLE_DEVICES", "0")
    # Ensure shared libs next to the binary are found
    bin_dir = str(Path(binary).parent)
    env["LD_LIBRARY_PATH"] = bin_dir + ":" + env.get("LD_LIBRARY_PATH", "")

    print(f"[server] Starting llama-server on {host}:{port}")
    print(f"[server]   Model: {model_path}")
    print(f"[server]   Context: {ctx_size}, Parallel: {parallel}")
    print(f"[server]   Flash Attention: {flash_attn}")
    print(f"[server]   KV Cache: K={cache_type_k}, V={cache_type_v}")
    print(f"[server]   GPU layers: {n_gpu_layers}")

    _server_proc = subprocess.Popen(
        cmd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    # Scale timeout with model size — large models need minutes to load
    model_file = Path(model_path)
    # For multi-shard models, sum all shard sizes
    shard_match = re.search(r'(\d{5})-of-(\d{5})', model_file.name)
    if shard_match:
        model_size_gb = sum(
            f.stat().st_size for f in model_file.parent.glob("*.gguf")
        ) / (1024**3)
    else:
        model_size_gb = model_file.stat().st_size / (1024**3)
    load_timeout = max(120, int(model_size_gb * 8))  # ~8s per GB, min 120s
    print(f"[server]   Load timeout: {load_timeout}s (model ~{model_size_gb:.1f} GB)")

    if not _wait_for_server(port, host, timeout=load_timeout, proc=_server_proc):
        # Capture stderr for debugging before stopping
        if _server_proc and _server_proc.poll() is not None:
            _, stderr = _server_proc.communicate(timeout=5)
            err_text = stderr.decode(errors='replace')[-500:] if stderr else ""
            if err_text:
                print(f"[server] stderr: {err_text}")
        stop_server()
        # Retry with 10% smaller context on allocation failure
        if _retry_count < _max_retries:
            new_ctx = int(ctx_size * 0.9)
            print(f"[server] ↻ Retry {_retry_count + 1}/{_max_retries}: "
                  f"reducing context {ctx_size} → {new_ctx}")
            return start_server(model_path, port=port, ctx_size=new_ctx,
                                n_gpu_layers=n_gpu_layers, parallel=parallel,
                                threads=threads, flash_attn=flash_attn,
                                cache_type_k=cache_type_k,
                                cache_type_v=cache_type_v,
                                host=host, extra_args=extra_args,
                                _retry_count=_retry_count + 1,
                                _max_retries=_max_retries)
        raise RuntimeError(f"llama-server failed to start after "
                           f"{_max_retries} retries (final ctx={ctx_size})")

    print(f"[server] ✓ Server ready on {host}:{port}")
    return _server_proc


def _wait_for_server(port, host="127.0.0.1", timeout=120, proc=None):
    """Poll the health endpoint until the server is ready."""
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


def stop_server(port=DEFAULT_PORT):
    """Stop the running llama-server process (tracked or orphaned)."""
    global _server_proc
    if _server_proc is not None:
        try:
            _server_proc.terminate()
            _server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
            _server_proc.wait(timeout=5)
        except Exception:
            pass
        _server_proc = None
        print("[server] Server stopped")
        return

    # No tracked process — try to kill any llama-server on this port
    try:
        out = subprocess.check_output(
            ["fuser", f"{port}/tcp"], stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
        for pid_str in out.split():
            pid = int(pid_str)
            os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        print(f"[server] Killed orphaned server on port {port}")
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass  # nothing on this port or fuser not available


def server_running(port=DEFAULT_PORT, host="127.0.0.1"):
    """Check if llama-server is responding."""
    try:
        req = urllib.request.Request(f"http://{host}:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def server_pid():
    """Return the PID of the running server, or None."""
    if _server_proc and _server_proc.poll() is None:
        return _server_proc.pid
    return None


# ── Inference API ────────────────────────────────────────────────────────────

def api_call(method, path, body=None, port=DEFAULT_PORT, host="127.0.0.1",
             timeout=300):
    """Make an API call to the llama-server."""
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


def loaded_models(port=DEFAULT_PORT, host="127.0.0.1"):
    """List models loaded in the server (OpenAI compatible)."""
    try:
        data = api_call("GET", "/v1/models", port=port, host=host, timeout=10)
        return data.get("data", [])
    except Exception:
        return []


def complete(messages, model=None, *, max_tokens=-1, temperature=0.7,
             top_p=None, frequency_penalty=None, presence_penalty=None,
             stop=None,
             port=DEFAULT_PORT, host="127.0.0.1", generation_timeout=300):
    """Run chat completion against the llama-server.

    Returns dict with: content, model, usage, tokens_per_sec.
    """
    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
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

    # Return OpenAI-compatible format (same as agent_lms) so nCore can
    # extract choices[0].message.content consistently.
    msg = choice.get("message", {})
    return {
        "id": data.get("id", "chatcmpl-spark"),
        "object": "chat.completion",
        "created": int(t0),
        "model": data.get("model", ""),
        "choices": [{
            "index": 0,
            "message": msg,
            "finish_reason": choice.get("finish_reason", "stop"),
        }],
        "usage": usage,
        "tokens_per_sec": round(tps, 1),
        "elapsed_sec": round(elapsed, 2),
        "completion_tokens": completion_tokens,
    }


def benchmark(model=None, port=DEFAULT_PORT, host="127.0.0.1"):
    """Run a quick benchmark against the loaded model.

    Returns dict: tokens_per_sec, completion_tokens, elapsed_sec.
    """
    messages = [
        {"role": "user",
         "content": "Write a detailed explanation of how neural networks learn through backpropagation."}
    ]
    result = complete(messages, model, max_tokens=512, temperature=0.0,
                      port=port, host=host, generation_timeout=120)
    return {
        "tokens_per_sec": result.get("tokens_per_sec", 0),
        "completion_tokens": result.get("completion_tokens", 0),
        "elapsed_sec": result.get("elapsed_sec", 0),
    }


def metrics(port=DEFAULT_PORT, host="127.0.0.1"):
    """Fetch Prometheus metrics from llama-server."""
    try:
        req = urllib.request.Request(f"http://{host}:{port}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode()
    except Exception:
        return ""
