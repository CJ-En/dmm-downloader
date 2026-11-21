#!/bin/sh
# Starts dmm-downloader. The first run creates a private Python environment (.venv) next to this
# file and installs what it needs there; later runs reuse it.
set -e
here=$(cd "$(dirname "$0")" && pwd)
venv="$here/.venv"
if [ ! -x "$venv/bin/python" ] || ! cmp -s "$here/requirements.txt" "$venv/requirements.installed"; then
    echo "Preparing the Python environment (first run or after an update)..."
    python3 -m venv "$venv" || { echo "Could not create it. On Debian/Ubuntu: sudo apt install python3-venv"; exit 2; }
    "$venv/bin/python" -m pip install --quiet --upgrade pip
    "$venv/bin/python" -m pip install --quiet -r "$here/requirements.txt"
    cp "$here/requirements.txt" "$venv/requirements.installed"
fi
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec "$venv/bin/python" -m dmm_downloader "$@"
