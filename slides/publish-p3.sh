#!/bin/zsh
set -euo pipefail

SRC_DIR="/Users/starfish/Documents/test1/detademo/autodatareport/slides"
DST_DIR="/Users/starfish/.local/share/p3-site"

mkdir -p "$DST_DIR"
cp "$SRC_DIR/ai-workflow-defense-v9.html" "$DST_DIR/index.html"
cp "$SRC_DIR/gui-screenshot-2026-03-17-001159.png" "$DST_DIR/"
cp "$SRC_DIR/final-delivery-result.png" "$DST_DIR/"

echo "Updated p3 site at $DST_DIR"
echo "Live URL: https://p3.haixing.uk"
