import os
import re
import uuid
import shutil
import threading
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl


# ============================================================
# CONFIG
# ============================================================

APP_NAME = "YT Prophecy"
VERSION = "3.0.0"

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_DOWNLOADS = int(
    os.getenv("MAX_DOWNLOADS", "2")
)

active_downloads = 0
download_lock = threading.Lock()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    description="YT Prophecy YouTube Downloader API"
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
# MODELS
# ============================================================

class URLRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: Optional[str] = "best"


# ============================================================
# COMMON YT-DLP OPTIONS
# ============================================================

def youtube_extractor_args():

    return {
        "youtube": {
            "player_client": [
                "ios",
                "web"
            ]
        }
    }


# ============================================================
# VALIDATION
# ============================================================

def validate_youtube_url(url: str):

    value = url.lower().strip()

    if (
        "youtube.com/" not in value
        and "youtu.be/" not in value
    ):
        raise HTTPException(
            status_code=400,
            detail="Only YouTube URLs are supported."
        )


# ============================================================
# QUALITY FORMAT
# ============================================================

def get_format(quality: str):

    formats = {

        "360":
            "bestvideo[height<=360]+bestaudio/"
            "best[height<=360]/best",

        "480":
            "bestvideo[height<=480]+bestaudio/"
            "best[height<=480]/best",

        "720":
            "bestvideo[height<=720]+bestaudio/"
            "best[height<=720]/best",

        "1080":
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080]/best",

        "1440":
            "bestvideo[height<=1440]+bestaudio/"
            "best[height<=1440]/best",

        "2160":
            "bestvideo[height<=2160]+bestaudio/"
            "best[height<=2160]/best",

        "best":
            "bestvideo+bestaudio/best"
    }

    return formats.get(
        quality,
        formats["best"]
    )


# ============================================================
# ERROR HANDLER
# ============================================================

def friendly_error(error):

    message = str(error)
    lower = message.lower()

    if (
        "sign in to confirm" in lower
        or "not a bot" in lower
        or "confirm you're not a bot" in lower
    ):
        return (
            "YouTube is requiring sign-in or bot "
            "verification for this video. "
            "The server cannot access it right now."
        )

    if "video unavailable" in lower:
        return "This video is unavailable."

    if "private video" in lower:
        return "This video is private."

    if "members-only" in lower:
        return "This video is members-only."

    if "age-restricted" in lower:
        return "This video is age-restricted."

    if "copyright" in lower:
        return (
            "This video cannot be accessed "
            "because of a copyright restriction."
        )

    if "sign in" in lower:
        return (
            "YouTube requires authentication "
            "for this video."
        )

    return (
        message[:1000]
        if message
        else "Unknown YouTube error."
    )


# ============================================================
# FILE HELPERS
# ============================================================

def find_job_file(job_id: str):

    if not re.fullmatch(
        r"[a-f0-9]{32}",
        job_id
    ):
        return None

    files = list(
        DOWNLOAD_DIR.glob(
            f"{job_id}.*"
        )
    )

    for file in files:

        if file.is_file():
            return file

    return None


