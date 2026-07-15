from __future__ import annotations

import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import threading
import uuid

from .config import OUTPUT_HEIGHT, OUTPUT_WIDTH, cache_root, ffmpeg_path, font_root
from .processes import run_command


MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
MAX_TEXT_LENGTH = 1000
DEFAULT_CENTER_Y = 640.0
DEFAULT_FONT_SIZE = 44.0
MIN_FONT_SIZE = 18.0
MAX_FONT_SIZE = 120.0
DEFAULT_FONT_ID = "source_han_sans_sc_heavy"
SHADOW_DISTANCE_AT_DEFAULT_SIZE = 2.0
PREVIEW_CACHE_VERSION = 1

FONTS = {
    "source_han_sans_sc_heavy": {
        "label": "思源黑体 SC Heavy",
        "filename": "SourceHanSansSC-Heavy.otf",
        "family": "Source Han Sans SC Heavy",
        "format": "opentype",
        "mimetype": "font/otf",
    },
    "source_han_serif_sc_heavy": {
        "label": "思源宋体 SC Heavy",
        "filename": "SourceHanSerifSC-Heavy.otf",
        "family": "Source Han Serif SC Heavy",
        "format": "opentype",
        "mimetype": "font/otf",
    },
}

_TIME_RE = re.compile(
    r"^(?P<hours>\d{1,3}):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"[,.](?P<millis>\d{1,3})$"
)
_TIMING_RE = re.compile(
    r"^\s*(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})(?:\s+.*)?$"
)
_PREVIEW_LOCK = threading.Lock()


def subtitle_cache_dir() -> Path:
    path = cache_root() / "subtitles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def subtitle_preview_cache_dir() -> Path:
    path = cache_root() / "subtitle_previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def font_catalog() -> list[dict]:
    return [
        {
            "font_id": font_id,
            "label": spec["label"],
            "family": spec["family"],
            "format": spec["format"],
        }
        for font_id, spec in FONTS.items()
    ]


def resolve_font(font_id: str) -> tuple[Path, dict]:
    normalized = str(font_id or "").strip()
    spec = FONTS.get(normalized)
    if not spec:
        raise ValueError(f"字幕字体不受支持: {normalized or font_id}")
    path = font_root() / spec["filename"]
    if not path.is_file():
        raise FileNotFoundError(
            f"字幕字体资源缺失: {spec['label']}，请运行资源下载脚本"
        )
    return path.resolve(), dict(spec)


def decode_srt(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("字幕文件内容无效")
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ValueError("字幕文件不能超过 5MB")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return bytes(data).decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("字幕文件编码不受支持，请使用 UTF-8 或 GB18030")


def _time_seconds(token: str) -> float:
    match = _TIME_RE.match(str(token or "").strip())
    if not match:
        raise ValueError(f"字幕时间格式无效: {token}")
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    millis = int(match.group("millis").ljust(3, "0"))
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"字幕时间格式无效: {token}")
    return hours * 3600.0 + minutes * 60.0 + seconds + millis / 1000.0


def normalize_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_srt_text(text: str) -> list[dict]:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[dict] = []
    index = 0
    while index < len(lines):
        match = _TIMING_RE.match(lines[index])
        if not match:
            index += 1
            continue
        start = _time_seconds(match.group("start"))
        end = _time_seconds(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        plain_text = normalize_text(
            html.unescape(re.sub(r"<[^>]*>", "", "\n".join(text_lines)))
        )
        if end > start and plain_text:
            cues.append({"start": round(start, 3), "end": round(end, 3), "text": plain_text})
    if not cues:
        raise ValueError("SRT 文件中没有有效的字幕时间轴")
    cues.sort(key=lambda cue: (cue["start"], cue["end"]))
    return cues


def cache_subtitle_bytes(data: bytes, original_name: str) -> dict:
    name = Path(str(original_name or "").strip()).name
    if not name or Path(name).suffix.lower() != ".srt":
        raise ValueError("只支持 .srt 字幕文件")
    text = decode_srt(data)
    cues = parse_srt_text(text)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    subtitle_id = hashlib.sha256(normalized).hexdigest()
    target = subtitle_cache_dir() / f"{subtitle_id}.srt"
    if not target.is_file():
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_bytes(normalized)
        temp.replace(target)
    return {
        "subtitle_id": subtitle_id,
        "name": name,
        "stem": Path(name).stem,
        "cue_count": len(cues),
        "cues": cues,
    }


def cache_subtitle_file(path: str | Path) -> dict:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"字幕文件不存在: {source}")
    if source.stat().st_size > MAX_SUBTITLE_BYTES:
        raise ValueError("字幕文件不能超过 5MB")
    return cache_subtitle_bytes(source.read_bytes(), source.name)


