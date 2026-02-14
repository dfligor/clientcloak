#!/usr/bin/env bash
# Launch ClientCloak web UI
set -e

cd "$(dirname "$0")/.."
source venv/bin/activate

PORT="${1:-8000}"

# Kill existing instance on that port if running
PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$PID" ]; then
    echo "Stopping existing process on port $PORT (PID $PID)..."
    kill "$PID" 2>/dev/null
    sleep 1
fi

echo "Starting ClientCloak on http://127.0.0.1:$PORT"
clientcloak-ui --port "$PORT"
