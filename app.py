from flask import Flask, request, jsonify, send_from_directory
import subprocess
import os
import threading
import uuid
import time
import re
import json

app = Flask(__name__)

# Config — use env vars for server, fallback for local
DEST = os.environ.get("YTDL_DOWNLOAD_DIR", os.path.expanduser("~/Desktop"))
YTDLP = os.environ.get("YTDL_YTDLP_PATH", "yt-dlp")

os.makedirs(DEST, exist_ok=True)

# Track downloads: {id: {status, title, filename, error, progress, url}}
downloads = {}

# Rate limiting: max 2 downloads per IP per hour
RATE_LIMIT = 2
RATE_WINDOW = 3600  # 1 hour in seconds
ip_requests = {}  # {ip: [timestamp, ...]}


def check_rate_limit(ip):
    now = time.time()
    if ip not in ip_requests:
        ip_requests[ip] = []
    # Remove old entries outside the window
    ip_requests[ip] = [t for t in ip_requests[ip] if now - t < RATE_WINDOW]
    if len(ip_requests[ip]) >= RATE_LIMIT:
        return False
    ip_requests[ip].append(now)
    return True


def get_client_ip():
    # Behind Cloudflare/nginx — check forwarded headers
    return request.headers.get("CF-Connecting-IP") or \
           request.headers.get("X-Real-IP") or \
           request.remote_addr


def seconds_to_timestamp(s):
    s = int(s)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h}:{m:02d}:{sec:02d}"


