#!/bin/bash
# Open and close a benchmark window on the beta host.
#
# The Mac serves a private beta. A 20 GB model loaded beside it competes for
# memory with the thing users are actually using, and a benchmark measured while
# that is happening measures the contention rather than the model. So the window
# is explicit: open it, run, close it, and the machine is back to normal.
#
#   scripts/bench_window.sh open     load Qwen, pause the beta app
#   scripts/bench_window.sh status   what is loaded and what is serving
#   scripts/bench_window.sh close    unload Qwen, bring the beta back
#
# `close` is safe to run twice and safe to run after a crash. The tunnel is left
# alone throughout: with the app stopped it answers 502, which is a truthful
# "briefly unavailable" rather than a hostname that has vanished.
set -uo pipefail

LMS="${LMS:-$HOME/.lmstudio/bin/lms}"
MODEL="${BENCH_MODEL:-qwen3.6-35b-a3b-mlx}"
AGENT="gui/$(id -u)/com.resell.app"

free_gb() {
  vm_stat | awk '/Pages free/{f=$3} /Pages inactive/{i=$3}
    END{gsub(/\./,"",f); gsub(/\./,"",i); printf "%.1f", (f+i)*16384/1073741824}'
}

case "${1:-status}" in
  open)
    echo "free memory before: $(free_gb) GB"
    echo "pausing the beta app so the benchmark is not measuring contention…"
    launchctl kill SIGTERM "$AGENT" 2>/dev/null
    launchctl stop com.resell.app 2>/dev/null
    sleep 2
    echo "starting the LM Studio server…"
    "$LMS" server start >/dev/null 2>&1
    echo "loading $MODEL (about 20 GB)…"
    "$LMS" load "$MODEL" --yes || { echo "load failed"; exit 1; }
    "$LMS" ps
    echo "free memory after: $(free_gb) GB"
    echo
    echo "window open. run the benchmark, then: scripts/bench_window.sh close"
    ;;

  close)
    echo "unloading local models…"
    "$LMS" unload --all 2>/dev/null
    "$LMS" server stop >/dev/null 2>&1
    echo "restarting the beta app…"
    launchctl start com.resell.app 2>/dev/null
    sleep 3
    launchctl list | grep com.resell || true
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 https://sell.knocknock.info/ || echo "000")
    echo "public site -> $code (302 = Access login, healthy)"
    echo "free memory: $(free_gb) GB"
    ;;

  status)
    echo "loaded models:"; "$LMS" ps 2>&1 | sed 's/^/  /'
    echo "beta services:"; launchctl list | grep com.resell | sed 's/^/  /' || echo "  none"
    echo "free memory: $(free_gb) GB"
    ;;

  *)
    echo "usage: $0 {open|close|status}"; exit 2 ;;
esac