def subtitle_path(subtitle_id: str) -> Path:
    normalized = str(subtitle_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("subtitle_id 无效")
    path = subtitle_cache_dir() / f"{normalized}.srt"
    if not path.is_file():
        raise FileNotFoundError("字幕缓存不存在，请重新上传字幕")
    return path


def normalize_layout(raw_layout, output_width=OUTPUT_WIDTH, output_height=OUTPUT_HEIGHT) -> dict:
    layout = raw_layout if isinstance(raw_layout, dict) else {}
    try:
        output_width = float(output_width)
        output_height = float(output_height)
        basis_width = float(layout.get("basis_width", OUTPUT_WIDTH) or OUTPUT_WIDTH)
        basis_height = float(layout.get("basis_height", OUTPUT_HEIGHT) or OUTPUT_HEIGHT)
        center_y = float(layout.get("center_y", DEFAULT_CENTER_Y))
        font_size = float(layout.get("font_size", DEFAULT_FONT_SIZE))
    except (TypeError, ValueError) as exc:
        raise ValueError("字幕位置或字号必须是数字") from exc
    values = (output_width, output_height, basis_width, basis_height, center_y, font_size)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("字幕位置或字号必须是有限数字")
    if min(output_width, output_height, basis_width, basis_height) <= 0:
        raise ValueError("字幕画布尺寸必须大于 0")
    scale_y = output_height / basis_height
    return {
        "center_x": round(output_width / 2.0, 3),
        "center_y": round(max(0.0, min(output_height, center_y * scale_y)), 3),
        "font_size": round(max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, font_size * scale_y)), 3),
        "basis_width": round(output_width, 3),
        "basis_height": round(output_height, 3),
    }


