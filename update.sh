#!/bin/bash
# Update YT Downloader: pull latest code and restart the service
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

git pull --ff-only

if [ "$(uname)" = "Darwin" ]; then
    sudo launchctl kickstart -k system/com.ytdl.app
else
    sudo systemctl restart yt-downloader
fi

echo "Updated. Go to http://ytdl"
