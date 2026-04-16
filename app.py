from flask import Flask, request, jsonify, send_from_directory
import subprocess
import os
import threading
import uuid
import time
import re
import json
import tempfile
import urllib.request
from datetime import datetime, timezone

app = Flask(__name__)

# Config
DEST = os.environ.get("YTDL_DOWNLOAD_DIR", os.path.expanduser("~/Desktop"))
YTDLP = os.environ.get("YTDL_YTDLP_PATH", "/Library/Frameworks/Python.framework/Versions/3.13/bin/yt-dlp")

os.makedirs(DEST, exist_ok=True)

# Track downloads
downloads = {}


# ─── Utilities ───

def seconds_to_timestamp(s):
    s = int(s)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ─── Download Logic ───

def extra_args():
    # ejs:github auto-fetches the JS challenge solver script needed to bypass
    # YouTube's n-challenge — without it, downloads fall back to formats that
    # may be missing or low quality.
    args = ["--remote-components", "ejs:github"]
    cookies_file = os.environ.get("YTDL_COOKIES_FILE", "")
    if cookies_file and os.path.isfile(cookies_file):
        args.extend(["--cookies", cookies_file])
    return args


def run_download_ytdlp(dl_id, url, start_time, end_time):
    result = subprocess.run(
        [YTDLP, "--get-title", "--no-playlist"] + extra_args() + [url],
        capture_output=True, text=True, timeout=30
    )
    title = result.stdout.strip() or "Unknown"
    downloads[dl_id]["title"] = title

    # Detect live stream (for full downloads or clip mode on live)
    is_live = False
    try:
        check = subprocess.run(
            [YTDLP, "--print", "%(is_live)s", "--no-playlist"] + extra_args() + [url],
            capture_output=True, text=True, timeout=30
        )
        is_live = check.stdout.strip().lower() == "true"
    except Exception:
        pass

    cmd = [YTDLP, "--no-playlist", "--restrict-filenames", "--newline", "--progress"] + extra_args()

    if is_live:
        cmd.extend(["-f", "96/95/94/93/92/91"])
        cmd.extend(["--sub-langs", "all,-live_chat"])
    else:
        cmd.extend(["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"])
        cmd.extend(["--merge-output-format", "mp4", "--recode-video", "mp4"])

    if start_time and end_time:
        cmd.extend(["--download-sections", f"*{start_time}-{end_time}"])
        cmd.extend(["--force-keyframes-at-cuts"])
        safe_start = start_time.replace(":", ".")
        safe_end = end_time.replace(":", ".")
        cmd.extend(["-o", os.path.join(DEST, f"%(title)s [{safe_start}-{safe_end}].%(ext)s")])
    else:
        cmd.extend(["-o", os.path.join(DEST, "%(title)s.%(ext)s")])

    cmd.append(url)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output_lines = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            output_lines.append(line)
        if line.startswith("[download]"):
            match = re.match(r"\[download\]\s+(\d+\.?\d*)%", line)
            if match:
                downloads[dl_id]["progress"] = match.group(1)
        if line.startswith("[Merger]") or line.startswith("[VideoConvertor]") or line.startswith("[FixupM3u8]"):
            downloads[dl_id]["progress"] = "converting"

    proc.wait()

    if proc.returncode == 0:
        downloads[dl_id]["status"] = "done"
        for f in sorted(os.listdir(DEST), key=lambda x: os.path.getmtime(os.path.join(DEST, x)), reverse=True):
            if (f.endswith(".mp4") or f.endswith(".ts")) and os.path.getmtime(os.path.join(DEST, f)) > time.time() - 300:
                downloads[dl_id]["filename"] = f
                break
    else:
        error_lines = [l for l in output_lines[-10:] if "ERROR" in l or "error" in l.lower()]
        if not error_lines:
            error_lines = output_lines[-5:]
        error_detail = "; ".join(error_lines) if error_lines else "unknown error"
        raise Exception(f"yt-dlp exited with error: {error_detail}")


