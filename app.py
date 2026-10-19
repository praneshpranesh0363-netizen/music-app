import os
import sys
import glob
import uuid
import shutil
import threading
import urllib.parse
import json
import re
import time
from collections import defaultdict
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
import yt_dlp

# Auto-detect FFmpeg from WinGet or common locations if not in PATH
winget_dirs = glob.glob(os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages\*FFmpeg*\*\bin"))
for d in winget_dirs:
    if os.path.isdir(d) and d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

app = Flask(__name__)
# Security: enforce request body payload limit (max 512KB)
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory progress tracking
progress_store = {}

# Security & Concurrency Tracking
rate_limit_lock = threading.Lock()
ip_request_history = defaultdict(list)

active_downloads_lock = threading.Lock()
active_downloads_count = 0
MAX_CONCURRENT_DOWNLOADS = 3

# Whitelisted root files for static serving
ALLOWED_ROOT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".ico", ".svg", ".css", ".js"}
ALLOWED_ROOT_FILES = {"playlist_flow_diagram.jpg", "favicon.ico"}


def check_rate_limit(key, max_requests, window_seconds=60):
    """Simple in-memory sliding-window rate limiter to prevent DoS attacks."""
    now = time.time()
    with rate_limit_lock:
        history = ip_request_history[key]
        ip_request_history[key] = [t for t in history if now - t < window_seconds]
        if len(ip_request_history[key]) >= max_requests:
            return False
        ip_request_history[key].append(now)
        return True


def cleanup_old_downloads(max_age_hours=2, max_total_mb=400):
    """
    Auto-purge downloaded MP3s older than max_age_hours or if folder exceeds max_total_mb.
    Protects server against disk-filling denial of service.
    """
    try:
        now = time.time()
        files = []
        total_size = 0
        for f in os.listdir(DOWNLOAD_DIR):
            fp = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(fp) and f != ".gitkeep":
                try:
                    st = os.stat(fp)
                    files.append((fp, st.st_mtime, st.st_size))
                    total_size += st.st_size
                except Exception:
                    pass

        # Remove files older than max_age_hours
        for fp, mtime, size in files:
            if now - mtime > max_age_hours * 3600:
                try:
                    os.remove(fp)
                    total_size -= size
                except Exception:
                    pass

        # If folder still exceeds quota, delete oldest first
        if total_size > max_total_mb * 1024 * 1024:
            files.sort(key=lambda x: x[1])
            for fp, mtime, size in files:
                if total_size <= max_total_mb * 1024 * 1024:
                    break
                try:
                    os.remove(fp)
                    total_size -= size
                except Exception:
                    pass
    except Exception as e:
        print(f"Cleanup error: {e}")


