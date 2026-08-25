import os
import re
import uuid
import shutil
import threading
import json
import time
import asyncio
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime
from http.cookiejar import CookieJar

import yt_dlp
import browser_cookie3
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================

APP_NAME = "YT Prophecy"
VERSION = "3.0.0"

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
COOKIE_DIR = BASE_DIR / "cookies"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

MAX_DOWNLOADS = int(os.getenv("MAX_DOWNLOADS", "5"))
COOKIE_TTL = int(os.getenv("COOKIE_TTL", "3600"))

active_downloads = 0
download_lock = threading.Lock()

# ============================================================
# DATABASE MANAGER
# ============================================================

class DatabaseManager:
    def __init__(self):
        self.db_path = BASE_DIR / "downloads.db"
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE,
                url TEXT,
                title TEXT,
                quality TEXT,
                filename TEXT,
                filesize INTEGER,
                download_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT,
                error TEXT
            )
        ''')
        conn.commit()
        conn.close()
    
    def save_download(self, job_id: str, data: dict):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO downloads 
            (job_id, url, title, quality, filename, filesize, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            job_id,
            data.get('url'),
            data.get('title'),
            data.get('quality'),
            data.get('filename'),
            data.get('filesize'),
            data.get('status', 'completed')
        ))
        conn.commit()
        conn.close()
    
    def get_history(self, limit=50):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT job_id, title, quality, filename, filesize, download_date, status
            FROM downloads
            ORDER BY download_date DESC
            LIMIT ?
        ''', (limit,))
        results = cursor.fetchall()
        conn.close()
        return results
    
    def update_status(self, job_id: str, status: str, error: str = None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        if error:
            cursor.execute('''
                UPDATE downloads 
                SET status = ?, error = ?
                WHERE job_id = ?
            ''', (status, error, job_id))
        else:
            cursor.execute('''
                UPDATE downloads 
                SET status = ?
                WHERE job_id = ?
            ''', (status, job_id))
        conn.commit()
        conn.close()

db_manager = DatabaseManager()

# ============================================================
# COOKIE MANAGER
# ============================================================

class CookieManager:
    def __init__(self):
        self.cookie_file = COOKIE_DIR / "cookies.txt"
        self.last_refresh = 0
        self.cookie_ttl = COOKIE_TTL
    
    def get_cookies(self):
        """Get cookies, auto-refresh if expired"""
        current_time = time.time()
        
        if (current_time - self.last_refresh > self.cookie_ttl or 
            not self.cookie_file.exists()):
            self.refresh_cookies()
        
        return str(self.cookie_file)
    
    def refresh_cookies(self):
        """Extract fresh cookies from browser"""
        try:
            browsers = [
                ('Chrome', browser_cookie3.chrome),
                ('Firefox', browser_cookie3.firefox),
                ('Edge', browser_cookie3.edge),
                ('Opera', browser_cookie3.opera),
                ('Brave', browser_cookie3.brave),
                ('Vivaldi', browser_cookie3.vivaldi),
                ('Chromium', browser_cookie3.chromium)
            ]
            
            cookies = None
            for browser_name, browser_func in browsers:
                try:
                    cookies = browser_func(domain_name='.youtube.com')
                    if cookies:
                        print(f"✅ Found cookies from {browser_name}")
                        break
                except Exception as e:
                    print(f"⚠️ {browser_name}: {e}")
                    continue
            
            if not cookies:
                raise Exception("No browser cookies found")
            
            self.save_cookies_to_file(cookies)
            self.last_refresh = time.time()
            print(f"✅ Cookies refreshed successfully")
            
        except Exception as e:
            print(f"❌ Cookie refresh failed: {e}")
            if not self.cookie_file.exists():
                raise
    
    def save_cookies_to_file(self, cookies):
        """Save cookies in Netscape format"""
        with open(self.cookie_file, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# This file is auto-generated\n")
            for cookie in cookies:
                if (cookie.domain.endswith('.youtube.com') or 
                    cookie.domain == 'youtube.com'):
                    f.write(f"{cookie.domain}\tTRUE\t{cookie.path}\t")
                    f.write(f"{'TRUE' if cookie.secure else 'FALSE'}\t")
                    f.write(f"{cookie.expires if cookie.expires else 0}\t")
                    f.write(f"{cookie.name}\t{cookie.value}\n")

cookie_manager = CookieManager()

# ============================================================
# DOWNLOAD MANAGER (WebSocket support)
# ============================================================

class DownloadManager:
    def __init__(self):
        self.downloads: Dict[str, Dict] = {}
        self.websockets: Dict[str, WebSocket] = {}
        self.lock = threading.Lock()
    
    def update_progress(self, job_id: str, progress: float, status: str, 
                        speed: str = "", eta: str = "", downloaded: str = ""):
        with self.lock:
            if job_id not in self.downloads:
                self.downloads[job_id] = {}
            
            self.downloads[job_id].update({
                'progress': progress,
                'status': status,
                'speed': speed,
                'eta': eta,
                'downloaded': downloaded,
                'updated_at': time.time()
            })
    
    def get_status(self, job_id: str):
        with self.lock:
            return self.downloads.get(job_id, {})
    
    async def send_websocket_update(self, job_id: str, data: dict):
        if job_id in self.websockets:
            try:
                await self.websockets[job_id].send_json(data)
            except:
                pass
    
    def add_websocket(self, job_id: str, websocket: WebSocket):
        with self.lock:
            self.websockets[job_id] = websocket
    
    def remove_websocket(self, job_id: str):
        with self.lock:
            if job_id in self.websockets:
                del self.websockets[job_id]

download_manager = DownloadManager()

# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    description="YT Prophecy YouTube Downloader API with Auto Cookie Management"
)

# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# TEMPLATES
# ============================================================

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ============================================================
# MODELS
# ============================================================

class URLRequest(BaseModel):
    url: HttpUrl

class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: Optional[str] = "best"

class BatchRequest(BaseModel):
    urls: List[HttpUrl]
    quality: Optional[str] = "best"

# ============================================================
# YT-DLP EXTRACTOR ARGS
# ============================================================

def youtube_extractor_args():
    return {
        "youtube": {
            "player_client": ["ios", "web", "android"],
            "player_skip": ["webpage", "configs"],
            "hls_streams": True,
        }
    }

# ============================================================
# VALIDATION
# ============================================================

def validate_youtube_url(url: str):
    value = url.lower().strip()
    if "youtube.com/" not in value and "youtu.be/" not in value:
        raise HTTPException(
            status_code=400,
            detail="Only YouTube URLs are supported."
        )

# ============================================================
# QUALITY FORMAT
# ============================================================

def get_format(quality: str):
    formats = {
        "360": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "1440": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
        "2160": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
        "best": "bestvideo+bestaudio/best"
    }
    return formats.get(quality, formats["best"])

# ============================================================
# ERROR HANDLER
# ============================================================

def friendly_error(error):
    message = str(error)
    lower = message.lower()
    
    if "sign in to confirm" in lower or "not a bot" in lower:
        return "YouTube requires sign-in or bot verification. Server cookies may have expired."
    if "video unavailable" in lower:
        return "This video is unavailable."
    if "private video" in lower:
        return "This video is private."
    if "members-only" in lower:
        return "This video is members-only."
    if "age-restricted" in lower:
        return "This video is age-restricted."
    if "copyright" in lower:
        return "This video has copyright restrictions."
    if "sign in" in lower:
        return "YouTube requires authentication for this video."
    if "rate limit" in lower:
        return "Rate limited by YouTube. Please try again later."
    
    return message[:1000] if message else "Unknown YouTube error."

# ============================================================
# FILE HELPERS
# ============================================================

def find_job_file(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return None
    
    files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
    for file in files:
        if file.is_file():
            return file
    return None

def cleanup_job(job_id: str):
    for file in DOWNLOAD_DIR.glob(f"{job_id}.*"):
        try:
            if file.is_file():
                file.unlink()
            elif file.is_dir():
                shutil.rmtree(file)
        except Exception:
            pass

# ============================================================
# CLEANUP SERVICE
# ============================================================

class CleanupService:
    def __init__(self):
        self.running = True
        self.thread = threading.Thread(target=self.cleanup_loop, daemon=True)
        self.thread.start()
    
    def cleanup_loop(self):
        while self.running:
            try:
                self.cleanup_old_files()
                time.sleep(3600)  # Run every hour
            except Exception as e:
                print(f"Cleanup error: {e}")
    
    def cleanup_old_files(self):
        """Delete files older than 24 hours"""
        now = time.time()
        cutoff = now - (24 * 3600)
        
        for file in DOWNLOAD_DIR.glob("*"):
            if file.is_file():
                if file.stat().st_mtime < cutoff:
                    try:
                        file.unlink()
                        job_id = file.stem
                        db_manager.update_status(job_id, "expired")
                        print(f"🧹 Cleaned up expired file: {file.name}")
                    except Exception as e:
                        print(f"❌ Failed to cleanup {file.name}: {e}")

# Start cleanup service
cleanup_service = CleanupService()

# ============================================================
# ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Serve the main HTML page"""
    return templates.TemplateResponse("index.html", {"request": request})

# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/health")
def health():
    return {
        "success": True,
        "status": "healthy",
        "active_downloads": active_downloads,
        "yt_dlp": yt_dlp.version.__version__,
        "cookies_valid": cookie_manager.cookie_file.exists()
    }

@app.post("/api/info")
def video_info(request: URLRequest):
    """Get video information without downloading"""
    url = str(request.url)
    validate_youtube_url(url)
    
    try:
        cookies = cookie_manager.get_cookies()
        
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
            "extractor_args": youtube_extractor_args(),
            "cookiefile": cookies,
            "socket_timeout": 30,
            "retries": 3,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        if not info:
            raise Exception("No video information returned.")
        
        formats = []
        seen_heights = set()
        
        for item in info.get("formats", []):
            height = item.get("height")
            if not height:
                continue
            if height in seen_heights:
                continue
            seen_heights.add(height)
            
            formats.append({
                "format_id": item.get("format_id"),
                "ext": item.get("ext"),
                "height": height,
                "width": item.get("width"),
                "fps": item.get("fps"),
                "filesize": item.get("filesize"),
                "vcodec": item.get("vcodec"),
                "acodec": item.get("acodec")
            })
        
        formats.sort(key=lambda x: x.get("height") or 0, reverse=True)
        
        return {
            "success": True,
            "video": {
                "id": info.get("id"),
                "title": info.get("title"),
                "description": info.get("description"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "channel": info.get("channel"),
                "channel_id": info.get("channel_id"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
                "like_count": info.get("like_count"),
                "upload_date": info.get("upload_date"),
                "webpage_url": info.get("webpage_url")
            },
            "formats": formats
        }
    
    except Exception as error:
        error_msg = friendly_error(error)
        print(f"❌ Info error: {error}")
        raise HTTPException(
            status_code=400,
            detail=f"Unable to retrieve video information: {error_msg}"
        )

@app.post("/api/quality")
def get_available_quality(request: URLRequest):
    """Get available quality options for a video"""
    url = str(request.url)
    validate_youtube_url(url)
    
    try:
        cookies = cookie_manager.get_cookies()
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extractor_args": youtube_extractor_args(),
            "cookiefile": cookies,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        heights = set()
        for f in info.get("formats", []):
            if f.get("height"):
                heights.add(str(f["height"]))
        
        available = sorted([int(h) for h in heights if h.isdigit()], reverse=True)
        available = [str(h) for h in available if h >= 360]
        
        # Recommend best quality
        recommended = available[0] if available else "best"
        
        return {
            "success": True,
            "available": available,
            "recommended": recommended
        }
    
    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to get quality options: {friendly_error(error)}"
        )

@app.post("/api/download")
def download_video(request: DownloadRequest):
    """Download a video with specified quality"""
    global active_downloads
    
    url = str(request.url)
    validate_youtube_url(url)
    
    quality = str(request.quality or "best").lower()
    allowed_quality = {"360", "480", "720", "1080", "1440", "2160", "best"}
    
    if quality not in allowed_quality:
        raise HTTPException(
            status_code=400,
            detail="Unsupported quality. Use 360, 480, 720, 1080, 1440, 2160 or best."
        )
    
    with download_lock:
        if active_downloads >= MAX_DOWNLOADS:
            raise HTTPException(
                status_code=429,
                detail=f"Server busy. Maximum {MAX_DOWNLOADS} concurrent downloads allowed."
            )
        active_downloads += 1
    
    job_id = uuid.uuid4().hex
    
    try:
        cookies = cookie_manager.get_cookies()
        output_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
        
        # Progress hook
        def progress_hook(d):
            if d['status'] == 'downloading':
                progress_str = d.get('_percent_str', '0%').strip('%')
                try:
                    progress = float(progress_str)
                except:
                    progress = 0
                
                download_manager.update_progress(
                    job_id,
                    progress,
                    "Downloading",
                    d.get('_speed_str', ''),
                    d.get('_eta_str', ''),
                    d.get('_downloaded_str', '')
                )
                
                # Async WebSocket update
                asyncio.create_task(
                    download_manager.send_websocket_update(job_id, {
                        'type': 'progress',
                        'job_id': job_id,
                        'progress': progress,
                        'speed': d.get('_speed_str', ''),
                        'eta': d.get('_eta_str', ''),
                        'downloaded': d.get('_downloaded_str', ''),
                        'total': d.get('_total_str', '')
                    })
                )
            
            elif d['status'] == 'finished':
                download_manager.update_progress(job_id, 100, "Complete")
                asyncio.create_task(
                    download_manager.send_websocket_update(job_id, {
                        'type': 'complete',
                        'job_id': job_id,
                        'message': 'Download completed!'
                    })
                )
        
        ydl_opts = {
            "format": get_format(quality),
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "fragment_retries": 5,
            "continuedl": True,
            "overwrites": True,
            "restrictfilenames": True,
            "windowsfilenames": True,
            "concurrent_fragment_downloads": 3,
            "extractor_args": youtube_extractor_args(),
            "cookiefile": cookies,
            "progress_hooks": [progress_hook],
            "socket_timeout": 30,
        }
        
        # Initialize download status
        download_manager.update_progress(job_id, 0, "Starting...")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        
        file_path = find_job_file(job_id)
        if not file_path:
            cleanup_job(job_id)
            raise Exception("Downloaded file was not found.")
        
        # Save to database
        db_manager.save_download(job_id, {
            'url': url,
            'title': info.get('title'),
            'quality': quality,
            'filename': file_path.name,
            'filesize': file_path.stat().st_size,
            'status': 'completed'
        })
        
        return {
            "success": True,
            "job_id": job_id,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "filename": file_path.name,
            "size": file_path.stat().st_size,
            "download_url": f"/api/file/{job_id}"
        }
    
    except Exception as error:
        cleanup_job(job_id)
        error_msg = friendly_error(error)
        db_manager.update_status(job_id, "failed", error_msg)
        print(f"❌ Download error: {error}")
        raise HTTPException(
            status_code=400,
            detail=f"Download failed: {error_msg}"
        )
    
    finally:
        with download_lock:
            active_downloads -= 1

@app.post("/api/batch-download")
def batch_download(request: BatchRequest):
    """Download multiple videos at once"""
    if len(request.urls) > 10:
        raise HTTPException(400, "Maximum 10 videos per batch")
    
    results = []
    for url in request.urls:
        try:
            download_req = DownloadRequest(url=url, quality=request.quality)
            result = download_video(download_req)
            results.append(result)
        except HTTPException as e:
            results.append({
                'url': str(url),
                'error': e.detail,
                'success': False
            })
        except Exception as e:
            results.append({
                'url': str(url),
                'error': str(e),
                'success': False
            })
    
    return {
        'success': True,
        'total': len(results),
        'successful': len([r for r in results if r.get('success')]),
        'jobs': results
    }

@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    """Download the actual file"""
    file_path = find_job_file(job_id)
    if not file_path:
        raise HTTPException(
            status_code=404,
            detail="File not found or expired."
        )
    
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream"
    )

@app.delete("/api/job/{job_id}")
def delete_job(job_id: str):
    """Delete a downloaded file"""
    file_path = find_job_file(job_id)
    if not file_path:
        raise HTTPException(
            status_code=404,
            detail="File not found or already deleted."
        )
    
    try:
        file_path.unlink()
        db_manager.update_status(job_id, "deleted")
        return {
            "success": True,
            "job_id": job_id,
            "message": "File deleted successfully."
        }
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to delete file: {error}"
        )

@app.get("/api/history")
def get_history(limit: int = 50):
    """Get download history"""
    history = db_manager.get_history(limit)
    return {
        'success': True,
        'count': len(history),
        'history': [
            {
                'job_id': row[0],
                'title': row[1],
                'quality': row[2],
                'filename': row[3],
                'filesize': row[4],
                'date': row[5],
                'status': row[6]
            }
            for row in history
        ]
    }

@app.post("/api/refresh-cookies")
def refresh_cookies():
    """Manually refresh cookies"""
    try:
        cookie_manager.refresh_cookies()
        return {
            'success': True,
            'message': 'Cookies refreshed successfully'
        }
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to refresh cookies: {error}"
        )

# ============================================================
# WEBSOCKET ENDPOINT
# ============================================================

@app.websocket("/api/ws/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str):
    await websocket.accept()
    download_manager.add_websocket(job_id, websocket)
    
    try:
        # Send initial status
        status = download_manager.get_status(job_id)
        if status:
            await websocket.send_json({
                'type': 'status',
                'job_id': job_id,
                'data': status
            })
        
        while True:
            # Keep connection alive
            data = await websocket.receive_text()
            if data == 'ping':
                await websocket.send_text('pong')
    
    except WebSocketDisconnect:
        download_manager.remove_websocket(job_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        download_manager.remove_websocket(job_id)

# ============================================================
# ERROR HANDLERS
# ============================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "status_code": exc.status_code
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "detail": str(exc) if os.getenv("DEBUG") else None
        }
    )

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
        )
