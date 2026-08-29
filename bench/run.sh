#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 ARTIFACT_DIR [ITERATIONS] [--config FILE]" >&2
    echo "ARTIFACT_DIR may be the built web/ directory or its js/ directory." >&2
    exit 2
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
artifact_dir="$1"
shift
if [[ $# -eq 0 || "$1" == --* ]]; then
    set -- 5 "$@"
fi
exec python3 "$script_dir/benchmark.py" "$artifact_dir" "$@"
