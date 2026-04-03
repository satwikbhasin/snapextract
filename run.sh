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

    # Pick input folder or HTML file
    echo "Select your Snapchat export (folder or HTML file)..."
    INPUT=$(osascript \
           -e 'tell application "Finder" to activate' \
           -e 'set f to choose file with prompt "Select your memories HTML file or click Cancel to pick a folder instead" of type {"public.html"}' \
           -e 'return POSIX path of f' 2>/dev/null) || {
        # User cancelled file picker — try folder picker instead
        INPUT=$(osascript -e 'tell application "Finder" to activate' \
               -e 'set f to choose folder with prompt "Select your Snapchat memories folder (or folder of zips)"' \
               -e 'return POSIX path of f' 2>/dev/null) || {
            echo "No input selected. Exiting."
            exit 1
        }
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

# Install system dependencies
install_sys_dep() {
    local cmd="$1"
    local apt_pkg="${2:-$1}"
    if command -v "$cmd" &> /dev/null; then
        return
    fi
    echo "Installing $cmd..."
    if command -v brew &> /dev/null; then
        brew install "$cmd"
    elif command -v apt-get &> /dev/null; then
        sudo apt-get install -y "$apt_pkg"
    elif command -v dnf &> /dev/null; then
        sudo dnf install -y "$cmd"
    elif command -v pacman &> /dev/null; then
        sudo pacman -S --noconfirm "$cmd"
    else
        echo "Warning: Could not install $cmd automatically. Please install it manually."
    fi
}

install_sys_dep exiftool libimage-exiftool-perl
install_sys_dep ffmpeg

# Install Python dependencies
pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

# ─── Run ─────────────────────────────────────────────────────────────────────

python "$SCRIPT_DIR/worker.py" "$@"