def validate_youtube_target(target):
    """
    Strict URL & Video ID Validator.
    Blocks SSRF (Server-Side Request Forgery), command injection, localhost loops,
    AWS metadata endpoints, and non-YouTube domains.
    """
    if not target or not isinstance(target, str):
        return None
    target = target.strip()
    if len(target) > 300:
        return None

    # 1. Direct 11-character Video ID
    if re.match(r'^[a-zA-Z0-9_-]{11}$', target):
        return f"https://www.youtube.com/watch?v={target}"

    # 2. Direct Playlist ID
    if re.match(r'^(PL|OLAK5uy_)[a-zA-Z0-9_-]{10,80}$', target):
        return f"https://www.youtube.com/playlist?list={target}"

    # 3. Full YouTube URLs
    try:
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme not in ("http", "https"):
            return None
        domain = (parsed.netloc or "").lower().split(":")[0]
        allowed_domains = {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
        if domain not in allowed_domains:
            return None

        # Block any private network or loopback strings
        lower_target = target.lower()
        if any(bad in lower_target for bad in ["169.254", "localhost", "127.0.0.1", "0.0.0.0", "@", "%00", "\\"]):
            return None

        return target
    except Exception:
        return None


def get_safe_ascii_name(name):
    """Generate safe ASCII-only filename with .mp3 extension to prevent header encoding issues."""
    clean = re.sub(r'[^a-zA-Z0-9\s\-_.()]', '', name or '').strip()
    clean = re.sub(r'\s+', ' ', clean)
    if not clean or clean == ".mp3":
        clean = "Song"
    if not clean.lower().endswith(".mp3"):
        clean += ".mp3"
    return clean


def progress_hook(d, task_id):
    """Track yt-dlp download progress."""
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        percent = round((downloaded / total) * 100, 1) if total > 0 else 0
        progress_store[task_id] = {
            "status": "downloading",
            "percent": percent,
        }
    elif d["status"] == "finished":
        progress_store[task_id] = {
            "status": "processing",
            "percent": 100,
        }


def download_song(url, task_id):
    """Download audio and convert to MP3 with concurrency & disk quota safety."""
    global active_downloads_count
    with active_downloads_lock:
        active_downloads_count += 1

    try:
        cleanup_old_downloads()
        has_ffmpeg = shutil.which("ffmpeg") is not None
        output_template = os.path.join(DOWNLOAD_DIR, f"{task_id}.%(ext)s")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "windowsfilenames": True,
            "progress_hooks": [lambda d: progress_hook(d, task_id)],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": 120 * 1024 * 1024,  # Cap at 120MB audio to prevent disk exhaustion
        }

        if has_ffmpeg:
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "Unknown Title")

        pattern = os.path.join(DOWNLOAD_DIR, f"{task_id}.*")
        matches = [m for m in glob.glob(pattern) if not m.endswith(".json") and not m.endswith(".part")]
        if matches:
            mp3_matches = [m for m in matches if m.lower().endswith(".mp3")]
            filepath = mp3_matches[0] if mp3_matches else matches[0]
            clean_name = get_safe_ascii_name(title)

            # Persist metadata to disk across server reloads
            meta_data = {
                "task_id": task_id,
                "title": title,
                "download_name": clean_name,
                "filename": os.path.basename(filepath)
            }
            try:
                meta_path = os.path.join(DOWNLOAD_DIR, f"{task_id}.meta.json")
                with open(meta_path, "w", encoding="utf-8") as mf:
                    json.dump(meta_data, mf, ensure_ascii=False)
            except Exception as me:
                print(f"Meta write error: {me}")

            progress_store[task_id] = {
                "status": "done",
                "percent": 100,
                "title": title,
                "filename": os.path.basename(filepath),
                "download_name": clean_name
            }
        else:
            progress_store[task_id] = {
                "status": "error",
                "message": "File not found after download completed",
            }

    except Exception as e:
        progress_store[task_id] = {
            "status": "error",
            "message": str(e),
        }
    finally:
        with active_downloads_lock:
            active_downloads_count = max(0, active_downloads_count - 1)


