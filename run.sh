#!/bin/bash
cd /home/ben/Kalshi/ai-bot
while true; do
    venv/bin/python cli.py run --safe-compounder --live
    echo "Sleeping 300s before next cycle..."
    sleep 300
done