def _parse_clock_time(s, reference_day_local):
    """Parse HH:MM or H:MM (24-hour, local time) on the reference day, return aware datetime."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        raise Exception(f"Invalid clock time '{s}' — expected HH:MM (24-hour)")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise Exception(f"Invalid clock time '{s}'")
    local_tz = datetime.now().astimezone().tzinfo
    return reference_day_local.replace(hour=hh, minute=mm, second=0, microsecond=0, tzinfo=local_tz)


def run_download_live_clip(dl_id, url, live_back=None, live_duration=None,
                           live_clock_start=None, live_clock_end=None):
    """Download a clip from a live stream by extracting HLS segments directly.

    Two modes:
      - Relative: live_back (minutes ago) + live_duration (minutes)
      - Absolute: live_clock_start + live_clock_end (HH:MM local time)
    """
    # Get video title
    result = subprocess.run(
        [YTDLP, "--get-title", "--no-playlist"] + extra_args() + [url],
        capture_output=True, text=True, timeout=30
    )
    title = result.stdout.strip() or "Unknown"
    downloads[dl_id]["title"] = title
    downloads[dl_id]["progress"] = "calculating"

    # Get HLS manifest URL for best available quality
    result = subprocess.run(
        [YTDLP, "-f", "96/95/94/93/92/91", "-g", "--no-playlist"] + extra_args() + [url],
        capture_output=True, text=True, timeout=30
    )
    manifest_url = result.stdout.strip()
    if not manifest_url:
        raise Exception("Could not get stream manifest URL")

    # Fetch the full HLS manifest (contains all DVR segments)
    resp = urllib.request.urlopen(manifest_url, timeout=30)
    manifest_data = resp.read().decode()
    lines = manifest_data.strip().split("\n")

    # Parse DVR-window start time from EXT-X-PROGRAM-DATE-TIME.
    # For long-running streams, this is the start of the available DVR window,
    # NOT the start of the stream itself.
    window_start = None
    for line in lines:
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            date_str = line.split(":", 1)[1].strip()
            window_start = datetime.fromisoformat(date_str)
            break
    if window_start is None:
        raise Exception("Could not determine DVR window start from manifest")

    # Parse EXT-X-MEDIA-SEQUENCE: the segment number of the first segment in
    # this manifest. This equals the first sq/N we see, which corresponds to
    # window_start. Non-zero for long streams with a rolling DVR window.
    media_sequence = 0
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":")[1].strip())
            break

    # Parse segment duration from manifest (default 5s)
    seg_duration = 5
    for line in lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            seg_duration = int(line.split(":")[1].strip())
            break

    # Calculate target offsets relative to window start
    if live_clock_start and live_clock_end:
        # Absolute clock-time mode — interpret times on the local day of window_start
        window_start_local = window_start.astimezone()
        clock_from = _parse_clock_time(live_clock_start, window_start_local)
        clock_to = _parse_clock_time(live_clock_end, window_start_local)
        if clock_to <= clock_from:
            raise Exception("End time must be after start time")
        target_start_sec = (clock_from - window_start).total_seconds()
        target_end_sec = (clock_to - window_start).total_seconds()
        if target_start_sec < 0:
            raise Exception(
                f"{live_clock_start} is before the available DVR window "
                f"(starts at {window_start_local.strftime('%H:%M')} local)"
            )
        label = f"{live_clock_start}–{live_clock_end} local"
    else:
        live_back_sec = int(live_back) * 60
        live_dur_sec = int(live_duration) * 60
        now = datetime.now(timezone.utc)
        window_age_sec = (now - window_start).total_seconds()
        target_start_sec = max(0, window_age_sec - live_back_sec)
        target_end_sec = target_start_sec + live_dur_sec
        label = f"{int(live_back)}min ago for {int(live_duration)}min"

    live_dur_sec = target_end_sec - target_start_sec
    # Segment numbers are offset by media_sequence (the sq of the first segment)
    start_sq = media_sequence + int(target_start_sec / seg_duration)
    end_sq = media_sequence + int(target_end_sec / seg_duration)

    # Extract all segment URLs from manifest
    seg_urls = {}
    for line in lines:
        if line.startswith("http") and "/sq/" in line:
            m = re.search(r"/sq/(\d+)/", line)
            if m:
                seg_urls[int(m.group(1))] = line.strip()

    # Collect target segments
    target_segments = [(sq, seg_urls[sq]) for sq in range(start_sq, end_sq + 1) if sq in seg_urls]
    if not target_segments:
        raise Exception(f"No segments found for range sq/{start_sq}-{end_sq}")

    actual_start = seconds_to_timestamp(int(target_start_sec))
    actual_end = seconds_to_timestamp(int(target_end_sec))
    downloads[dl_id]["live_timestamps"] = f"{actual_start} - {actual_end} ({label})"

    # Build custom m3u8 playlist with only the target segments
    playlist_path = os.path.join(tempfile.gettempdir(), f"ytdl_live_{dl_id}.m3u8")
    with open(playlist_path, "w") as f:
        f.write("#EXTM3U\n#EXT-X-VERSION:3\n")
        f.write(f"#EXT-X-TARGETDURATION:{seg_duration}\n")
        f.write(f"#EXT-X-MEDIA-SEQUENCE:{target_segments[0][0]}\n")
        for sq, seg_url in target_segments:
            f.write(f"#EXTINF:{seg_duration}.0,\n{seg_url}\n")
        f.write("#EXT-X-ENDLIST\n")

    # Download with ffmpeg (copy, no re-encode)
    safe_title = re.sub(r"[^\w\s\-.]", "_", title)[:80].strip("_")
    output_path = os.path.join(DEST, f"{safe_title}_live_clip.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-i", playlist_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output_lines = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            output_lines.append(line)
        m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
        if m:
            h, mins, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            elapsed = h * 3600 + mins * 60 + s
            pct = min(99, int(elapsed / live_dur_sec * 100))
            downloads[dl_id]["progress"] = str(pct)

    proc.wait()

    try:
        os.remove(playlist_path)
    except OSError:
        pass

    if proc.returncode == 0:
        downloads[dl_id]["status"] = "done"
        downloads[dl_id]["filename"] = os.path.basename(output_path)
    else:
        error_detail = "; ".join(output_lines[-5:]) if output_lines else "unknown error"
        raise Exception(f"ffmpeg exited with error: {error_detail}")


def run_download(dl_id, url, start_time=None, end_time=None, live_back=None, live_duration=None,
                 live_clock_start=None, live_clock_end=None):
    try:
        downloads[dl_id]["status"] = "downloading"
        if live_clock_start and live_clock_end:
            run_download_live_clip(dl_id, url, live_clock_start=live_clock_start, live_clock_end=live_clock_end)
        elif live_back is not None and live_duration is not None:
            run_download_live_clip(dl_id, url, live_back=live_back, live_duration=live_duration)
        else:
            run_download_ytdlp(dl_id, url, start_time, end_time)
    except Exception as e:
        downloads[dl_id]["status"] = "error"
        downloads[dl_id]["error"] = str(e)


# ─── Routes ───

@app.route("/")
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YT Downloader</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #0f172a;
        color: #e2e8f0;
        min-height: 100vh;
        display: flex;
        flex-direction: column;
        align-items: center;
        padding: 60px 20px;
    }
    h1 { font-size: 28px; margin-bottom: 8px; color: #f8fafc; }
    .subtitle { color: #64748b; margin-bottom: 24px; font-size: 14px; }
    .form-area {
        width: 100%;
        max-width: 700px;
        margin-bottom: 40px;
    }
    .input-row {
        display: flex;
        gap: 10px;
        margin-bottom: 12px;
    }
    input[type="text"], input[type="number"] {
        flex: 1;
        padding: 14px 18px;
        border-radius: 10px;
        border: 1px solid #334155;
        background: #1e293b;
        color: #f1f5f9;
        font-size: 16px;
        outline: none;
        transition: border-color 0.2s;
    }
    input[type="text"]:focus, input[type="number"]:focus { border-color: #3b82f6; }
    input[type="text"]::placeholder, input[type="number"]::placeholder { color: #475569; }
    button {
        padding: 14px 28px;
        border-radius: 10px;
        border: none;
        background: #3b82f6;
        color: white;
        font-size: 16px;
        font-weight: 600;
        cursor: pointer;
        transition: background 0.2s;
        white-space: nowrap;
    }
    button:hover { background: #2563eb; }
    button:disabled { background: #334155; cursor: not-allowed; }
    .mode-toggle {
        display: flex;
        gap: 0;
        margin-bottom: 16px;
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid #334155;
    }
    .mode-toggle button {
        flex: 1;
        padding: 10px 16px;
        border-radius: 0;
        border: none;
        background: #1e293b;
        color: #94a3b8;
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
    }
    .mode-toggle button.active {
        background: #3b82f6;
        color: white;
    }
    .mode-toggle button:hover:not(.active) {
        background: #334155;
    }
    .mode-section {
        display: none;
        margin-bottom: 12px;
    }
    .mode-section.visible { display: block; }
    .time-row {
        display: flex;
        gap: 10px;
        align-items: center;
        margin-bottom: 8px;
    }
    .time-row label {
        color: #94a3b8;
        font-size: 13px;
        min-width: 40px;
    }
    .time-row input[type="text"] {
        width: 120px;
        flex: none;
        padding: 10px 14px;
        font-size: 14px;
        text-align: center;
        font-family: 'SF Mono', 'Menlo', monospace;
    }
    .time-hint {
        color: #475569;
        font-size: 12px;
        margin-left: 8px;
    }
    .live-row {
        display: flex;
        gap: 10px;
        align-items: center;
        margin-bottom: 8px;
    }
    .live-row label {
        color: #94a3b8;
        font-size: 13px;
        white-space: nowrap;
    }
    .live-row input[type="number"] {
        width: 80px;
        flex: none;
        padding: 10px 14px;
        font-size: 14px;
        text-align: center;
        font-family: 'SF Mono', 'Menlo', monospace;
    }
    .live-row .unit {
        color: #64748b;
        font-size: 13px;
    }
    .live-hint {
        color: #475569;
        font-size: 12px;
        margin-top: 4px;
    }
    .downloads { width: 100%; max-width: 700px; }
    .dl-item {
        background: #1e293b;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 12px;
        transition: border-color 0.3s;
    }
    .dl-item.done { border-color: #22c55e; }
    .dl-item.error { border-color: #ef4444; }
    .dl-title { font-weight: 600; font-size: 15px; margin-bottom: 6px; }
    .dl-url { color: #64748b; font-size: 12px; margin-bottom: 4px; word-break: break-all; }
    .dl-clip { color: #f59e0b; font-size: 12px; margin-bottom: 8px; }
    .dl-status { font-size: 13px; }
    .dl-status .label { color: #94a3b8; }
    .dl-status .value { font-weight: 500; }
    .dl-status .value.downloading { color: #3b82f6; }
    .dl-status .value.converting { color: #f59e0b; }
    .dl-status .value.done { color: #22c55e; }
    .dl-status .value.error { color: #ef4444; }
    .progress-bar {
        width: 100%;
        height: 4px;
        background: #334155;
        border-radius: 2px;
        margin-top: 10px;
        overflow: hidden;
    }
    .progress-fill {
        height: 100%;
        background: #3b82f6;
        border-radius: 2px;
        transition: width 0.3s;
    }
    .progress-fill.converting {
        background: #f59e0b;
        width: 100% !important;
        animation: pulse 1.5s ease-in-out infinite;
    }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
    .done-file { color: #94a3b8; font-size: 12px; margin-top: 6px; }
    .dl-download {
        display: inline-block;
        margin-top: 8px;
        padding: 8px 18px;
        background: #22c55e;
        color: #0f172a;
        border-radius: 8px;
        text-decoration: none;
        font-size: 13px;
        font-weight: 600;
        transition: background 0.2s;
    }
    .dl-download:hover { background: #16a34a; }
</style>
</head>
<body>
    <h1>YT Downloader</h1>
    <p class="subtitle">Paste a YouTube URL. Downloads highest quality MP4 to Desktop.</p>

    <div class="form-area">
        <div class="input-row">
            <input type="text" id="url" placeholder="https://www.youtube.com/watch?v=..." autofocus>
            <button id="btn" onclick="startDownload()">Download</button>
        </div>

        <div class="mode-toggle">
            <button class="active" id="modeFullBtn" onclick="setMode('full')">Full Video</button>
            <button id="modeClipBtn" onclick="setMode('clip')">Clip</button>
            <button id="modeLiveBtn" onclick="setMode('live')">Live Clip</button>
        </div>

        <div class="mode-section" id="clipSection">
            <div class="time-row">
                <label for="startTime">Start</label>
                <input type="text" id="startTime" placeholder="0:00:00">
                <label for="endTime">End</label>
                <input type="text" id="endTime" placeholder="0:00:00">
                <span class="time-hint">H:MM:SS or MM:SS</span>
            </div>
        </div>

        <div class="mode-section" id="liveSection">
            <div class="mode-toggle" style="margin-bottom:12px">
                <button class="active" id="liveModeRelBtn" onclick="setLiveMode('rel')">Minutes ago</button>
                <button id="liveModeClockBtn" onclick="setLiveMode('clock')">Clock time</button>
            </div>

            <div id="liveRelSection">
                <div class="live-row">
                    <label for="liveBack">Go back</label>
                    <input type="number" id="liveBack" placeholder="30" min="1">
                    <span class="unit">min</span>
                </div>
                <div class="live-row">
                    <label for="liveDuration">Record for</label>
                    <input type="number" id="liveDuration" placeholder="10" min="1">
                    <span class="unit">min</span>
                </div>
                <p class="live-hint">Calculated from current stream position</p>
            </div>

            <div id="liveClockSection" style="display:none">
                <div class="time-row">
                    <label for="liveClockStart">From</label>
                    <input type="text" id="liveClockStart" placeholder="10:35">
                    <label for="liveClockEnd">To</label>
                    <input type="text" id="liveClockEnd" placeholder="10:42">
                    <span class="time-hint">HH:MM (24-hour, local time)</span>
                </div>
                <p class="live-hint">Exact wall-clock times on the stream's start day</p>
            </div>
        </div>
    </div>

    <div class="downloads" id="downloads"></div>

<script>
const dlList = document.getElementById('downloads');
const urlInput = document.getElementById('url');
const btn = document.getElementById('btn');
let activePolls = {};
let currentMode = 'full';
let currentLiveMode = 'rel';

function setMode(mode) {
    currentMode = mode;
    document.getElementById('modeFullBtn').className = mode === 'full' ? 'active' : '';
    document.getElementById('modeClipBtn').className = mode === 'clip' ? 'active' : '';
    document.getElementById('modeLiveBtn').className = mode === 'live' ? 'active' : '';
    document.getElementById('clipSection').className = 'mode-section' + (mode === 'clip' ? ' visible' : '');
    document.getElementById('liveSection').className = 'mode-section' + (mode === 'live' ? ' visible' : '');
}

function setLiveMode(mode) {
    currentLiveMode = mode;
    document.getElementById('liveModeRelBtn').className = mode === 'rel' ? 'active' : '';
    document.getElementById('liveModeClockBtn').className = mode === 'clock' ? 'active' : '';
    document.getElementById('liveRelSection').style.display = mode === 'rel' ? '' : 'none';
    document.getElementById('liveClockSection').style.display = mode === 'clock' ? '' : 'none';
}

urlInput.addEventListener('keydown', e => { if (e.key === 'Enter') startDownload(); });

function startDownload() {
    const url = urlInput.value.trim();
    if (!url) return;

    const payload = {url};
    let clipLabel = '';

    if (currentMode === 'clip') {
        const st = document.getElementById('startTime').value.trim();
        const et = document.getElementById('endTime').value.trim();
        if (!st || !et) { document.getElementById('startTime').focus(); return; }
        payload.start_time = st;
        payload.end_time = et;
        clipLabel = 'Clip: ' + st + ' - ' + et;
    } else if (currentMode === 'live') {
        if (currentLiveMode === 'clock') {
            const from = document.getElementById('liveClockStart').value.trim();
            const to = document.getElementById('liveClockEnd').value.trim();
            if (!from || !to) { document.getElementById('liveClockStart').focus(); return; }
            payload.live_clock_start = from;
            payload.live_clock_end = to;
            clipLabel = 'Live: ' + from + '–' + to + ' local';
        } else {
            const back = document.getElementById('liveBack').value.trim();
            const dur = document.getElementById('liveDuration').value.trim();
            if (!back || !dur) { document.getElementById('liveBack').focus(); return; }
            payload.live_back = back;
            payload.live_duration = dur;
            clipLabel = 'Live: ' + back + 'min ago, record ' + dur + 'min';
        }
    }

    btn.disabled = true;
    fetch('/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .then(data => {
        btn.disabled = false;
        urlInput.value = '';
        if (currentMode === 'clip') {
            document.getElementById('startTime').value = '';
            document.getElementById('endTime').value = '';
        }
        urlInput.focus();
        addItem(data.id, url, clipLabel);
        pollStatus(data.id);
    })
    .catch(() => { btn.disabled = false; });
}

function addItem(id, url, clipLabel) {
    const div = document.createElement('div');
    div.className = 'dl-item';
    div.id = 'dl-' + id;
    const clipHtml = clipLabel ? `<div class="dl-clip">${clipLabel}</div>` : '';
    div.innerHTML = `
        <div class="dl-title" id="title-${id}">Fetching info...</div>
        <div class="dl-url">${url}</div>
        ${clipHtml}
        <div class="dl-status"><span class="label">Status: </span><span class="value downloading" id="status-${id}">starting...</span></div>
        <div class="progress-bar"><div class="progress-fill" id="prog-${id}" style="width:0%"></div></div>
        <div class="done-file" id="file-${id}"></div>
        <div id="dlbtn-${id}"></div>
    `;
    dlList.prepend(div);
}

function pollStatus(id) {
    activePolls[id] = setInterval(() => {
        fetch('/status/' + id).then(r => r.json()).then(d => {
            const titleEl = document.getElementById('title-' + id);
            const statusEl = document.getElementById('status-' + id);
            const progEl = document.getElementById('prog-' + id);
            const fileEl = document.getElementById('file-' + id);
            const itemEl = document.getElementById('dl-' + id);
            const dlBtn = document.getElementById('dlbtn-' + id);

            if (d.title) titleEl.textContent = d.title;

            if (d.live_timestamps) {
                fileEl.textContent = 'Stream timestamps: ' + d.live_timestamps;
            }

            if (d.status === 'downloading') {
                const pct = d.progress || '0';
                if (pct === 'calculating') {
                    statusEl.textContent = 'Calculating stream position...';
                    statusEl.className = 'value converting';
                    progEl.className = 'progress-fill converting';
                } else if (pct === 'converting') {
                    statusEl.textContent = 'Converting to MP4...';
                    statusEl.className = 'value converting';
                    progEl.className = 'progress-fill converting';
                } else {
                    statusEl.textContent = 'Downloading... ' + pct + '%';
                    statusEl.className = 'value downloading';
                    progEl.style.width = pct + '%';
                }
            } else if (d.status === 'done') {
                statusEl.textContent = 'Complete';
                statusEl.className = 'value done';
                progEl.style.width = '100%';
                progEl.className = 'progress-fill';
                progEl.style.background = '#22c55e';
                itemEl.className = 'dl-item done';
                if (d.filename) {
                    fileEl.textContent = d.filename;
                    dlBtn.innerHTML = '<a class="dl-download" href="/file/' + encodeURIComponent(d.filename) + '" download>Download File</a>';
                }
                clearInterval(activePolls[id]);
            } else if (d.status === 'error') {
                statusEl.textContent = 'Error: ' + (d.error || 'unknown');
                statusEl.className = 'value error';
                itemEl.className = 'dl-item error';
                clearInterval(activePolls[id]);
            }
        });
    }, 1000);
}
</script>
</body>
</html>"""


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    start_time = data.get("start_time", "").strip() or None
    end_time = data.get("end_time", "").strip() or None
    live_back = data.get("live_back") or None
    live_duration = data.get("live_duration") or None
    live_clock_start = (data.get("live_clock_start") or "").strip() or None
    live_clock_end = (data.get("live_clock_end") or "").strip() or None

    dl_id = str(uuid.uuid4())[:8]
    downloads[dl_id] = {
        "status": "queued",
        "title": None,
        "filename": None,
        "error": None,
        "progress": "0",
        "url": url,
        "live_timestamps": None,
    }

    thread = threading.Thread(
        target=run_download,
        args=(dl_id, url, start_time, end_time, live_back, live_duration,
              live_clock_start, live_clock_end),
        daemon=True,
    )
    thread.start()

    return jsonify({"id": dl_id})


@app.route("/status/<dl_id>")
def status(dl_id):
    dl = downloads.get(dl_id)
    if not dl:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dl)


@app.route("/file/<path:filename>")
def serve_file(filename):
    return send_from_directory(DEST, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=80, debug=False)
