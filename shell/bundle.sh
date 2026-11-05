#!/bin/bash
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
#
# usage: bundle.sh <binary> <web assets folder> <Info.plist> <icon.icns> <output binary path>
# Assembles AnkiShell.app around the output path (.../Contents/MacOS/anki-shell).

set -euo pipefail

bin=$1
web=$2
plist=$3
icon=$4
out=$5

contents=$(dirname "$(dirname "$out")")
rm -rf "$contents"
mkdir -p "$contents/MacOS" "$contents/Resources"
cp "$plist" "$contents/Info.plist"
cp "$icon" "$contents/Resources/AppIcon.icns"
# the web assets are read at runtime from Resources/web
cp -R "$web" "$contents/Resources/web"
cp "$bin" "$out"
# ad-hoc signature, so macOS will run the bundle locally
codesign --force --deep --sign - "$(dirname "$contents")" >/dev/null
