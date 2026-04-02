#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ─── Interactive mode (no args) ──────────────────────────────────────────────

if [ $# -eq 0 ]; then
    echo ""
    echo "╔══════════════════════════════════════╗"
    echo "║        SnapExtract                   ║"
    echo "║  Snapchat Memories Extractor         ║"
    echo "╚══════════════════════════════════════╝"
    echo ""

    # Pick input folder/file
    echo "Select your Snapchat export (folder or zip)..."
    INPUT=$(osascript -e 'tell application "Finder" to activate' \
           -e 'set f to choose folder with prompt "Select your Snapchat memories folder (or folder of zips)"' \
           -e 'return POSIX path of f' 2>/dev/null) || {
        echo "No folder selected. Exiting."
        exit 1
    }
    echo "  Input: $INPUT"

    # Pick output folder
    echo "Select where to save the results..."
    OUTPUT=$(osascript -e 'tell application "Finder" to activate' \
            -e 'set f to choose folder with prompt "Select the output folder for your memories"' \
            -e 'return POSIX path of f' 2>/dev/null) || {
        echo "No output folder selected. Exiting."
        exit 1
    }
    echo "  Output: $OUTPUT"
    echo ""

    set -- -i "$INPUT" -d "$OUTPUT"
fi

# ─── Install dependencies ───────────────────────────────────────────────────

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# Install system dependencies via Homebrew if missing
if command -v brew &> /dev/null; then
    if ! command -v exiftool &> /dev/null; then
        echo "Installing exiftool..."
        brew install exiftool
    fi
    if ! command -v ffmpeg &> /dev/null; then
        echo "Installing ffmpeg..."
        brew install ffmpeg
    fi
else
    echo "Warning: Homebrew not found. Please install exiftool and ffmpeg manually."
fi

# Install Python dependencies
pip install --quiet --upgrade pip
pip install --quiet beautifulsoup4 pyexiftool Pillow

# ─── Run ─────────────────────────────────────────────────────────────────────

python "$SCRIPT_DIR/snapchat_memories_downloader.py" "$@"