def run_download(dl_id, url, start_time=None, end_time=None, live_back=None, live_duration=None):
    try:
        downloads[dl_id]["status"] = "downloading"

        # Get title first
        result = subprocess.run(
            [YTDLP, "--get-title", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        title = result.stdout.strip() or "Unknown"
        downloads[dl_id]["title"] = title

        # For live stream clips, calculate timestamps from current stream position
        if live_back is not None and live_duration is not None:
            downloads[dl_id]["progress"] = "calculating"

            info_result = subprocess.run(
                [YTDLP, "-j", "--no-playlist", url],
                capture_output=True, text=True, timeout=60
            )
            try:
                info = json.loads(info_result.stdout)
            except (json.JSONDecodeError, TypeError):
                downloads[dl_id]["status"] = "error"
                downloads[dl_id]["error"] = "Could not get stream info"
                return

            current_duration = 0

            release_ts = info.get("release_timestamp")
            if release_ts and release_ts > 0:
                current_duration = int(time.time() - release_ts)

            if current_duration <= 0:
                dur = info.get("duration")
                if dur and dur > 0:
                    current_duration = int(dur)

            if current_duration <= 0:
                for fmt in info.get("formats", []):
                    fdur = fmt.get("duration")
                    if fdur and fdur > 0:
                        current_duration = max(current_duration, int(fdur))

            if current_duration <= 0:
                stream_url = info.get("url") or info.get("manifest_url")
                if stream_url:
                    probe = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", stream_url],
                        capture_output=True, text=True, timeout=30
                    )
                    try:
                        current_duration = int(float(probe.stdout.strip()))
                    except (ValueError, TypeError):
                        pass

            if current_duration <= 0:
                downloads[dl_id]["status"] = "error"
                downloads[dl_id]["error"] = "Could not determine how long stream has been live"
                return

            live_back_sec = int(live_back) * 60
            live_dur_sec = int(live_duration) * 60

            clip_start = current_duration - live_back_sec
            if clip_start < 0:
                clip_start = 0
            clip_end = clip_start + live_dur_sec
            if clip_end > current_duration:
                clip_end = current_duration

            start_time = seconds_to_timestamp(clip_start)
            end_time = seconds_to_timestamp(clip_end)
            downloads[dl_id]["live_timestamps"] = f"{start_time} - {end_time}"

        # Detect if this is a live stream
        is_live = False
        if live_back is not None:
            is_live = True
        else:
            try:
                check = subprocess.run(
                    [YTDLP, "--print", "%(is_live)s", "--no-playlist", url],
                    capture_output=True, text=True, timeout=30
                )
                is_live = check.stdout.strip().lower() == "true"
            except Exception:
                pass

        # Build command — live streams use HLS formats, regular videos use mp4
        cmd = [YTDLP, "--no-playlist", "--restrict-filenames", "--newline", "--progress"]

        if is_live:
            cmd.extend(["-f", "301/300/94/93/92/91"])
            cmd.extend(["--sub-langs", "all,-live_chat"])
        else:
            cmd.extend(["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"])
            cmd.extend(["--merge-output-format", "mp4", "--recode-video", "mp4"])

        # Add section download if timestamps provided
        if start_time and end_time:
            cmd.extend(["--download-sections", f"*{start_time}-{end_time}"])
            cmd.extend(["--force-keyframes-at-cuts"])
            safe_start = start_time.replace(":", ".")
            safe_end = end_time.replace(":", ".")
            cmd.extend(["-o", os.path.join(DEST, f"%(title)s [{safe_start}-{safe_end}].%(ext)s")])
        else:
            cmd.extend(["-o", os.path.join(DEST, "%(title)s.%(ext)s")])

        cmd.append(url)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

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
            downloads[dl_id]["status"] = "error"
            # Include the last few lines of yt-dlp output for debugging
            error_lines = [l for l in output_lines[-10:] if "ERROR" in l or "error" in l.lower()]
            if not error_lines:
                error_lines = output_lines[-5:]
            error_detail = "; ".join(error_lines) if error_lines else "unknown error"
            downloads[dl_id]["error"] = f"yt-dlp exited with error: {error_detail}"

    except Exception as e:
        downloads[dl_id]["status"] = "error"
        downloads[dl_id]["error"] = str(e)


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
    .subtitle { color: #64748b; margin-bottom: 40px; font-size: 14px; }
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
    <p class="subtitle">Paste a YouTube URL. Downloads highest quality MP4.</p>

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
            <p class="live-hint">Calculates timestamps from current stream position</p>
        </div>
    </div>

    <div class="downloads" id="downloads"></div>

<script>
const dlList = document.getElementById('downloads');
const urlInput = document.getElementById('url');
const btn = document.getElementById('btn');
let activePolls = {};
let currentMode = 'full';

function setMode(mode) {
    currentMode = mode;
    document.getElementById('modeFullBtn').className = mode === 'full' ? 'active' : '';
    document.getElementById('modeClipBtn').className = mode === 'clip' ? 'active' : '';
    document.getElementById('modeLiveBtn').className = mode === 'live' ? 'active' : '';
    document.getElementById('clipSection').className = 'mode-section' + (mode === 'clip' ? ' visible' : '');
    document.getElementById('liveSection').className = 'mode-section' + (mode === 'live' ? ' visible' : '');
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
        const back = document.getElementById('liveBack').value.trim();
        const dur = document.getElementById('liveDuration').value.trim();
        if (!back || !dur) { document.getElementById('liveBack').focus(); return; }
        payload.live_back = back;
        payload.live_duration = dur;
        clipLabel = 'Live: ' + back + 'min ago, record ' + dur + 'min';
    }

    btn.disabled = true;
    fetch('/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    })
    .then(r => {
        if (r.status === 429) {
            r.json().then(d => alert(d.error || 'Rate limit exceeded'));
            btn.disabled = false;
            return;
        }
        return r.json();
    })
    .then(data => {
        if (!data) return;
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
    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({"error": "Rate limit exceeded. Max 2 downloads per hour."}), 429

    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    start_time = data.get("start_time", "").strip() or None
    end_time = data.get("end_time", "").strip() or None
    live_back = data.get("live_back") or None
    live_duration = data.get("live_duration") or None

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
        args=(dl_id, url, start_time, end_time, live_back, live_duration),
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
    app.run(host="127.0.0.1", port=8855, debug=False)
