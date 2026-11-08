#!/usr/bin/env bash
# Start The Lifedao BTC Trading Bot on Mac or Linux.
# The first time it installs what the bot needs (a few minutes) and asks for your key.
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo
    echo "Python 3.11 or newer is needed and wasn't found."
    echo "  Mac:   download it from https://www.python.org/downloads/ and run the installer."
    echo "  Linux: install python3 (3.11+) and python3-venv with your package manager."
    echo "Then start this again."
    read -r -p "Press Enter to close." _
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Setting up for the first time. This takes a few minutes..."
    "$PY" -m venv .venv || { echo "Setup failed (on Linux, install python3-venv)."; read -r -p "Press Enter." _; exit 1; }
fi
if ! cmp -s requirements.txt .venv/installed.txt; then
    echo "Installing the bot's components..."
    .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt \
        || { echo "Install failed. Check your internet connection and try again."; read -r -p "Press Enter." _; exit 1; }
    cp requirements.txt .venv/installed.txt
fi

.venv/bin/python start.py "$@"
echo
echo "The bot has stopped."
read -r -p "Press Enter to close." _
