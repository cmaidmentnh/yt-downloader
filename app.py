from flask import Flask, request, jsonify, send_from_directory, redirect, session
import subprocess
import os
import threading
import uuid
import time
import re
import json
import secrets
import sys
import requests
from urllib.parse import urlencode

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# Config
DEST = os.environ.get("YTDL_DOWNLOAD_DIR", os.path.expanduser("~/Desktop"))
YTDLP = os.environ.get("YTDL_YTDLP_PATH", "yt-dlp")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
APP_URL = os.environ.get("APP_URL", "https://ytdl.nhhouse.gop")

os.makedirs(DEST, exist_ok=True)

# Track downloads
downloads = {}

# Rate limiting
RATE_LIMIT = 2
RATE_WINDOW = 3600
ip_requests = {}

# User sessions: {session_id: {email, name, picture, access_token, refresh_token, token_expiry}}
user_sessions = {}


# ─── Rate Limiting ───

def check_rate_limit(ip):
    now = time.time()
    if ip not in ip_requests:
        ip_requests[ip] = []
    ip_requests[ip] = [t for t in ip_requests[ip] if now - t < RATE_WINDOW]
    if len(ip_requests[ip]) >= RATE_LIMIT:
        return False
    ip_requests[ip].append(now)
    return True


def get_client_ip():
    return request.headers.get("CF-Connecting-IP") or \
           request.headers.get("X-Real-IP") or \
           request.remote_addr


# ─── Utilities ───

def seconds_to_timestamp(s):
    s = int(s)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def extract_video_id(url):
    m = re.search(r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None


def safe_filename(title):
    name = re.sub(r'[^\w\s\-\(\)]', '', title).strip()
    return re.sub(r'\s+', '_', name)


# ─── Google OAuth ───

def get_google_auth_url(state):
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{APP_URL}/auth/callback",
        "scope": "openid email profile https://www.googleapis.com/auth/youtube.readonly",
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def exchange_code_for_tokens(code):
    return requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": f"{APP_URL}/auth/callback",
        "grant_type": "authorization_code",
    }).json()


def do_refresh_token(refresh_tok):
    return requests.post("https://oauth2.googleapis.com/token", data={
        "refresh_token": refresh_tok,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }).json()


def get_user_info(token):
    return requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {token}"}).json()


def get_valid_token(sid):
    s = user_sessions.get(sid)
    if not s:
        return None
    if time.time() >= s.get("token_expiry", 0) - 60:
        rt = s.get("refresh_token")
        if rt:
            t = do_refresh_token(rt)
            if "access_token" in t:
                s["access_token"] = t["access_token"]
                s["token_expiry"] = time.time() + t.get("expires_in", 3600)
            else:
                return None
        else:
            return None
    return s.get("access_token")


# ─── YouTube InnerTube API ───

def innertube_player(video_id, access_token):
    # Try multiple client types — some work better with OAuth from datacenter IPs
    clients = [
        {"clientName": "ANDROID", "clientVersion": "19.29.37", "androidSdkVersion": 34, "platform": "MOBILE"},
        {"clientName": "IOS", "clientVersion": "19.29.1", "deviceMake": "Apple", "deviceModel": "iPhone16,2", "platform": "MOBILE"},
        {"clientName": "WEB", "clientVersion": "2.20250101.00.00", "platform": "DESKTOP"},
    ]
    last_result = {}
    for client in clients:
        resp = requests.post(
            "https://www.youtube.com/youtubei/v1/player",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "videoId": video_id,
                "context": {"client": client},
            },
            timeout=30,
        )
        result = resp.json()
        ps = result.get("playabilityStatus", {})
        sd = result.get("streamingData", {})
        print(f"  InnerTube {client['clientName']}: status={ps.get('status')}, reason={ps.get('reason','')[:60]}, formats={len(sd.get('adaptiveFormats', []))}, hls={bool(sd.get('hlsManifestUrl'))}, keys={list(result.keys())[:8]}", flush=True)
        if ps.get("status") == "OK" and (sd.get("adaptiveFormats") or sd.get("hlsManifestUrl")):
            return result
        last_result = result
    return last_result


