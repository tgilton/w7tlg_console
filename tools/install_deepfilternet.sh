#!/usr/bin/env bash
#
# Install DeepFilterNet3 noise reduction into the active venv.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# deepfilternet 0.5.6 / deepfilterlib 0.5.6 publish metadata capping numpy<2.0
# (stale pre-NumPy-2 packaging conservatism from mid-2024; 0.5.6 is the final
# release, never updated). This project pins numpy==2.4.6, so a normal
# `pip install deepfilternet` refuses to resolve. The cap is not a real runtime
# dependency — verified 2026-07-07 that DeepFilterNet3 loads and enhances audio
# correctly under numpy 2.4.6 (the FFI boundary only marshals arrays into the
# compiled Rust core). We therefore install with --no-deps to bypass the stale
# cap, after the numpy-2-safe deps are already in from requirements.txt.
#
# deepfilterlib has no prebuilt wheel for py3.12/macOS-arm64 and compiles from
# source, so Rust/cargo must be on PATH.
#
# Run this AFTER `pip install -r requirements.txt`, with the venv activated
# (or it will use whatever `pip` is first on PATH).
#
set -euo pipefail

# Make rustup's cargo visible even in a non-login shell.
export PATH="$HOME/.cargo/bin:$PATH"

if ! command -v cargo >/dev/null 2>&1; then
  echo "ERROR: cargo not found on PATH." >&2
  echo "deepfilterlib compiles from source and needs Rust. Install with:" >&2
  echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh" >&2
  exit 1
fi

echo "cargo: $(cargo --version)"
echo "python: $(python -c 'import sys; print(sys.executable)')"
echo "numpy before: $(python -c 'import numpy; print(numpy.__version__)')"

echo "Installing deepfilterlib==0.5.6 (--no-deps, compiles Rust core)..."
pip install deepfilterlib==0.5.6 --no-deps

echo "Installing deepfilternet==0.5.6 (--no-deps)..."
pip install deepfilternet==0.5.6 --no-deps

# Sanity: numpy must NOT have been downgraded, and NR must import.
python - <<'PY'
import numpy
assert numpy.__version__.startswith("2."), f"numpy got downgraded to {numpy.__version__}!"
print("numpy after:", numpy.__version__, "(unchanged, good)")
import sys, types
try:
    import torchaudio.backend.common  # noqa
except ModuleNotFoundError:
    b = types.ModuleType("torchaudio.backend"); c = types.ModuleType("torchaudio.backend.common")
    class AudioMetaData: pass
    c.AudioMetaData = AudioMetaData; b.common = c
    sys.modules.setdefault("torchaudio.backend", b)
    sys.modules.setdefault("torchaudio.backend.common", c)
from df.enhance import enhance, init_df  # noqa
print("df.enhance imports OK — noise reduction available")
PY

echo "Done. DeepFilterNet3 noise reduction installed."
