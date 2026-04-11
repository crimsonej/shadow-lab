#!/usr/bin/env bash
# start-dashboard.sh — Launch the Shadow-Lab Control Plane on your local machine
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="$SCRIPT_DIR/dashboard-client"
VENV="$DASHBOARD_DIR/venv"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║      Shadow-Lab Control Plane — Starting     ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "[ERROR] Python 3 is required. Install it from https://python.org"
  exit 1
fi

# Create venv if needed
if [ ! -d "$VENV" ]; then
  echo "[INFO] Creating virtual environment..."
  python3 -m venv "$VENV"
fi

# Install deps
echo "[INFO] Installing/checking dependencies..."
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$DASHBOARD_DIR/requirements.txt" -q

echo ""
echo "  Dashboard: http://localhost:7860"
echo "  Press Ctrl+C to stop"
echo ""

cd "$DASHBOARD_DIR"
"$VENV/bin/python" main.py
