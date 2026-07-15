from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


APP_NAME = "AI Landscape"
APP_VERSION = "0.1.0"
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 720
DEFAULT_VIDEO_BITRATE_K = 2300
MAX_WORKERS = 8


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    return Path(frozen_root).resolve() if frozen_root else project_root()


def user_data_root() -> Path:
    configured = os.environ.get("AI_LANDSCAPE_USER_DATA_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
    else:
        root = Path.home() / ".ai-landscape"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_root() -> Path:
    path = user_data_root() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def static_root() -> Path:
    return bundle_root() / "static"


def font_root() -> Path:
    candidates = [
        bundle_root() / "subtitle_fonts",
        project_root() / "assets" / "fonts",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def find_binary(name: str) -> str:
    env_name = f"AI_LANDSCAPE_{name.upper()}"
    configured = os.environ.get(env_name, "").strip()
    executable = f"{name}.exe" if sys.platform == "win32" else name
    candidates = [
        Path(configured) if configured else None,
        bundle_root() / executable,
        project_root() / "vendor" / "ffmpeg" / "bin" / executable,
        project_root() / executable,
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate.resolve())
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise FileNotFoundError(
        f"未找到 {executable}，请运行 scripts/fetch_third_party.ps1 或配置 {env_name}"
    )


def ffmpeg_path() -> str:
    return find_binary("ffmpeg")


def ffprobe_path() -> str:
    return find_binary("ffprobe")
