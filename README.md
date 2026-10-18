# MusicFlow - Ad-Free Music Player & MP3 Downloader 🎵

A modern, fast, and responsive web application to stream any song **100% ad-free** and download high-quality **MP3** files in one click.

## ✨ Features

- 🎧 **100% Ad-Free Audio Streaming**: Play songs directly in the browser without any ads or interruptions.
- ⬇️ **High-Quality MP3 Downloads**: 1-click download converted to 192kbps MP3 format with metadata.
- 🔍 **Universal Search**: Search by song name, movie, singer, or album across millions of tracks.
- 📋 **Direct URL Downloader**: Paste links from YouTube, SoundCloud, and other supported audio sites.
- 🎛️ **Sticky Player Bar**: Seek bar, play/pause, next/previous tracks, volume control, and duration timers.
- 📂 **Downloads Library**: View and replay all previously downloaded songs directly from the web interface.
- ⚡ **Auto-detects FFmpeg**: Seamlessly configures audio conversion tools on Windows.

## 🚀 Quick Start

### 1. Requirements
- Python 3.9+
- FFmpeg (for MP3 conversion)

### 2. Run the App
Double-click `run.bat` or run via command line:

```bash
pip install -r requirements.txt
python app.py
```

Open your browser at:
```
http://localhost:8080
```

## 🛠️ Tech Stack
- **Backend**: Python, Flask, yt-dlp
- **Frontend**: HTML5, Modern CSS (Glassmorphism), Vanilla JavaScript Audio API
- **Audio Conversion**: FFmpeg
