"""Small FFprobe helpers used during video analysis."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import Optional


def find_ffprobe(ffmpeg_path: Optional[str] = None) -> Optional[str]:
    resolved = shutil.which("ffprobe")
    if resolved:
        return resolved
    if ffmpeg_path:
        executable = "ffprobe.exe" if Path(ffmpeg_path).suffix.lower() == ".exe" else "ffprobe"
        candidate = Path(ffmpeg_path).with_name(executable)
        if candidate.is_file():
            return str(candidate)
    return None


def detect_audio_stream(input_video: Path | str, ffmpeg_path: Optional[str] = None) -> Optional[bool]:
    ffprobe = find_ffprobe(ffmpeg_path)
    if not ffprobe:
        return None
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(input_video),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return bool(result.stdout.strip()) if result.returncode == 0 else None
