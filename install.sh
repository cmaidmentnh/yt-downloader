#!/bin/bash
# Install YT Downloader as a local service

# Kill any running instance
pkill -f "python3.*app.py" 2>/dev/null

# Undo pf changes if they exist
grep -q 'ytdl' /etc/pf.conf 2>/dev/null && {
    sed -i '' '/ytdl/d' /etc/pf.conf
    rm -f /etc/pf.anchors/ytdl
    pfctl -f /etc/pf.conf 2>/dev/null
}

# Remove old LaunchDaemon
launchctl bootout system/com.ytdl.app 2>/dev/null
rm -f /Library/LaunchDaemons/com.ytdl.app.plist

# Remove old hosts entries and add new one
sed -i '' '/ytdl/d' /etc/hosts
echo '127.0.0.1 ytdl' >> /etc/hosts

# Install LaunchDaemon
cp /Users/chrismaidment/yt-downloader/com.ytdl.app.plist /Library/LaunchDaemons/
launchctl bootstrap system /Library/LaunchDaemons/com.ytdl.app.plist

echo ""
echo "Done! Go to http://ytdl"
