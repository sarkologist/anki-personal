#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 ARTIFACT_DIR [ITERATIONS] [--config FILE]" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
python_bin="${ANKI_PYTHON:-/Users/sark/code/anki/out/pyenv/bin/python}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
export QTWEBENGINE_CHROMIUM_FLAGS="${QTWEBENGINE_CHROMIUM_FLAGS:---single-process --no-sandbox --disable-gpu --disable-software-rasterizer}"
exec "$python_bin" "$script_dir/qt_driver.py" "$@"
