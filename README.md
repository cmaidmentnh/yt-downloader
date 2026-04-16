# YT Downloader

A local web app for downloading YouTube videos and clips. Runs on your machine at `http://ytdl`.

## Features

- Download full videos as MP4
- Clip specific timestamps from any video
- Clip live streams two ways:
  - "Go back X minutes, record Y minutes"
  - Exact wall-clock times (e.g. 10:35–10:42 local)
- Progress tracking with live percentage updates
- Downloads to your Desktop

## Requirements

- Python 3
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [ffmpeg](https://ffmpeg.org/)
- Flask

## Install

### macOS

```bash
brew install ffmpeg
pip3 install yt-dlp flask
git clone https://github.com/cmaidmentnh/yt-downloader.git
cd yt-downloader
sudo ./install.sh
```

### Windows

1. Install [Python 3](https://www.python.org/downloads/), [ffmpeg](https://www.gyan.dev/ffmpeg/builds/), and [yt-dlp](https://github.com/yt-dlp/yt-dlp):
   ```
   pip install yt-dlp flask
   ```
2. Clone the repo:
   ```
   git clone https://github.com/cmaidmentnh/yt-downloader.git
   cd yt-downloader
   ```
3. Right-click `install.bat` → **Run as administrator**

### Linux

```bash
sudo apt install ffmpeg  # or your distro's package manager
pip3 install yt-dlp flask
git clone https://github.com/cmaidmentnh/yt-downloader.git
cd yt-downloader
sudo ./install.sh
```

## Usage

Open **http://ytdl** in your browser.

- **Full Video** — Paste a YouTube URL, click Download
- **Clip** — Switch to Clip mode, enter start/end timestamps (H:MM:SS)
- **Live Clip** — Switch to Live Clip mode, then pick:
  - **Minutes ago** — "go back X min, record Y min" from the live edge
  - **Clock time** — exact HH:MM (24-hour, local time) range within the DVR window

Files are saved to your Desktop.

## Update

### macOS / Linux

```bash
cd yt-downloader
sudo ./update.sh
```

This pulls the latest code and restarts the service.

### Windows

```
cd yt-downloader
git pull
```
Then restart the scheduled task from Task Scheduler (or reboot).

## Uninstall

### macOS
```bash
sudo launchctl bootout system/com.ytdl.app
sudo rm /Library/LaunchDaemons/com.ytdl.app.plist
sudo sed -i '' '/ytdl/d' /etc/hosts
```

### Windows (Run as Administrator)
```
schtasks /delete /tn "YTDownloader" /f
findstr /v "ytdl" %SystemRoot%\System32\drivers\etc\hosts > %TEMP%\hosts.tmp && move /y %TEMP%\hosts.tmp %SystemRoot%\System32\drivers\etc\hosts
```

### Linux
```bash
sudo systemctl disable --now yt-downloader
sudo rm /etc/systemd/system/yt-downloader.service
sudo sed -i '/ytdl/d' /etc/hosts
```
