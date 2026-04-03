#!/usr/bin/env bash
# build.sh — Build llama.cpp for ClusterFlock Agent (all platforms)
#
# Auto-detects platform:
#   macOS:  Builds with Metal (Apple Silicon) — single flat binary
#   Linux:  Builds CUDA 12 / CUDA 11 / CPU variants with bundled libs
#
# Usage:
#   ./build.sh              # Auto-detect and build all appropriate variants
#   ./build.sh metal        # macOS Metal build
#   ./build.sh cuda12       # Linux CUDA 12 only
#   ./build.sh cuda11       # Linux CUDA 11 only
#   ./build.sh cpu          # Linux CPU-only
#
# Prerequisites:
#   macOS:  Xcode command-line tools, cmake
#   Linux:  cmake >= 3.14, gcc/g++, CUDA toolkit(s), libcurl4-openssl-dev

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_SRC="$SCRIPT_DIR/llama_cpp"
BUILD_ROOT="$SCRIPT_DIR/build"

if [[ "$(uname -s)" == "Darwin" ]]; then
    JOBS="${JOBS:-$(sysctl -n hw.ncpu 2>/dev/null || echo 4)}"
else
    JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
fi

# Clone llama.cpp if source tree not present
if [ ! -d "$LLAMA_SRC" ]; then
    echo "Cloning llama.cpp..."
    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_SRC"
fi

# ── Library bundling (Linux) ─────────────────────────────────────────────
# Copy shared library dependencies alongside the binary so the host only
# needs the NVIDIA driver (libcuda.so, libnvidia-*.so).

bundle_libs_linux() {
    local BIN="$1"
    local DEST="$2"

    ldd "$BIN" 2>/dev/null | while IFS= read -r line; do
        lib=$(echo "$line" | awk '{print $3}')
        [ -z "$lib" ] && continue
        [ ! -f "$lib" ] && continue

        local base
        base="$(basename "$lib")"

        # Skip core system libs
        case "$base" in
            libc.so*|libm.so*|libdl.so*|librt.so*|libpthread.so*) continue ;;
            libgcc_s.so*|ld-linux*) continue ;;
        esac

        # Skip NVIDIA driver libs (provided by host)
        case "$base" in
            libcuda.so*|libnvidia-*.so*) continue ;;
        esac

        cp -L "$lib" "$DEST/" 2>/dev/null || true
    done
}


# ── Build a single variant ───────────────────────────────────────────────

