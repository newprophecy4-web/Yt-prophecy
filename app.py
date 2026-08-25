import os
import uuid
import shutil
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
import yt_dlp


# =========================
# Configuration
# =========================

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_DOWNLOADS = int(os.getenv("MAX_DOWNLOADS", "2"))

active_downloads = 0
download_lock = threading.Lock()


# =========================
# FastAPI
# =========================

app = FastAPI(
    title="YouTube Downloader API",
    version="1.0.0",
    description="yt-dlp based video downloader API"
)


# =========================
# Models
# =========================

class URLRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: Optional[str] = "best"


# =========================
# Helpers
# =========================

def clean_download_dir():
    """
    Remove old files.
    Render local disk is temporary, so files should not
    be treated as permanent storage.
    """

    for item in DOWNLOAD_DIR.iterdir():

        try:
            if item.is_file():
                item.unlink()

            elif item.is_dir():
                shutil.rmtree(item)

        except Exception:
            pass


def validate_url(url: str):

    allowed_domains = (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtube-nocookie.com",
    )

    if not any(
        domain in url.lower()
        for domain in allowed_domains
    ):
        raise HTTPException(
            status_code=400,
            detail="Only supported YouTube URLs are allowed."
        )


def get_format(quality: str):

    quality = quality.lower()

    formats = {
        "360": "bestvideo[height<=360]+bestaudio/best[height<=360]",
        "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "1440": "bestvideo[height<=1440]+bestaudio/best[height<=1440]",
        "2160": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
        "best": "bestvideo+bestaudio/best",
    }

    return formats.get(
        quality,
        formats["best"]
    )


# =========================
# Health
# =========================

@app.get("/")
def root():

    return {
        "success": True,
        "service": "YouTube Downloader API",
        "version": "1.0.0",
        "status": "online"
    }


@app.get("/api/health")
def health():

    return {
        "success": True,
        "status": "healthy",
        "active_downloads": active_downloads
    }


# =========================
# Video Information
# =========================

@app.post("/api/info")
def video_info(request: URLRequest):

    url = str(request.url)

    validate_url(url)

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:

        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

        formats = []

        for f in info.get("formats", []):

            height = f.get("height")

            if height:

                formats.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "height": height,
                    "width": f.get("width"),
                    "fps": f.get("fps"),
                    "filesize": f.get("filesize"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                })

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
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
                "webpage_url": info.get("webpage_url"),
            },
            "formats": formats
        }

    except Exception as e:

        raise HTTPException(
            status_code=400,
            detail=f"Unable to retrieve video information: {str(e)}"
        )


# =========================
# Download
# =========================

@app.post("/api/download")
def download_video(request: DownloadRequest):

    global active_downloads

    url = str(request.url)

    validate_url(url)

    with download_lock:

        if active_downloads >= MAX_DOWNLOADS:

            raise HTTPException(
                status_code=429,
                detail="Server is busy. Please try again later."
            )

        active_downloads += 1

    job_id = uuid.uuid4().hex

    output_template = str(
        DOWNLOAD_DIR /
        f"{job_id}.%(ext)s"
    )

    options = {
        "format": get_format(request.quality),

        "outtmpl": output_template,

        "merge_output_format": "mp4",

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "restrictfilenames": True,

        "overwrites": True,

        "retries": 3,

        "fragment_retries": 3,

    }

    try:

        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

        # Find generated file
        files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))

        if not files:

            raise Exception(
                "Downloaded file was not found."
            )

        file_path = files[0]

        return {
            "success": True,

            "job_id": job_id,

            "title": info.get("title"),

            "duration": info.get("duration"),

            "filename": file_path.name,

            "download_url":
                f"/api/file/{job_id}"
        }

    except Exception as e:

        raise HTTPException(
            status_code=400,
            detail=f"Download failed: {str(e)}"
        )

    finally:

        with download_lock:
            active_downloads -= 1


# =========================
# File Download
# =========================

@app.get("/api/file/{job_id}")
def get_file(job_id: str):

    if not job_id.isalnum():

        raise HTTPException(
            status_code=400,
            detail="Invalid job ID."
        )

    files = list(
        DOWNLOAD_DIR.glob(
            f"{job_id}.*"
        )
    )

    if not files:

        raise HTTPException(
            status_code=404,
            detail="File not found or expired."
        )

    file_path = files[0]

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream"
    )


# =========================
# Delete File
# =========================

@app.delete("/api/job/{job_id}")
def delete_job(job_id: str):

    if not job_id.isalnum():

        raise HTTPException(
            status_code=400,
            detail="Invalid job ID."
        )

    files = list(
        DOWNLOAD_DIR.glob(
            f"{job_id}.*"
        )
    )

    if not files:

        raise HTTPException(
            status_code=404,
            detail="File not found."
        )

    for file in files:

        try:
            file.unlink()
        except Exception:
            pass

    return {
        "success": True,
        "message": "File deleted."
  }
