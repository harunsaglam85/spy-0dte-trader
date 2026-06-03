#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== CloudTrader Startup ==="
echo "Directory: $SCRIPT_DIR"
echo "Time: $(date)"

# Create required directories
mkdir -p logs reports data

# Check .env exists
if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in credentials."
    exit 1
fi

# Install/update dependencies
echo "Installing dependencies..."
pip install -r requirements.txt -q

# Kill existing instance if running
if [ -f /tmp/cloud_trader.pid ]; then
    OLD_PID=$(cat /tmp/cloud_trader.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing instance (PID: $OLD_PID)..."
        kill "$OLD_PID"
        sleep 2
    fi
fi

# Remove old kill file if present
rm -f /tmp/kill_all

# Start main bot in background
echo "Starting CloudTrader..."
nohup python main.py >> logs/main.log 2>&1 &
echo $! > /tmp/cloud_trader.pid

# Start dashboard
echo "Starting Dashboard on port 8080..."
nohup python dashboard.py >> logs/dashboard.log 2>&1 &
echo $! > /tmp/cloud_trader_dashboard.pid

sleep 2

if kill -0 "$(cat /tmp/cloud_trader.pid)" 2>/dev/null; then
    echo "Started successfully. PID: $(cat /tmp/cloud_trader.pid)"
    echo "Logs: tail -f $SCRIPT_DIR/logs/main.log"
else
    echo "ERROR: Process died immediately. Check logs/main.log"
    exit 1
fi