build_variant() {
    local VARIANT="$1"
    local BDIR="$LLAMA_SRC/build-$VARIANT"

    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  Building: $VARIANT"
    echo "════════════════════════════════════════════════════════"

    rm -rf "$BDIR"
    mkdir -p "$BDIR"

    local CMAKE_ARGS=(
        -B "$BDIR"
        -S "$LLAMA_SRC"
        -DCMAKE_BUILD_TYPE=Release
    )

    local DEST
    case "$VARIANT" in
        metal)
            DEST="$BUILD_ROOT"
            mkdir -p "$DEST"
            CMAKE_ARGS+=(
                -DGGML_METAL=ON
                -DLLAMA_CURL=OFF
                -DLLAMA_OPENSSL=OFF
                -DCMAKE_OSX_ARCHITECTURES=arm64
            )
            ;;
        cuda13)
            DEST="$BUILD_ROOT/cuda13"
            mkdir -p "$DEST"
            local CUDA_PATH=""
            for p in /usr/local/cuda-13.0 /usr/local/cuda-13 /usr/local/cuda; do
                if [ -d "$p" ] && "$p/bin/nvcc" --version 2>/dev/null | grep -q "release 13\|V13"; then
                    CUDA_PATH="$p"; break
                fi
            done
            if [ -z "$CUDA_PATH" ]; then
                echo "  CUDA 13 toolkit not found — skipping"
                return 1
            fi
            echo "  CUDA toolkit: $CUDA_PATH"
            CMAKE_ARGS+=(
                -DGGML_CUDA=ON
                -DGGML_CUDA_FA=ON
                -DGGML_CUDA_FA_ALL_QUANTS=ON
                -DLLAMA_CURL=ON
                -DCMAKE_CUDA_COMPILER="$CUDA_PATH/bin/nvcc"
            )
            ;;
        cuda12)
            DEST="$BUILD_ROOT/cuda12"
            mkdir -p "$DEST"
            local CUDA_PATH=""
            for p in /usr/local/cuda-12 /usr/local/cuda "$HOME/cuda-toolkit" "$HOME/cuda-12"; do
                if [ -d "$p" ] && "$p/bin/nvcc" --version 2>/dev/null | grep -q "release 12"; then
                    # Verify headers exist (system packages may split them)
                    if [ -f "$p/include/cuda_runtime.h" ] || [ -f "/usr/include/cuda_runtime.h" ]; then
                        CUDA_PATH="$p"; break
                    fi
                fi
            done
            # Fallback: system package without proper include dir
            if [ -z "$CUDA_PATH" ]; then
                for p in /usr/lib/nvidia-cuda-toolkit; do
                    if [ -d "$p" ] && "$p/bin/nvcc" --version 2>/dev/null | grep -q "release 12"; then
                        CUDA_PATH="$p"; break
                    fi
                done
            fi
            if [ -z "$CUDA_PATH" ]; then
                echo "  CUDA 12 toolkit not found — skipping"
                return 1
            fi
            echo "  CUDA toolkit: $CUDA_PATH"
            CMAKE_ARGS+=(
                -DGGML_CUDA=ON
                -DGGML_CUDA_FA=ON
                -DGGML_CUDA_FA_ALL_QUANTS=ON
                -DLLAMA_CURL=ON
                -DCMAKE_CUDA_COMPILER="$CUDA_PATH/bin/nvcc"
            )
            # System package installs headers in /usr/include, help CMake find them
            if [ ! -f "$CUDA_PATH/include/cuda_runtime.h" ] && [ -f "/usr/include/cuda_runtime.h" ]; then
                CMAKE_ARGS+=(
                    -DCUDAToolkit_ROOT=/usr
                    -DCMAKE_CUDA_TOOLKIT_INCLUDE_DIRECTORIES=/usr/include
                )
            fi
            ;;
        cuda11)
            DEST="$BUILD_ROOT/cuda11"
            mkdir -p "$DEST"
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
                -DLLAMA_CURL=ON
                -DCMAKE_CUDA_COMPILER="$CUDA_PATH/bin/nvcc"
            )
            ;;
        cpu)
            DEST="$BUILD_ROOT/cpu"
            mkdir -p "$DEST"
            CMAKE_ARGS+=(
                -DGGML_CUDA=OFF
                -DLLAMA_CURL=ON
            )
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

    # Bundle shared libs on Linux
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "  Bundling shared libraries..."
        bundle_libs_linux "$DEST/llama-server" "$DEST"
    fi

    local COUNT
    COUNT=$(find "$DEST" -type f | wc -l)
    echo "  ✓ $VARIANT complete — $COUNT files in $DEST/"

    # Cleanup intermediate build tree
    rm -rf "$BDIR"
}


# ── Main ─────────────────────────────────────────────────────────────────

TARGETS="${1:-auto}"

mkdir -p "$BUILD_ROOT"

if [ "$TARGETS" = "auto" ]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
        build_variant metal
    else
        build_variant cuda13 || echo "  (cuda13 skipped)"
        build_variant cuda12 || echo "  (cuda12 skipped)"
        build_variant cuda11 || echo "  (cuda11 skipped)"
        build_variant cpu
    fi
elif [ "$TARGETS" = "all" ]; then
    # Force all Linux variants
    build_variant cuda13 || echo "  (cuda13 skipped)"
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
echo "Deploy by copying /agent/ to target hosts."
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Target hosts only need NVIDIA drivers — no CUDA toolkit."
fi
