#!/usr/bin/env bash
# Double-click to start The Lifedao BTC Trading Bot on a Mac.
# (The first time, macOS may block it: right-click this file, choose Open, then Open.)
cd "$(dirname "$0")" || exit 1
exec bash ./start.sh "$@"