def pick_formats(info):
    streaming = info.get("streamingData", {})
    adaptive = streaming.get("adaptiveFormats", [])

    vids = [f for f in adaptive if f.get("mimeType", "").startswith("video/") and f.get("url")]
    auds = [f for f in adaptive if f.get("mimeType", "").startswith("audio/") and f.get("url")]

    # Prefer mp4/h264
    mp4 = [f for f in vids if "mp4" in f.get("mimeType", "")]
    if mp4:
        vids = mp4
    m4a = [f for f in auds if "mp4a" in f.get("mimeType", "")]
    if m4a:
        auds = m4a

    best_v = max(vids, key=lambda f: f.get("height", 0), default=None) if vids else None
    best_a = max(auds, key=lambda f: f.get("bitrate", 0), default=None) if auds else None
    return best_v, best_a


# ─── Download Logic ───

def run_download_oauth(dl_id, url, start_time, end_time, live_back, live_duration, access_token):
    video_id = extract_video_id(url)
    if not video_id:
        raise Exception("Could not extract video ID from URL")

    info = innertube_player(video_id, access_token)

    ps = info.get("playabilityStatus", {})
    if ps.get("status") not in ("OK", "LIVE_STREAM_OFFLINE"):
        raise Exception(ps.get("reason") or ps.get("status", "Video unavailable"))

    vd = info.get("videoDetails", {})
    title = vd.get("title", "Unknown")
    downloads[dl_id]["title"] = title
    is_live = vd.get("isLive", False) or vd.get("isLiveContent", False)

    # Handle live clip timestamps
    if live_back is not None and live_duration is not None:
        downloads[dl_id]["progress"] = "calculating"
        duration = int(vd.get("lengthSeconds", 0))
        if duration <= 0:
            raise Exception("Could not determine stream duration")
        live_back_sec = int(live_back) * 60
        live_dur_sec = int(live_duration) * 60
        clip_start = max(0, duration - live_back_sec)
        clip_end = min(clip_start + live_dur_sec, duration)
        start_time = seconds_to_timestamp(clip_start)
        end_time = seconds_to_timestamp(clip_end)
        downloads[dl_id]["live_timestamps"] = f"{start_time} - {end_time}"

    # Get stream URLs
    streaming = info.get("streamingData", {})
    hls_url = streaming.get("hlsManifestUrl")
    best_v, best_a = pick_formats(info)

    if not best_v and not hls_url:
        raise Exception("No downloadable format found (may need different auth scope)")

    # Build filename
    fname = safe_filename(title)
    if start_time and end_time:
        safe_start = start_time.replace(":", ".")
        safe_end = end_time.replace(":", ".")
        filename = f"{fname} [{safe_start}-{safe_end}].mp4"
    else:
        filename = f"{fname}.mp4"

    output_path = os.path.join(DEST, filename)
    downloads[dl_id]["progress"] = "downloading"

    # Build ffmpeg command
    cmd = ["ffmpeg", "-y"]

    if hls_url and (is_live or not best_v):
        # HLS stream (live or fallback)
        if start_time and end_time:
            cmd.extend(["-ss", start_time, "-to", end_time])
        cmd.extend(["-i", hls_url, "-c", "copy"])
    else:
        # Adaptive formats — separate video + audio
        if start_time and end_time:
            cmd.extend(["-ss", start_time, "-to", end_time])
        cmd.extend(["-i", best_v["url"]])

        if best_a:
            if start_time and end_time:
                cmd.extend(["-ss", start_time, "-to", end_time])
            cmd.extend(["-i", best_a["url"]])

        cmd.extend(["-c", "copy", "-movflags", "+faststart"])

    cmd.append(output_path)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        line = line.strip()
        if "time=" in line:
            downloads[dl_id]["progress"] = "converting"
    proc.wait()

    if proc.returncode == 0 and os.path.exists(output_path):
        downloads[dl_id]["status"] = "done"
        downloads[dl_id]["filename"] = filename
    else:
        raise Exception("ffmpeg processing failed")


