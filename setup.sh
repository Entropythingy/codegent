#!/usr/bin/env bash
set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
if command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm python python-pip wget git cmake base-devel curl || true
elif command -v apt >/dev/null; then
    sudo apt update && sudo apt install -y python3 python3-pip wget git cmake build-essential curl
elif command -v dnf >/dev/null; then
    sudo dnf install -y python3 python3-pip wget git cmake gcc-c++ make curl
fi
pip install --user --break-system-packages -r "$REPO_DIR/requirements.txt" 2>/dev/null || pip install --user -r "$REPO_DIR/requirements.txt"
mkdir -p ~/.config/codegent ~/.local/bin
[ ! -f ~/.config/codegent/config.toml ] && cp "$REPO_DIR/config.example.toml" ~/.config/codegent/config.toml
ln -sf "$REPO_DIR/codegent.py" ~/.local/bin/codegent
chmod +x ~/.local/bin/codegent
echo "✅ Setup complete! Run: codegent"
