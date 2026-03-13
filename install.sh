#!/bin/bash
# Install YT Downloader as a local service (macOS/Linux)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill any running instance
pkill -f "python3.*app.py" 2>/dev/null

if [ "$(uname)" = "Darwin" ]; then
    # ─── macOS ───

    # Undo pf changes if they exist
    grep -q 'ytdl' /etc/pf.conf 2>/dev/null && {
        sed -i '' '/ytdl/d' /etc/pf.conf
        rm -f /etc/pf.anchors/ytdl
        pfctl -f /etc/pf.conf 2>/dev/null
    }

    # Remove old LaunchDaemon
    launchctl bootout system/com.ytdl.app 2>/dev/null
    rm -f /Library/LaunchDaemons/com.ytdl.app.plist

    # Add hosts entry if not present
    grep -q 'ytdl' /etc/hosts || echo '127.0.0.1 ytdl' >> /etc/hosts

    # Find python3 path
    PYTHON=$(which python3)

    # Generate plist with correct paths
    cat > /Library/LaunchDaemons/com.ytdl.app.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ytdl.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT_DIR}/app.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/ytdl.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ytdl.log</string>
</dict>
</plist>
EOF

    launchctl bootstrap system /Library/LaunchDaemons/com.ytdl.app.plist

else
    # ─── Linux ───

    # Add hosts entry if not present
    grep -q 'ytdl' /etc/hosts || echo '127.0.0.1 ytdl' >> /etc/hosts

    # Find python3 path
    PYTHON=$(which python3)

    # Create systemd service
    cat > /etc/systemd/system/yt-downloader.service <<EOF
[Unit]
Description=YT Downloader
After=network.target

[Service]
Type=simple
ExecStart=${PYTHON} ${SCRIPT_DIR}/app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable yt-downloader
    systemctl start yt-downloader
fi

echo ""
echo "Done! Go to http://ytdl"
