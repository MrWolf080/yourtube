"""FFmpeg pipeline shared by the worker and the demo data generator."""

import json
import math
import subprocess

from fastapi import HTTPException

ERROR = "Не удалось обработать видео. Проверьте формат и размер файла."
MAX_OUTPUT = 512 * 1024**2
MAX_PREVIEW = 1024**2


def prepare_video(source, directory, run=None):
    if run is None:
        def run(command, timeout, on_progress=None):
            return subprocess.run(command, capture_output=True, check=True, timeout=timeout).stdout

    try:
        info = json.loads(run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries",
             "stream=index,codec_type:stream_disposition=attached_pic:format=duration,format_name",
             "-of", "json", str(source)], 20,
        ))
        formats = set(info["format"]["format_name"].split(","))
        if not formats.intersection({"mov", "mp4", "matroska", "webm", "avi", "mpegts", "mpeg", "flv", "ogg"}):
            raise ValueError
        index = next(stream["index"] for stream in info["streams"]
                     if stream["codec_type"] == "video" and not stream.get("disposition", {}).get("attached_pic"))
        seconds = float(info["format"]["duration"])
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
        ffmpeg = ["ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "2"]
        video = directory / "video.mp4"
        run(ffmpeg + [
            "-protocol_whitelist", "file", "-threads", "2", "-i", str(source),
            "-map", f"0:{index}", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", "scale=w='max(2,trunc(min(1920,iw)/2)*2)':h=-2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "veryfast", "-threads", "2", "-c:a", "aac", "-movflags", "+faststart",
            "-fs", str(MAX_OUTPUT), "-progress", "pipe:1", "-nostats", str(video),
        ], 900, seconds)
        if video.stat().st_size >= MAX_OUTPUT:
            raise ValueError
        run(ffmpeg + ["-ss", str(min(2, seconds / 4)), "-threads", "2", "-i", str(video),
                      "-frames:v", "1", "-threads", "1", "-vf", "scale=640:-2",
                      str(directory / "preview.jpg")], 30)
        if not 0 < (directory / "preview.jpg").stat().st_size <= MAX_PREVIEW:
            raise ValueError
        return round(seconds)
    except (subprocess.SubprocessError, KeyError, StopIteration, ValueError, OverflowError, FileNotFoundError):
        raise HTTPException(422, ERROR)

