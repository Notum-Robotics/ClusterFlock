#!/usr/bin/env bash
# build.sh — Build llama.cpp for ClusterFlock Linux Agent
#
# Creates prebuilt binaries for CUDA 12, CUDA 11, and CPU-only.
# Run this on a build machine with CUDA toolkit(s) installed.
# The resulting build/ directory ships with the agent —
# target hosts only need NVIDIA drivers, not the CUDA toolkit.
#
# Usage:
#   ./build.sh              # Build all variants
#   ./build.sh cuda12       # Build CUDA 12 only
#   ./build.sh cuda11       # Build CUDA 11 only
#   ./build.sh cpu          # Build CPU-only
#
# Prerequisites:
#   - cmake >= 3.14
#   - gcc/g++ (build-essential)
#   - CUDA toolkit 12.x and/or 11.x (for GPU builds)
#   - libcurl4-openssl-dev (for HuggingFace downloads)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_SRC="$SCRIPT_DIR/llama_cpp"
BUILD_ROOT="$SCRIPT_DIR/build"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

# Clone llama.cpp if source tree not present
if [ ! -d "$LLAMA_SRC" ]; then
    echo "Cloning llama.cpp..."
    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_SRC"
fi

# ── Library bundling ─────────────────────────────────────────────────────
# Copy shared library dependencies alongside the binary so the host only
# needs the NVIDIA driver (libcuda.so, libnvidia-*.so).
# System C/math/pthread/linker libs are excluded.

bundle_libs() {
    local BIN="$1"
    local DEST="$2"

    ldd "$BIN" 2>/dev/null | while IFS= read -r line; do
        lib=$(echo "$line" | awk '{print $3}')
        [ -z "$lib" ] && continue
        [ ! -f "$lib" ] && continue

        local base
        base="$(basename "$lib")"

        # Skip core system libs (always present on any Linux)
        case "$base" in
            libc.so*|libm.so*|libdl.so*|librt.so*|libpthread.so*) continue ;;
            libgcc_s.so*|ld-linux*) continue ;;
        esac

        # Skip NVIDIA driver libs (provided by the host driver package)
        case "$base" in
            libcuda.so*|libnvidia-*.so*) continue ;;
        esac

        # Bundle everything else (llama.cpp libs, CUDA runtime, cuBLAS,
        # libstdc++, libcurl, etc.)
        cp -L "$lib" "$DEST/" 2>/dev/null || true
    done
}


# ── Build a single variant ───────────────────────────────────────────────

build_variant() {
    local VARIANT="$1"
    local DEST="$BUILD_ROOT/$VARIANT"
    local BDIR="$LLAMA_SRC/build-$VARIANT"

    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  Building: $VARIANT"
    echo "════════════════════════════════════════════════════════"

    rm -rf "$BDIR"
    mkdir -p "$BDIR" "$DEST"

    local CMAKE_ARGS=(
        -B "$BDIR"
        -S "$LLAMA_SRC"
        -DCMAKE_BUILD_TYPE=Release
        -DLLAMA_CURL=ON
    )

    case "$VARIANT" in
        cuda12)
            local CUDA_PATH=""
            for p in /usr/local/cuda-12 /usr/local/cuda; do
                if [ -d "$p" ] && "$p/bin/nvcc" --version 2>/dev/null | grep -q "release 12"; then
                    CUDA_PATH="$p"; break
                fi
            done
            if [ -z "$CUDA_PATH" ]; then
                echo "  CUDA 12 toolkit not found — skipping"
                return 1
            fi
            echo "  CUDA toolkit: $CUDA_PATH"
            CMAKE_ARGS+=(
                -DGGML_CUDA=ON
                -DGGML_CUDA_FA=ON
                -DGGML_CUDA_FA_ALL_QUANTS=ON
                -DCMAKE_CUDA_COMPILER="$CUDA_PATH/bin/nvcc"
            )
            ;;
        cuda11)
            local CUDA_PATH=""
            for p in /usr/local/cuda-11 /usr/local/cuda; do
                if [ -d "$p" ] && "$p/bin/nvcc" --version 2>/dev/null | grep -q "release 11"; then
                    CUDA_PATH="$p"; break
                fi
            done
            if [ -z "$CUDA_PATH" ]; then
                echo "  CUDA 11 toolkit not found — skipping"
                return 1
            fi
            echo "  CUDA toolkit: $CUDA_PATH"
            CMAKE_ARGS+=(
                -DGGML_CUDA=ON
                -DGGML_CUDA_FA=ON
                -DCMAKE_CUDA_COMPILER="$CUDA_PATH/bin/nvcc"
            )
            ;;
        cpu)
            # Pure CPU build — no CUDA dependency at all.
            # Used for CPU/RAM device or hosts without NVIDIA GPUs.
            CMAKE_ARGS+=(-DGGML_CUDA=OFF)
            ;;
        *)
            echo "Unknown variant: $VARIANT"
            return 1
            ;;
    esac

    echo "  Configuring..."
    cmake "${CMAKE_ARGS[@]}"

    echo "  Building with $JOBS jobs..."
    cmake --build "$BDIR" --config Release -j "$JOBS" --target llama-server

    # Find the built binary
    local BIN=""
    for candidate in "$BDIR/bin/llama-server" "$BDIR/bin/Release/llama-server"; do
        if [ -f "$candidate" ]; then BIN="$candidate"; break; fi
    done
    if [ -z "$BIN" ]; then
        echo "  ERROR: llama-server not found after build"
        return 1
    fi

    cp "$BIN" "$DEST/"
    chmod +x "$DEST/llama-server"

    echo "  Bundling shared libraries..."
    bundle_libs "$DEST/llama-server" "$DEST"

    local COUNT
    COUNT=$(find "$DEST" -type f | wc -l)
    echo "  ✓ $VARIANT complete — $COUNT files in $DEST/"

    # Cleanup intermediate build tree (large)
    rm -rf "$BDIR"
}


# ── Main ─────────────────────────────────────────────────────────────────

TARGETS="${1:-all}"

mkdir -p "$BUILD_ROOT"

if [ "$TARGETS" = "all" ]; then
    build_variant cuda12 || echo "  (cuda12 skipped)"
    build_variant cuda11 || echo "  (cuda11 skipped)"
    build_variant cpu
else
    build_variant "$TARGETS"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Build complete"
echo "════════════════════════════════════════════════════════"
echo ""
echo "Prebuilt binaries:"
find "$BUILD_ROOT" -type f -name "llama-server" -exec ls -lh {} \;
echo ""
echo "Deploy by copying agents/agent_linux/ to target hosts."
echo "Target hosts only need NVIDIA drivers — no CUDA toolkit."
