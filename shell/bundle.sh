#!/bin/bash
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
#
# usage: bundle.sh <binary> <web assets folder> <Info.plist> <icon.icns>
#   <generated pylib folder> <generated hooks.py> <output binary path>
# Assembles AnkiShell.app around the output path (.../Contents/MacOS/anki-shell).

set -euo pipefail

bin=$1
web=$2
plist=$3
icon=$4
pylib_gen=$5
hooks=$6
out=$7
src=$(cd "$(dirname "$0")/.." && pwd)

contents=$(dirname "$(dirname "$out")")
rm -rf "$contents"
mkdir -p "$contents/MacOS" "$contents/Resources"
cp "$plist" "$contents/Info.plist"
cp "$icon" "$contents/Resources/AppIcon.icns"
# the web assets are read at runtime from Resources/web
cp -R "$web" "$contents/Resources/web"
# the plugin host, with pylib (no _rsbridge: it uses the shell's backend)
# and the aqt hooks it shares with Anki
host="$contents/Resources/plugin_host"
rsync -a --exclude __pycache__ --exclude tests "$src/shell/plugin_host/" "$host/"
rsync -a --exclude __pycache__ "$src/pylib/anki/" "$host/anki/"
rsync -a --exclude __pycache__ --exclude '*.so' "$pylib_gen/" "$host/anki/"
mkdir -p "$host/_aqt"
cp "$hooks" "$host/_aqt/hooks.py"
cp "$bin" "$out"
# ad-hoc signature, so macOS will run the bundle locally
codesign --force --deep --sign - "$(dirname "$contents")" >/dev/null