def normalize_style(raw_style) -> dict:
    if raw_style in (None, ""):
        style = {}
    elif isinstance(raw_style, dict):
        style = raw_style
    else:
        raise ValueError("字幕 style 必须是对象")
    font_id = str(style.get("font_id") or DEFAULT_FONT_ID).strip()
    if font_id not in FONTS:
        raise ValueError(f"字幕字体不受支持: {font_id}")
    try:
        outline = float(style.get("outline_width", 0) or 0)
        shadow = float(style.get("shadow_opacity_percent", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("字幕描边或阴影透明度必须是数字") from exc
    if not math.isfinite(outline) or not 0 <= outline <= 10:
        raise ValueError("字幕描边粗细必须在 0 到 10px 之间")
    if not math.isfinite(shadow) or not 0 <= shadow <= 100:
        raise ValueError("字幕阴影透明度必须在 0% 到 100% 之间")
    return {
        "font_id": font_id,
        "outline_width": round(outline, 3),
        "shadow_opacity_percent": round(shadow, 3),
    }


def apply_text_overrides(cues: list[dict], raw_overrides) -> tuple[list[dict], list[dict]]:
    normalized = [dict(cue) for cue in cues]
    if raw_overrides in (None, "", []):
        return normalized, []
    if not isinstance(raw_overrides, list):
        raise ValueError("cue_text_overrides 必须是数组")
    seen: set[int] = set()
    overrides: list[dict] = []
    for item in raw_overrides:
        if not isinstance(item, dict):
            raise ValueError("cue_text_overrides 每项必须是对象")
        cue_index = item.get("cue_index")
        if isinstance(cue_index, bool) or not isinstance(cue_index, int):
            raise ValueError("字幕 cue_index 必须是整数")
        if cue_index < 0 or cue_index >= len(normalized):
            raise ValueError(f"字幕 cue_index 越界: {cue_index}")
        if cue_index in seen:
            raise ValueError(f"字幕 cue_index 重复: {cue_index}")
        text = item.get("text")
        if not isinstance(text, str):
            raise ValueError(f"字幕 text 必须是字符串: cue {cue_index}")
        text = normalize_text(text)
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"字幕文字不能超过 {MAX_TEXT_LENGTH} 个字符: cue {cue_index}")
        seen.add(cue_index)
        normalized[cue_index]["text"] = text
        overrides.append({"cue_index": cue_index, "text": text})
    return normalized, overrides


def normalize_subtitle_configs(raw_configs, video_paths: list[str]) -> dict[str, dict]:
    if raw_configs in (None, "", []):
        return {}
    if not isinstance(raw_configs, list):
        raise ValueError("subtitle_configs 必须是数组")
    valid_paths = {str(Path(path).resolve()) for path in video_paths}
    result: dict[str, dict] = {}
    for raw in raw_configs:
        if not isinstance(raw, dict):
            raise ValueError("subtitle_configs 每项必须是对象")
        video_path = str(Path(str(raw.get("video_path") or "")).expanduser().resolve())
        if video_path not in valid_paths:
            raise ValueError(f"字幕目标视频不在任务列表中: {video_path}")
        if video_path in result:
            raise ValueError(f"同一视频重复配置字幕: {video_path}")
        source = subtitle_path(raw.get("subtitle_id"))
        cues = parse_srt_text(source.read_text(encoding="utf-8"))
        cues, overrides = apply_text_overrides(cues, raw.get("cue_text_overrides"))
        result[video_path] = {
            "video_path": video_path,
            "subtitle_id": source.stem,
            "cues": cues,
            "layout": normalize_layout(raw.get("layout")),
            "style": normalize_style(raw.get("style")),
            "cue_text_overrides": overrides,
        }
    return result


def clip_cues(cues: list[dict], duration: float) -> list[dict]:
    result = []
    for cue in cues:
        start = max(0.0, float(cue.get("start", 0) or 0))
        end = min(float(duration), float(cue.get("end", 0) or 0))
        text = normalize_text(cue.get("text"))
        if text and end - start >= 0.01:
            result.append({"start": start, "end": end, "text": text})
    return result


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, int(round(float(seconds or 0) * 100.0)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{fraction:02d}"


def _ass_escape(value: str) -> str:
    return normalize_text(value).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def ffmpeg_filter_path(path: str | Path) -> str:
    value = str(Path(path).resolve()).replace("\\", "/")
    return value.replace(":", r"\:").replace("'", r"\'")


def _ascii_font_copy(font_path: Path, directory: Path) -> Path:
    target = directory / f"subtitle_font{font_path.suffix.lower()}"
    if not target.is_file() or target.stat().st_size != font_path.stat().st_size:
        shutil.copy2(font_path, target)
    return target


def write_ass(
    directory: str | Path,
    subtitle_config: dict,
    cues: list[dict],
    *,
    prefix: str = "main_",
    output_width: int = OUTPUT_WIDTH,
    output_height: int = OUTPUT_HEIGHT,
) -> tuple[Path, Path, dict, dict]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    layout = normalize_layout(subtitle_config.get("layout"), output_width, output_height)
    style = normalize_style(subtitle_config.get("style"))
    source_font, font_spec = resolve_font(style["font_id"])
    font_path = _ascii_font_copy(source_font, directory)
    center_x = int(round(layout["center_x"]))
    center_y = int(round(layout["center_y"]))
    font_size = int(round(layout["font_size"]))
    outline = f"{style['outline_width']:.3f}".rstrip("0").rstrip(".") or "0"
    shadow_alpha = int(round(255 * (1 - style["shadow_opacity_percent"] / 100)))
    shadow_distance = (
        font_size * SHADOW_DISTANCE_AT_DEFAULT_SIZE / DEFAULT_FONT_SIZE
        if style["shadow_opacity_percent"] > 0
        else 0.0
    )
    shadow = f"{shadow_distance:.3f}".rstrip("0").rstrip(".") or "0"
    shadow_color = f"&H{shadow_alpha:02X}000000"
    ass_path = directory / f"{prefix}subtitles.ass"
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {output_width}\nPlayResY: {output_height}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_spec['family']},{font_size},&H00FFFFFF,&H00FFFFFF,"
        f"&H00000000,{shadow_color},0,0,0,0,100,100,0,0,1,{outline},{shadow},"
        "5,20,20,20,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    with ass_path.open("w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write(header)
        for cue in cues:
            override = rf"{{\an5\pos({center_x},{center_y})\fs{font_size}\q2}}"
            handle.write(
                f"Dialogue: 0,{_ass_timestamp(cue['start'])},{_ass_timestamp(cue['end'])},"
                f"Default,,0,0,0,,{override}{_ass_escape(cue['text'])}\n"
            )
    return ass_path, font_path, layout, style


def _preview_key(text: str, layout: dict, style: dict, font_path: Path) -> str:
    stat = font_path.stat()
    payload = {
        "version": PREVIEW_CACHE_VERSION,
        "text": text,
        "layout": layout,
        "style": style,
        "font_sha_hint": [stat.st_size, stat.st_mtime_ns],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def preview_path(cache_key: str) -> Path:
    key = str(cache_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise ValueError("subtitle preview cache_key 无效")
    path = subtitle_preview_cache_dir() / f"{key}.png"
    if not path.is_file():
        raise FileNotFoundError("字幕真实效果图不存在")
    return path


def render_preview(text: str, layout=None, style=None) -> dict:
    normalized_text = normalize_text(text)
    if not normalized_text:
        raise ValueError("字幕文字不能为空")
    if len(normalized_text) > MAX_TEXT_LENGTH:
        raise ValueError(f"字幕文字不能超过 {MAX_TEXT_LENGTH} 个字符")
    normalized_layout = normalize_layout(layout)
    normalized_style = normalize_style(style)
    source_font, _ = resolve_font(normalized_style["font_id"])
    key = _preview_key(normalized_text, normalized_layout, normalized_style, source_font)
    target = subtitle_preview_cache_dir() / f"{key}.png"
    if target.is_file() and target.stat().st_size > 0:
        return {"cache_key": key, "path": str(target), "generated": False}
    with _PREVIEW_LOCK:
        if target.is_file() and target.stat().st_size > 0:
            return {"cache_key": key, "path": str(target), "generated": False}
        work = subtitle_preview_cache_dir() / f"work-{uuid.uuid4().hex}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            ass_path, font_path, _, _ = write_ass(
                work,
                {"layout": normalized_layout, "style": normalized_style},
                [{"start": 0.0, "end": 1.0, "text": normalized_text}],
                prefix="preview_",
            )
            internal = work / "preview.png"
            ass_filter = (
                f"format=rgba,ass=filename='{ffmpeg_filter_path(ass_path)}':"
                f"fontsdir='{ffmpeg_filter_path(font_path.parent)}':alpha=1,format=rgba"
            )
            command = [
                ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "warning",
                "-f", "lavfi", "-i",
                f"color=c=black@0.0:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:d=1,format=rgba",
                "-vf", ass_filter, "-frames:v", "1", str(internal),
            ]
            returncode, _, stderr = run_command(command, timeout=30)
            if returncode != 0 or not internal.is_file():
                raise RuntimeError(f"字幕真实效果生成失败: {stderr.strip()[-500:]}")
            internal.replace(target)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return {"cache_key": key, "path": str(target), "generated": True}
