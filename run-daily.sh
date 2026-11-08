#!/bin/bash
# Runs from this script's directory by default; set HOMELAB_AI_DIR to override.

CODE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_BASE_DIR="$CODE_DIR"
if [ "$(basename "$CODE_DIR")" = "code" ]; then
    DEFAULT_BASE_DIR="$(dirname "$CODE_DIR")"
fi
BASE_DIR="${HOMELAB_AI_DIR:-$DEFAULT_BASE_DIR}"
BASE_DIR="$(cd -- "$BASE_DIR" && pwd)" || exit 1
export HOMELAB_AI_DIR="$BASE_DIR"

cd -- "$BASE_DIR" || exit 1
# Always run the scraper
"$BASE_DIR/venv/bin/python" "$CODE_DIR/market_data.py"

# Check if today is Friday (day = 5) OR the anomaly flag has been created
WEEK_DAY=$(date +%u)
if [ "$WEEK_DAY" -eq 5 ] || [ -f logs/trigger_anomaly.flag ]; then
    echo "[!] Triggering LLM Agent analysis..."
    "$BASE_DIR/venv/bin/python" "$CODE_DIR/compile_weekly_context.py"
    "$BASE_DIR/venv/bin/python" "$CODE_DIR/agent.py"
    
    # Skasuj flagę anomalii (aby uniknąć pętli)
    rm -f logs/trigger_anomaly.flag
fi