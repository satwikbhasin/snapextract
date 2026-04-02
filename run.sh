#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# Install dependencies if not already installed
pip install --quiet --upgrade pip
pip install --quiet beautifulsoup4 pandas requests pyexiftool Pillow lxml

# Run the downloader, passing through all arguments
python "$SCRIPT_DIR/snapchat_memories_downloader.py" "$@"