def extra_args():
    args = ["--js-runtimes", "node"]
    cookies_file = os.environ.get("YTDL_COOKIES_FILE", "")
    if cookies_file and os.path.isfile(cookies_file):
        args.extend(["--cookies", cookies_file])
    return args


def run_download_ytdlp(dl_id, url, start_time, end_time, live_back, live_duration):
    """Fallback: download using yt-dlp (may fail without auth on datacenter IPs)."""
    result = subprocess.run(
        [YTDLP, "--get-title", "--no-playlist"] + extra_args() + [url],
        capture_output=True, text=True, timeout=30
    )
    title = result.stdout.strip() or "Unknown"
    downloads[dl_id]["title"] = title

    # For live stream clips, calculate timestamps
    if live_back is not None and live_duration is not None:
        downloads[dl_id]["progress"] = "calculating"
        info_result = subprocess.run(
            [YTDLP, "-j", "--no-playlist"] + extra_args() + [url],
            capture_output=True, text=True, timeout=60
        )
        try:
            info = json.loads(info_result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise Exception("Could not get stream info")

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
            raise Exception("Could not determine how long stream has been live")

        live_back_sec = int(live_back) * 60
        live_dur_sec = int(live_duration) * 60
        clip_start = max(0, current_duration - live_back_sec)
        clip_end = min(clip_start + live_dur_sec, current_duration)
        start_time = seconds_to_timestamp(clip_start)
        end_time = seconds_to_timestamp(clip_end)
        downloads[dl_id]["live_timestamps"] = f"{start_time} - {end_time}"

    # Detect live stream
    is_live = live_back is not None
    if not is_live:
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
        cmd.extend(["-f", "301/300/94/93/92/91"])
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


def run_download(dl_id, url, start_time=None, end_time=None, live_back=None, live_duration=None, access_token=None):
    try:
        downloads[dl_id]["status"] = "downloading"

        # Try OAuth + InnerTube + ffmpeg first
        if access_token:
            try:
                print(f"[{dl_id}] Using OAuth path (token: {access_token[:20]}...)", flush=True)
                run_download_oauth(dl_id, url, start_time, end_time, live_back, live_duration, access_token)
                return
            except Exception as e:
                print(f"[{dl_id}] OAuth path failed: {e}", flush=True)
                # Reset state and fall through to yt-dlp
                downloads[dl_id]["status"] = "downloading"
                downloads[dl_id]["error"] = None
        else:
            print(f"[{dl_id}] No OAuth token, using yt-dlp directly", flush=True)

        # Fallback: yt-dlp
        run_download_ytdlp(dl_id, url, start_time, end_time, live_back, live_duration)

    except Exception as e:
        downloads[dl_id]["status"] = "error"
        downloads[dl_id]["error"] = str(e)


# ─── Auth Routes ───

@app.route("/auth/google")
def auth_google():
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Google OAuth not configured"}), 500
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    return redirect(get_google_auth_url(state))


@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error or not code or state != session.get("oauth_state"):
        return redirect("/")

    tokens = exchange_code_for_tokens(code)
    if "error" in tokens:
        return redirect("/")

    access_token = tokens["access_token"]
    user_info = get_user_info(access_token)

    sid = session.get("ytdl_session") or secrets.token_hex(16)
    session["ytdl_session"] = sid

    user_sessions[sid] = {
        "email": user_info.get("email", ""),
        "name": user_info.get("name", ""),
        "picture": user_info.get("picture", ""),
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token", ""),
        "token_expiry": time.time() + tokens.get("expires_in", 3600),
    }

    return redirect("/")


@app.route("/auth/status")
def auth_status():
    sid = session.get("ytdl_session")
    if sid and sid in user_sessions:
        u = user_sessions[sid]
        has_token = get_valid_token(sid) is not None
        return jsonify({
            "signed_in": True,
            "active": has_token,
            "email": u["email"],
            "name": u["name"],
            "picture": u.get("picture", ""),
        })
    return jsonify({"signed_in": False, "oauth_configured": bool(GOOGLE_CLIENT_ID)})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    sid = session.get("ytdl_session")
    if sid and sid in user_sessions:
        del user_sessions[sid]
    session.pop("ytdl_session", None)
    session.pop("oauth_state", None)
    return jsonify({"ok": True})


# ─── Main Routes ───

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
    .auth-area {
        width: 100%;
        max-width: 700px;
        margin-bottom: 24px;
        background: #1e293b;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 14px 20px;
        display: flex;
        align-items: center;
        gap: 12px;
        min-height: 52px;
    }
    .auth-area.no-oauth { display: none; }
    .auth-msg {
        color: #94a3b8;
        font-size: 13px;
        flex: 1;
    }
    .google-btn {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 8px 16px;
        background: #4285f4;
        color: white;
        border: none;
        border-radius: 6px;
        font-size: 13px;
        font-weight: 500;
        text-decoration: none;
        cursor: pointer;
        white-space: nowrap;
    }
    .google-btn:hover { background: #3367d6; }
    .google-btn svg { flex-shrink: 0; }
    .auth-user {
        display: flex;
        align-items: center;
        gap: 10px;
        flex: 1;
    }
    .auth-avatar {
        width: 28px;
        height: 28px;
        border-radius: 50%;
    }
    .auth-name {
        color: #e2e8f0;
        font-size: 13px;
        font-weight: 500;
    }
    .auth-email {
        color: #64748b;
        font-size: 12px;
    }
    .sign-out-btn {
        padding: 6px 12px;
        background: transparent;
        border: 1px solid #475569;
        color: #94a3b8;
        border-radius: 6px;
        font-size: 12px;
        cursor: pointer;
    }
    .sign-out-btn:hover { border-color: #94a3b8; color: #e2e8f0; }
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

    <div class="auth-area" id="authArea">
        <div id="authLoading"><span class="auth-msg">Checking sign-in...</span></div>
        <div id="authSignedOut" style="display:none">
            <span class="auth-msg">Sign in with Google to enable downloads</span>
            <a href="/auth/google" class="google-btn">
                <svg width="18" height="18" viewBox="0 0 24 24"><path fill="#fff" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#fff" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#fff" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#fff" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
                Sign in with Google
            </a>
        </div>
        <div id="authSignedIn" style="display:none">
            <div class="auth-user">
                <img id="authPic" class="auth-avatar" src="" alt="">
                <div>
                    <div class="auth-name" id="authName"></div>
                    <div class="auth-email" id="authEmail"></div>
                </div>
            </div>
            <button class="sign-out-btn" onclick="signOut()">Sign out</button>
        </div>
    </div>

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

// Auth
fetch('/auth/status').then(r => r.json()).then(d => {
    document.getElementById('authLoading').style.display = 'none';
    if (d.signed_in) {
        document.getElementById('authSignedIn').style.display = 'flex';
        document.getElementById('authName').textContent = d.name || d.email;
        document.getElementById('authEmail').textContent = d.email;
        if (d.picture) document.getElementById('authPic').src = d.picture;
    } else if (d.oauth_configured) {
        document.getElementById('authSignedOut').style.display = 'flex';
    } else {
        document.getElementById('authArea').className = 'auth-area no-oauth';
    }
});

function signOut() {
    fetch('/auth/logout', {method: 'POST'}).then(() => location.reload());
}

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

    # Get user's OAuth token if signed in
    sid = session.get("ytdl_session")
    access_token = get_valid_token(sid) if sid else None
    print(f"[download] sid={sid}, has_token={access_token is not None}, sessions={list(user_sessions.keys())}", flush=True)

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
        args=(dl_id, url, start_time, end_time, live_back, live_duration, access_token),
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
