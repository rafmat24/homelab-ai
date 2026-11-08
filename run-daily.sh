#!/bin/bash
# Save as e.g. /opt/ai-orchestrator/run_daily.sh and make executable with chmod +x

cd /opt/ai-orchestrator
# Always run the scraper
./venv/bin/python market_data.py

# Check if today is Friday (day = 5) OR the anomaly flag has been created
WEEK_DAY=$(date +%u)
if [ "$WEEK_DAY" -eq 5 ] || [ -f logs/trigger_anomaly.flag ]; then
    echo "[!] Triggering LLM Agent analysis..."
    ./venv/bin/python compile_weekly_context.py
    ./venv/bin/python agent.py
    
    # Skasuj flagę anomalii (aby uniknąć pętli)
    rm -f logs/trigger_anomaly.flag
fi