def extract_playlist_songs(query, limit=75):
    """
    Extract individual songs line-by-line from a YouTube playlist (expanded to up to 75 songs)
    when searching with terms like 'playlist', 'album', or full playlist URL.
    """
    try:
        clean_q = query.lower()
        for word in ["playlist", "album", "songs", "song"]:
            clean_q = clean_q.replace(word, "")
        clean_q = clean_q.strip() or query

        if "list=" in query:
            url = query
            pl_title = "Playlist"
        else:
            search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(clean_q)}&sp=EgIQAw%253D%253D"
            ydl_opts_search = {"quiet": True, "no_warnings": True, "extract_flat": True}
            with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
                res = ydl.extract_info(search_url, download=False)
                entries = res.get("entries", [])
                if not entries:
                    return None, []
                top_pl = entries[0]
                pl_id = top_pl.get("id")
                pl_title = top_pl.get("title", "Playlist")
                url = top_pl.get("url") if (top_pl.get("url") and "playlist" in top_pl.get("url")) else f"https://www.youtube.com/playlist?list={pl_id}"

        ydl_opts_pl = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": limit}
        with yt_dlp.YoutubeDL(ydl_opts_pl) as ydl:
            res = ydl.extract_info(url, download=False)
            songs = []
            seen_ids = set()
            for e in res.get("entries", []):
                if e and e.get("id"):
                    vid = e.get("id")
                    title = (e.get("title") or "").strip()
                    if not title or title.lower() in ("[private video]", "[deleted video]", "none"):
                        continue
                    if vid in seen_ids:
                        continue
                    seen_ids.add(vid)
                    songs.append({
                        "id": vid,
                        "videoId": vid,
                        "title": title,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration": e.get("duration") or 240,
                        "channel": e.get("uploader") or e.get("channel") or "Unknown Artist",
                        "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                    })
            extracted_title = res.get("title") or pl_title
            return extracted_title, songs
    except Exception as e:
        print(f"Error extracting playlist: {e}")
        return None, []


# ========================================================
# SECURITY MIDDLEWARE & HEADERS
# ========================================================

@app.before_request
def security_before_request():
    """Security filter: rate limits, IP tracking, and request size validation."""
    client_ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "127.0.0.1"
    )

    # Rate limiting on API search endpoints (40 searches / minute per IP)
    if request.path == "/api/search":
        if not check_rate_limit(f"{client_ip}:search", max_requests=40, window_seconds=60):
            return jsonify({"error": "Too many searches. Please slow down and wait a minute."}), 429

    # Rate limiting on API download initiation (6 downloads / minute per IP)
    elif request.path == "/api/download":
        if not check_rate_limit(f"{client_ip}:download", max_requests=6, window_seconds=60):
            return jsonify({"error": "Too many download requests. Please wait a minute before starting another."}), 429

    # Rate limiting on streaming extraction (30 streams / minute per IP)
    elif request.path == "/api/stream":
        if not check_rate_limit(f"{client_ip}:stream", max_requests=30, window_seconds=60):
            return jsonify({"error": "Too many stream requests. Please wait a moment."}), 429


@app.after_request
def add_security_and_cors_headers(response):
    """Inject OWASP enterprise security headers & CORS headers."""
    # CORS Headers
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"

    # OWASP Core Security Headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

    # Content-Security-Policy: Allow trusted resources
    csp = (
        "default-src 'self' https: data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://www.youtube.com https://s.ytimg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' https: data: blob:; "
        "media-src 'self' https: data: blob:; "
        "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
        "connect-src 'self' https:;"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# ========================================================
# APPLICATION ROUTES
# ========================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def search_songs():
    """Search for songs or full playlist tracks on YouTube (up to 75 songs for playlists, 50 for songs)."""
    data = request.get_json(silent=True) or {}
    raw_query = data.get("query", "")
    if not raw_query or not isinstance(raw_query, str):
        return jsonify({"error": "Please provide a search query"}), 400

    # Sanitize query input: strip control characters and cap at 120 characters
    query = re.sub(r'[\x00-\x1f\x7f]', '', raw_query).strip()[:120]
    if not query:
        return jsonify({"error": "Invalid search query"}), 400

    try:
        # Check if user is searching for a playlist, album, or direct playlist link
        is_playlist_query = any(k in query.lower() for k in ["playlist", "album", "list="])
        if is_playlist_query:
            pl_title, pl_songs = extract_playlist_songs(query, limit=75)
            if pl_songs and len(pl_songs) > 0:
                return jsonify({
                    "playlist_title": pl_title,
                    "results": pl_songs
                })

        # Regular multi-track search expanded up to 50 songs
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "default_search": "ytsearch50",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(f"ytsearch50:{query}", download=False)

        songs = []
        seen_ids = set()
        for entry in results.get("entries", []):
            if entry:
                vid = entry.get("id")
                url = entry.get("url", "")
                if not vid:
                    if "v=" in url:
                        vid = url.split("v=")[-1].split("&")[0]
                    else:
                        vid = url
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)
                thumb = ""
                if entry.get("thumbnails"):
                    thumb = entry["thumbnails"][-1].get("url", "")
                if not thumb and vid:
                    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                songs.append({
                    "id": vid,
                    "videoId": vid,
                    "title": entry.get("title", "Unknown"),
                    "url": url,
                    "duration": entry.get("duration") or 240,
                    "channel": entry.get("channel") or entry.get("uploader", "Unknown Artist"),
                    "thumbnail": thumb,
                })

        return jsonify({"results": songs})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stream", methods=["POST"])
def stream_song():
    """Extract direct audio stream URL with SSRF protection."""
    data = request.get_json(silent=True) or {}
    raw_url = data.get("url", "")
    safe_url = validate_youtube_target(raw_url)
    if not safe_url:
        return jsonify({"error": "Invalid audio URL. Only legitimate YouTube links are permitted."}), 400

    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(safe_url, download=False)
            audio_url = info.get("url", "")
            title = info.get("title", "Unknown")
            thumbnail = ""
            if info.get("thumbnails"):
                thumbnail = info["thumbnails"][-1].get("url", "")
            duration = info.get("duration", 0)
            channel = info.get("channel") or info.get("uploader", "Unknown")

        return jsonify({
            "stream_url": audio_url,
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration,
            "channel": channel,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    """Start downloading a song with concurrency & security safeguards."""
    # Check concurrent downloads limit
    with active_downloads_lock:
        if active_downloads_count >= MAX_CONCURRENT_DOWNLOADS:
            return jsonify({"error": "The server is currently processing other downloads. Please try again in a few moments."}), 503

    data = request.get_json(silent=True) or {}
    raw_url = data.get("url", "")
    safe_url = validate_youtube_target(raw_url)
    if not safe_url:
        return jsonify({"error": "Invalid URL. Only legitimate YouTube audio links are permitted."}), 400

    task_id = str(uuid.uuid4())[:8]
    progress_store[task_id] = {"status": "starting", "percent": 0}

    thread = threading.Thread(target=download_song, args=(safe_url, task_id))
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def get_progress(task_id):
    """Check download status and percentage with ID sanitization."""
    # Sanitize task_id format
    if not task_id or not re.match(r'^[a-zA-Z0-9_-]{1,32}$', task_id):
        return jsonify({"status": "unknown"}), 400
    info = progress_store.get(task_id, {"status": "unknown"})
    return jsonify(info)


@app.route("/api/file/<filename>")
def serve_file(filename):
    """
    Serve a downloaded MP3 file safely.
    Blocks directory traversal and path manipulation attacks.
    """
    # 1. Path Traversal Guard: reject null bytes, directory slashes, or non-matching names
    if ".." in filename or "/" in filename or "\\" in filename or "\x00" in filename:
        return jsonify({"error": "Invalid file request"}), 400

    clean_filename = secure_filename(filename)
    if not re.match(r'^[a-zA-Z0-9_\-\.]+\.mp3$', clean_filename):
        return jsonify({"error": "Invalid file format requested"}), 400

    filepath = os.path.join(DOWNLOAD_DIR, clean_filename)

    # 2. Path Traversal Guard: ensure absolute path is strictly inside DOWNLOAD_DIR
    real_download_dir = os.path.abspath(DOWNLOAD_DIR)
    real_filepath = os.path.abspath(filepath)
    if not real_filepath.startswith(real_download_dir):
        return jsonify({"error": "Access denied"}), 403

    if not os.path.exists(real_filepath):
        task_id = clean_filename.split(".")[0]
        pattern = os.path.join(DOWNLOAD_DIR, f"{task_id}.*")
        matches = [m for m in glob.glob(pattern) if not m.endswith(".json") and not m.endswith(".part")]
        if matches:
            real_filepath = matches[0]
        else:
            return jsonify({"error": "File not found"}), 404

    task_id = os.path.basename(real_filepath).split(".")[0]
    info = progress_store.get(task_id, {})
    title = info.get("title")
    clean_name = info.get("download_name")

    if not title or not clean_name:
        meta_path = os.path.join(DOWNLOAD_DIR, f"{task_id}.meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as mf:
                    meta = json.load(mf)
                    title = title or meta.get("title")
                    clean_name = clean_name or meta.get("download_name")
            except Exception:
                pass

    if not title:
        title = filename.rsplit(".", 1)[0]
    if not clean_name:
        clean_name = get_safe_ascii_name(title)

    encoded_title = urllib.parse.quote(f"{title}.mp3" if not title.lower().endswith(".mp3") else title)

    response = send_file(
        real_filepath,
        as_attachment=True,
        download_name=clean_name,
        mimetype="audio/mpeg"
    )
    # Add RFC 5987 UTF-8 filename for mobile browsers & standard ASCII fallback
    response.headers["Content-Disposition"] = f'attachment; filename="{clean_name}"; filename*=UTF-8\'\'{encoded_title}'
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/api/downloads")
def list_downloads():
    """List recent downloads safely."""
    files = []
    for f in os.listdir(DOWNLOAD_DIR):
        if f.endswith(".json") or f.startswith("."):
            continue
        full_path = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(full_path):
            files.append({
                "filename": f,
                "size_mb": round(os.path.getsize(full_path) / (1024 * 1024), 2),
                "title": f.split("_", 1)[-1] if "_" in f else f
            })
    return jsonify({"files": files})


@app.route("/<path:filename>")
def serve_root_file(filename):
    """
    Serve strictly whitelisted public assets (like playlist_flow_diagram.jpg).
    Blocks directory traversal, configuration files, source code (.py), and environment keys (.env).
    """
    if ".." in filename or "/" in filename or "\\" in filename or "\x00" in filename:
        return jsonify({"error": "Access denied"}), 403

    safe_name = secure_filename(filename)
    ext = os.path.splitext(safe_name)[1].lower()

    # Strict Whitelist: Only permit safe static asset extensions
    if ext not in ALLOWED_ROOT_EXTENSIONS and safe_name not in ALLOWED_ROOT_FILES:
        return jsonify({"error": "File type forbidden"}), 403

    filepath = os.path.abspath(os.path.join(BASE_DIR, safe_name))
    if not filepath.startswith(os.path.abspath(BASE_DIR)):
        return jsonify({"error": "Access denied"}), 403

    if os.path.exists(filepath) and os.path.isfile(filepath):
        return send_file(filepath)
    return jsonify({"error": "File not found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n==============================================")
    print(f" Music Web App is running with Security Enabled!")
    print(f" OWASP Headers: ACTIVE | Rate Limiting: ACTIVE")
    print(f" Open http://localhost:{port} in your browser")
    print(f"==============================================\n")
    # Production security: debug=False prevents Werkzeug remote interactive console execution
    app.run(debug=False, host="0.0.0.0", port=port)
