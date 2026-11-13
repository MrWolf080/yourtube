"""FFmpeg pipeline shared by the worker and the demo data generator."""

import json
import math
import subprocess

from fastapi import HTTPException

ERROR = "Не удалось обработать видео. Проверьте формат и размер файла."
MAX_OUTPUT = 512 * 1024**2
MAX_PREVIEW = 1024**2
MAX_DIMENSION = 4096
MAX_PIXELS = 4096 * 2160
MAX_DURATION = 3600


def probe_video(run, source):
    try:
        return run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries",
             "stream=index,codec_type,width,height,duration:stream_disposition=attached_pic:format=duration,format_name",
             "-of", "json", str(source)], 20,
        )
    except subprocess.CalledProcessError:
        raise HTTPException(422, ERROR)


def prepare_video(source, directory, run=None):
    if run is None:
        def run(command, timeout, on_progress=None):
            return subprocess.run(command, capture_output=True, check=True, timeout=timeout).stdout

    try:
        info = json.loads(probe_video(run, source))
        formats = set(info["format"]["format_name"].split(","))
        if not formats.intersection({"mov", "mp4", "matroska", "webm", "avi", "mpegts", "mpeg", "flv", "ogg"}):
            raise ValueError
        video_stream = next(stream for stream in info["streams"]
                     if stream["codec_type"] == "video" and not stream.get("disposition", {}).get("attached_pic"))
        width, height = int(video_stream["width"]), int(video_stream["height"])
        if (not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION
                or width * height > MAX_PIXELS):
            raise HTTPException(422, "Разрешение видео превышает допустимый предел.")
        seconds = max(float(info["format"]["duration"]),
                      float(video_stream.get("duration") or 0))
        if seconds > MAX_DURATION:
            raise HTTPException(422, "Продолжительность видео превышает 60 минут.")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
        ffmpeg = ["ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "2"]
        video = directory / "video.mp4"
        run(ffmpeg + [
            "-protocol_whitelist", "file", "-threads", "2", "-max_pixels", str(MAX_PIXELS),
            "-i", str(source),
            "-map", f"0:{video_stream['index']}", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", "scale=w='max(2,trunc(min(1920,iw)/2)*2)':h=-2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "veryfast", "-threads", "2", "-c:a", "aac", "-movflags", "+faststart",
            "-t", str(MAX_DURATION), "-fs", str(MAX_OUTPUT), "-progress", "pipe:1", "-nostats", str(video),
        ], 900, seconds)
        if video.stat().st_size >= MAX_OUTPUT:
            raise ValueError
        run(ffmpeg + ["-ss", str(min(2, seconds / 4)), "-threads", "2", "-i", str(video),
                      "-frames:v", "1", "-threads", "1", "-vf", "scale=640:-2",
                      str(directory / "preview.jpg")], 30)
        if not 0 < (directory / "preview.jpg").stat().st_size <= MAX_PREVIEW:
            raise ValueError
        return round(seconds)
    except subprocess.TimeoutExpired:
        raise HTTPException(503, "Истекло время обработки видео.")
    except subprocess.CalledProcessError:
        raise HTTPException(503, "FFmpeg завершился с ошибкой.")
    except (KeyError, StopIteration, ValueError, OverflowError, FileNotFoundError):
        raise HTTPException(422, ERROR)

