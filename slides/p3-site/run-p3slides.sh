#!/bin/zsh
cd /Users/starfish/Documents/test1/detademo/autodatareport/slides/p3-site || exit 1
exec /usr/bin/python3 -m http.server 8766 --bind 127.0.0.1
