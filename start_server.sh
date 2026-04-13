#!/bin/bash
# Start the OpenAlgo trading server
# After starting, open http://127.0.0.1:5000 in your browser and log in with Zerodha

set -e

echo "=========================================="
echo " OpenAlgo Trading Server"
echo "=========================================="
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENALGO_DIR="$SCRIPT_DIR/openalgo"

if [ ! -d "$OPENALGO_DIR" ]; then
    echo "ERROR: openalgo/ directory not found."
    exit 1
fi

# Install OpenAlgo dependencies if needed
echo "Installing dependencies..."
pip install -r "$OPENALGO_DIR/requirements.txt" --quiet

echo ""
echo "Starting OpenAlgo server..."
echo "Open http://127.0.0.1:5000 in your browser to log in."
echo "Press Ctrl+C to stop."
echo ""

cd "$OPENALGO_DIR"
python app.py