def cleanup_job(job_id: str):

    for file in DOWNLOAD_DIR.glob(
        f"{job_id}.*"
    ):

        try:

            if file.is_file():
                file.unlink()

            elif file.is_dir():
                shutil.rmtree(file)

        except Exception:
            pass


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {

        "success": True,

        "service":
            "YouTube Downloader API",

        "name":
            APP_NAME,

        "version":
            VERSION,

        "status":
            "online",

        "yt_dlp":
            yt_dlp.version.__version__,

        "routes": {

            "root":
                "GET /",

            "health":
                "GET /api/health",

            "info":
                "POST /api/info",

            "download":
                "POST /api/download",

            "file":
                "GET /api/file/{job_id}",

            "delete":
                "DELETE /api/job/{job_id}"
        }
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/api/health")
def health():

    return {

        "success": True,

        "status":
            "healthy",

        "active_downloads":
            active_downloads,

        "yt_dlp":
            yt_dlp.version.__version__
    }


# ============================================================
# VIDEO INFO
# ============================================================

@app.post("/api/info")
def video_info(
    request: URLRequest
):

    url = str(request.url)

    validate_youtube_url(url)

    ydl_opts = {

        "quiet":
            True,

        "no_warnings":
            True,

        "skip_download":
            True,

        "noplaylist":
            True,

        "extract_flat":
            False,

        "extractor_args":
            youtube_extractor_args()
    }

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

        if not info:

            raise Exception(
                "No video information returned."
            )

        formats = []

        seen_heights = set()

        for item in info.get(
            "formats",
            []
        ):

            height = item.get(
                "height"
            )

            if not height:
                continue

            if height in seen_heights:
                continue

            seen_heights.add(height)

            formats.append({

                "format_id":
                    item.get("format_id"),

                "ext":
                    item.get("ext"),

                "height":
                    height,

                "width":
                    item.get("width"),

                "fps":
                    item.get("fps"),

                "filesize":
                    item.get("filesize"),

                "vcodec":
                    item.get("vcodec"),

                "acodec":
                    item.get("acodec")
            })

        formats.sort(
            key=lambda x:
                x.get("height") or 0,
            reverse=True
        )

        return {

            "success":
                True,

            "video": {

                "id":
                    info.get("id"),

                "title":
                    info.get("title"),

                "description":
                    info.get("description"),

                "thumbnail":
                    info.get("thumbnail"),

                "duration":
                    info.get("duration"),

                "channel":
                    info.get("channel"),

                "channel_id":
                    info.get("channel_id"),

                "uploader":
                    info.get("uploader"),

                "view_count":
                    info.get("view_count"),

                "like_count":
                    info.get("like_count"),

                "upload_date":
                    info.get("upload_date"),

                "webpage_url":
                    info.get("webpage_url")
            },

            "formats":
                formats
        }

    except Exception as error:

        raise HTTPException(

            status_code=400,

            detail=(
                "Unable to retrieve "
                "video information: "
                + friendly_error(error)
            )
        )


# ============================================================
# DOWNLOAD
# ============================================================

@app.post("/api/download")
def download_video(
    request: DownloadRequest
):

    global active_downloads

    url = str(request.url)

    validate_youtube_url(url)

    quality = str(
        request.quality or "best"
    ).lower()

    allowed_quality = {

        "360",
        "480",
        "720",
        "1080",
        "1440",
        "2160",
        "best"
    }

    if quality not in allowed_quality:

        raise HTTPException(

            status_code=400,

            detail=
                "Unsupported quality. "
                "Use 360, 480, 720, 1080, "
                "1440, 2160 or best."
        )

    with download_lock:

        if active_downloads >= MAX_DOWNLOADS:

            raise HTTPException(

                status_code=429,

                detail=(
                    "Server is busy. "
                    "Please try again later."
                )
            )

        active_downloads += 1

    job_id = uuid.uuid4().hex

    output_template = str(

        DOWNLOAD_DIR /
        f"{job_id}.%(ext)s"
    )

    ydl_opts = {

        "format":
            get_format(quality),

        "outtmpl":
            output_template,

        "merge_output_format":
            "mp4",

        "noplaylist":
            True,

        "quiet":
            True,

        "no_warnings":
            True,

        "retries":
            3,

        "fragment_retries":
            3,

        "continuedl":
            True,

        "overwrites":
            True,

        "restrictfilenames":
            True,

        "windowsfilenames":
            True,

        "concurrent_fragment_downloads":
            2,

        "extractor_args":
            youtube_extractor_args()
    }

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

        file_path = find_job_file(
            job_id
        )

        if not file_path:

            cleanup_job(job_id)

            raise Exception(
                "Downloaded file was not found."
            )

        return {

            "success":
                True,

            "job_id":
                job_id,

            "title":
                info.get("title"),

            "duration":
                info.get("duration"),

            "filename":
                file_path.name,

            "size":
                file_path.stat().st_size,

            "download_url":
                f"/api/file/{job_id}"
        }

    except Exception as error:

        cleanup_job(job_id)

        raise HTTPException(

            status_code=400,

            detail=(
                "Download failed: "
                + friendly_error(error)
            )
        )

    finally:

        with download_lock:

            active_downloads -= 1


# ============================================================
# FILE DOWNLOAD
# ============================================================

@app.get("/api/file/{job_id}")
def get_file(
    job_id: str
):

    file_path = find_job_file(
        job_id
    )

    if not file_path:

        raise HTTPException(

            status_code=404,

            detail=
                "File not found or expired."
        )

    return FileResponse(

        path=str(file_path),

        filename=file_path.name,

        media_type=
            "application/octet-stream"
    )


# ============================================================
# DELETE JOB
# ============================================================

@app.delete("/api/job/{job_id}")
def delete_job(
    job_id: str
):

    file_path = find_job_file(
        job_id
    )

    if not file_path:

        raise HTTPException(

            status_code=404,

            detail=
                "File not found or already deleted."
        )

    try:

        file_path.unlink()

        return {

            "success":
                True,

            "job_id":
                job_id,

            "message":
                "File deleted successfully."
        }

    except Exception as error:

        raise HTTPException(

            status_code=500,

            detail=
                f"Unable to delete file: {error}"
        )
