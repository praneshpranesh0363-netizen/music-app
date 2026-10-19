import os
import sys
import glob
import uuid
import shutil
import threading
import urllib.parse
from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp

# Auto-detect FFmpeg from WinGet or common locations if not in PATH
winget_dirs = glob.glob(os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages\*FFmpeg*\*\bin"))
for d in winget_dirs:
    if os.path.isdir(d) and d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory progress tracking
progress_store = {}


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
    """Download audio and convert to MP3."""
    try:
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
        matches = glob.glob(pattern)
        if matches:
            filepath = matches[0]
            safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_", "(", ")", ".")).strip()
            if not safe_title:
                safe_title = "song"
            progress_store[task_id] = {
                "status": "done",
                "percent": 100,
                "title": title,
                "filename": os.path.basename(filepath),
                "download_name": f"{safe_title}.mp3"
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


def extract_playlist_songs(query, limit=35):
    """
    Extract individual songs line-by-line from a YouTube playlist
    when searching with terms like 'playlist', 'album', or full playlist URL.
    """
    try:
        clean_q = query.lower()
        for word in ["playlist", "album"]:
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
            for e in res.get("entries", []):
                if e and e.get("id"):
                    vid = e.get("id")
                    songs.append({
                        "id": vid,
                        "videoId": vid,
                        "title": e.get("title", "Song"),
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def search_songs():
    """Search for songs or playlist tracks on YouTube."""
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "Please provide a search query"}), 400

    try:
        # Check if user is searching for a playlist or album
        is_playlist_query = any(k in query.lower() for k in ["playlist", "album", "list="])
        if is_playlist_query:
            pl_title, pl_songs = extract_playlist_songs(query, limit=35)
            if pl_songs and len(pl_songs) > 0:
                return jsonify({
                    "playlist_title": pl_title,
                    "results": pl_songs
                })

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "default_search": "ytsearch25",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(f"ytsearch25:{query}", download=False)

        songs = []
        for entry in results.get("entries", []):
            if entry:
                vid = entry.get("id")
                url = entry.get("url", "")
                if not vid:
                    if "v=" in url:
                        vid = url.split("v=")[-1].split("&")[0]
                    else:
                        vid = url
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
    """Extract direct audio stream URL for ad-free playback."""
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Please provide a URL"}), 400

    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"

    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
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
    """Start downloading a song."""
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Please provide a URL"}), 400

    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"

    task_id = str(uuid.uuid4())[:8]
    progress_store[task_id] = {"status": "starting", "percent": 0}

    thread = threading.Thread(target=download_song, args=(url, task_id))
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id})


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/api/progress/<task_id>")
def get_progress(task_id):
    """Check download status and percentage."""
    info = progress_store.get(task_id, {"status": "unknown"})
    return jsonify(info)


@app.route("/api/file/<filename>")
def serve_file(filename):
    """Serve a downloaded MP3 file."""
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(filepath):
        task_id = filename.split(".")[0]
        info = progress_store.get(task_id, {})
        clean_name = info.get("download_name")
        if not clean_name:
            clean_name = filename.split("_", 1)[-1] if "_" in filename else filename
        if not clean_name.lower().endswith(".mp3"):
            clean_name += ".mp3"
        try:
            return send_file(
                filepath,
                as_attachment=True,
                download_name=clean_name,
                mimetype="audio/mpeg"
            )
        except TypeError:
            return send_file(
                filepath,
                as_attachment=True,
                attachment_filename=clean_name,
                mimetype="audio/mpeg"
            )
    return jsonify({"error": "File not found"}), 404


@app.route("/api/downloads")
def list_downloads():
    """List all previously downloaded files."""
    files = []
    for f in os.listdir(DOWNLOAD_DIR):
        full_path = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(full_path):
            files.append({
                "filename": f,
                "size_mb": round(os.path.getsize(full_path) / (1024 * 1024), 2),
                "title": f.split("_", 1)[-1] if "_" in f else f
            })
    return jsonify({"files": files})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n==============================================")
    print(f" Music Web App is running!")
    print(f" Open http://localhost:{port} in your browser")
    print(f"==============================================\n")
    app.run(debug=True, host="0.0.0.0", port=port)
