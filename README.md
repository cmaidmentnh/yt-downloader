# YT Downloader

A local web app for downloading YouTube videos and clips. Runs on your Mac at `http://ytdl`.

![Screenshot](https://img.shields.io/badge/macOS-only-blue)

## Features

- Download full videos as MP4
- Clip specific timestamps from any video
- Clip live streams by specifying "go back X minutes, record Y minutes"
- Progress tracking with live percentage updates
- Downloads to your Desktop

## Requirements

- macOS
- Python 3
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [ffmpeg](https://ffmpeg.org/)
- Flask

## Install

```bash
# Install dependencies
brew install ffmpeg
pip3 install yt-dlp flask

# Clone the repo
git clone https://github.com/cmaidmentnh/yt-downloader.git
cd yt-downloader

# Install as a background service
sudo ./install.sh
```

This sets up a local web server at **http://ytdl** that runs in the background and starts automatically on login.

## Usage

Open **http://ytdl** in your browser.

- **Full Video** — Paste a YouTube URL, click Download
- **Clip** — Switch to Clip mode, enter start/end timestamps (H:MM:SS)
- **Live Clip** — Switch to Live Clip mode, enter how many minutes back and how long to record

Files are saved to your Desktop.

## Uninstall

```bash
sudo launchctl bootout system/com.ytdl.app
sudo rm /Library/LaunchDaemons/com.ytdl.app.plist
sudo sed -i '' '/ytdl/d' /etc/hosts
```
