from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re

from .config import cache_root, ffmpeg_path, ffprobe_path
from .processes import run_command


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
EFFECT_EXTENSIONS = {".gif", ".mov", ".webm"}
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _ratio(value) -> float:
    text = str(value or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else 0.0
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _rotation(stream: dict) -> int:
    tags = stream.get("tags") or {}
    raw = tags.get("rotate")
    if raw not in (None, ""):
        try:
            return int(round(float(raw))) % 360
        except (TypeError, ValueError):
            pass
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            try:
                return int(round(float(item["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    return 0


def probe_video(path: str | Path) -> dict:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"视频不存在: {source}")
    if source.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"不支持的视频格式: {source.suffix}")
    command = [
        ffprobe_path(), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(source),
    ]
    returncode, stdout, stderr = run_command(command, timeout=30)
    if returncode != 0:
        raise RuntimeError(f"读取视频信息失败: {stderr.strip()[-500:]}")
    document = json.loads(stdout or "{}")
    streams = document.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("文件中没有视频流")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    encoded_width = int(video.get("width") or 0)
    encoded_height = int(video.get("height") or 0)
    rotation = _rotation(video)
    width, height = encoded_width, encoded_height
    if rotation in {90, 270}:
        width, height = height, width
    duration = _ratio(video.get("duration")) or _ratio((document.get("format") or {}).get("duration"))
    if duration <= 0:
        raise ValueError("视频时长无效")
    stat = source.stat()
    digest = hashlib.sha256(
        f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "id": digest,
        "path": str(source),
        "name": source.name,
        "duration": round(duration, 6),
        "width": width,
        "height": height,
        "encoded_width": encoded_width,
        "encoded_height": encoded_height,
        "rotation": rotation,
        "fps": _ratio(video.get("r_frame_rate")),
        "avg_fps": _ratio(video.get("avg_frame_rate")),
        "codec": str(video.get("codec_name") or ""),
        "pix_fmt": str(video.get("pix_fmt") or ""),
        "has_audio": bool(audio),
        "sample_rate": int((audio or {}).get("sample_rate") or 44100),
        "file_size": int(stat.st_size),
        "file_mtime": float(stat.st_mtime),
    }


def thumbnail_path(path: str | Path, time_seconds: float | None = None) -> tuple[str, Path]:
    source = Path(path).expanduser().resolve()
    stat = source.stat()
    timestamp = max(0.0, float(time_seconds if time_seconds is not None else 0.5))
    key = hashlib.sha256(
        f"{source}:{stat.st_size}:{stat.st_mtime_ns}:{timestamp:.3f}".encode("utf-8")
    ).hexdigest()[:32]
    directory = cache_root() / "thumbnails"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{key}.jpg"
    if not target.is_file() or target.stat().st_size == 0:
        command = [
            ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1",
            "-vf", "scale=320:-2", "-q:v", "3", str(target),
        ]
        returncode, _, stderr = run_command(command, timeout=30)
        if returncode != 0 or not target.is_file():
            raise RuntimeError(f"生成缩略图失败: {stderr.strip()[-400:]}")
    return key, target


def cached_thumbnail(key: str) -> Path:
    normalized = str(key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        raise ValueError("缩略图键无效")
    path = cache_root() / "thumbnails" / f"{normalized}.jpg"
    if not path.is_file():
        raise FileNotFoundError("缩略图不存在")
    return path


def video_mimetype(path: str | Path) -> str:
    guessed = mimetypes.guess_type(str(path))[0]
    return guessed or "application/octet-stream"


def natural_key(value: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def validate_output_dir(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise ValueError(f"输出目录不可用: {target}")
    probe = target / ".ai_landscape_write_test"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise PermissionError(f"输出目录没有写入权限: {target}") from exc
    return target


def finite_number(value, default=0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)
