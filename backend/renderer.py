from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid

from frameshift.integration import scene_crop_records

from .config import OUTPUT_HEIGHT, OUTPUT_WIDTH, ffmpeg_path
from .effects import ensure_effects
from .processes import CommandCancelled, run_command
from .subtitles import clip_cues, ffmpeg_filter_path, write_ass


def scene_frame_expression(records: list[dict], field: str, frame_expression: str = "n") -> str:
    """Build a balanced expression over exact decoded frame indexes."""
    if not records:
        raise ValueError("AI横屏缺少有效镜头裁剪数据")
    normalized: list[tuple[int, int]] = []
    previous_end = -1
    for scene in records:
        try:
            end_frame = int(scene["end_frame"])
            value = int(scene[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("AI横屏镜头裁剪数据无效") from exc
        if end_frame <= previous_end:
            raise ValueError("AI横屏镜头结束帧必须严格递增")
        normalized.append((end_frame, value))
        previous_end = end_frame

    def build(start: int, end: int) -> str:
        if end - start == 1:
            return str(normalized[start][1])
        middle = (start + end) // 2
        boundary = normalized[middle - 1][0]
        return (
            f"if(lt({frame_expression},{boundary}),"
            f"{build(start, middle)},{build(middle, end)})"
        )

    return build(0, len(normalized))


def _safe_name(value: str, fallback: str = "video") -> str:
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or "")).strip(" ._")
    return normalized[:120] or fallback


def _link_or_copy(source: Path, target: Path) -> Path:
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target


def _safe_input(source: str | Path, directory: Path, stem: str) -> Path:
    path = Path(source).resolve()
    return _link_or_copy(path, directory / f"{stem}{path.suffix.lower()}")


def _filter_script(directory: Path, steps: list[str]) -> Path:
    path = directory / "filter.ffscript"
    path.write_text(";\n".join(steps), encoding="utf-8", newline="\n")
    return path


def _encoder_arguments(gpu_mode: str, bitrate_k: int, encoder: str) -> list[str]:
    bitrate = f"{int(bitrate_k)}k"
    if encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-b:v", bitrate, "-maxrate", bitrate,
            "-bufsize", f"{int(bitrate_k) * 2}k", "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", "libx264", "-preset", "medium", "-b:v", bitrate,
        "-maxrate", bitrate, "-bufsize", f"{int(bitrate_k) * 2}k",
        "-pix_fmt", "yuv420p",
    ]


_ENCODER_LOCK = threading.Lock()
_NVENC_LISTED: bool | None = None


def nvenc_listed() -> bool:
    global _NVENC_LISTED
    with _ENCODER_LOCK:
        if _NVENC_LISTED is not None:
            return _NVENC_LISTED
        returncode, stdout, stderr = run_command(
            [ffmpeg_path(), "-hide_banner", "-encoders"], timeout=20
        )
        _NVENC_LISTED = returncode == 0 and "h264_nvenc" in f"{stdout}\n{stderr}"
        return _NVENC_LISTED


def encoder_candidates(gpu_mode: str) -> list[str]:
    normalized = str(gpu_mode or "auto").strip().lower()
    if normalized not in {"auto", "gpu", "cpu"}:
        raise ValueError("gpu_mode 只能是 auto、gpu 或 cpu")
    if normalized == "cpu":
        return ["libx264"]
    if normalized == "gpu":
        if not nvenc_listed():
            raise RuntimeError("当前 FFmpeg 不包含 h264_nvenc 编码器")
        return ["h264_nvenc"]
    return ["h264_nvenc", "libx264"] if nvenc_listed() else ["libx264"]


def _build_filter(
    directory: Path,
    analysis: dict,
    vertical_bounds: dict | None,
    effects: list[dict],
    subtitle_config: dict | None,
    duration: float,
) -> tuple[Path, dict]:
    records = scene_crop_records(analysis, vertical_bounds=vertical_bounds)
    if not records:
        raise ValueError("AI横屏没有生成有效镜头")
    crop_width = int(analysis["crop_width"])
    crop_height = int(analysis["crop_height"])
    x_expression = scene_frame_expression(records, "crop_x")
    y_expression = scene_frame_expression(records, "crop_y")
    steps = [
        "[0:v:0]"
        f"crop=w={crop_width}:h={crop_height}:"
        f"x='{x_expression}':y='{y_expression}',"
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=lanczos,"
        "setsar=1,setpts=PTS-STARTPTS[canvas]"
    ]
    stage = "canvas"
    for index, _effect in enumerate(effects, start=1):
        overlay = f"overlay_{index}"
        output = f"effect_{index}"
        steps.append(
            f"[{index}:v:0]setpts=PTS-STARTPTS,"
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=lanczos,setsar=1[{overlay}]"
        )
        steps.append(f"[{stage}][{overlay}]overlay=0:0:shortest=1:format=auto[{output}]")
        stage = output

    subtitle_details = {
        "enabled": False,
        "cue_count": 0,
        "font_id": "",
        "font_size": 0,
        "center_y": 0,
        "outline_width": 0,
        "shadow_opacity_percent": 0,
    }
    if subtitle_config:
        cues = clip_cues(subtitle_config.get("cues") or [], duration)
        if cues:
            ass_path, font_path, layout, style = write_ass(
                directory, subtitle_config, cues, prefix="output_"
            )
            output = "subtitles"
            steps.append(
                f"[{stage}]ass=filename='{ffmpeg_filter_path(ass_path)}':"
                f"fontsdir='{ffmpeg_filter_path(font_path.parent)}'[{output}]"
            )
            stage = output
            subtitle_details = {
                "enabled": True,
                "cue_count": len(cues),
                "font_id": style["font_id"],
                "font_size": layout["font_size"],
                "center_y": layout["center_y"],
                "outline_width": style["outline_width"],
                "shadow_opacity_percent": style["shadow_opacity_percent"],
            }
    steps.append(f"[{stage}]format=yuv420p[final_video]")
    return _filter_script(directory, steps), {
        "scene_count": len(records),
        "crop_switch_mode": "frame_index",
        "crop_width": crop_width,
        "crop_height": crop_height,
        "subtitle": subtitle_details,
    }


def render_output(
    source_meta: dict,
    analysis: dict,
    output_path: str | Path,
    *,
    effect_ids: list[str] | None = None,
    subtitle_config: dict | None = None,
    vertical_bounds: dict | None = None,
    gpu_mode: str = "auto",
    bitrate_k: int = 2300,
    job_id: str = "",
    cancel_event: threading.Event | None = None,
    debug_dir: str | Path | None = None,
) -> dict:
    started = time.perf_counter()
    source = Path(str(source_meta.get("path") or "")).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"源视频不存在: {source}")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    effect_map = ensure_effects(effect_ids or [])
    effect_items = [effect_map[item] for item in dict.fromkeys(effect_ids or []) if item in effect_map]
    work = Path(tempfile.mkdtemp(prefix="ai_landscape_render_"))
    internal_output = work / "output.mp4"
    try:
        safe_source = _safe_input(source, work, "source")
        safe_effects = []
        for index, item in enumerate(effect_items, start=1):
            copied = _safe_input(item["path"], work, f"effect_{index}")
            safe_effects.append({**item, "path": str(copied)})
        script, details = _build_filter(
            work,
            analysis,
            vertical_bounds,
            safe_effects,
            subtitle_config,
            float(source_meta.get("duration") or 0),
        )
        if debug_dir:
            debug_path = Path(debug_dir)
            debug_path.mkdir(parents=True, exist_ok=True)
            shutil.copy2(script, debug_path / "filter.ffscript")

        base = [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "warning", "-i", str(safe_source)]
        for item in safe_effects:
            base.extend(["-stream_loop", "-1", "-i", item["path"]])
        base.extend([
            "-filter_complex_script", str(script),
            "-map", "[final_video]", "-map", "0:a:0?",
        ])

        attempts = []
        last_error = ""
        used_encoder = ""
        for encoder in encoder_candidates(gpu_mode):
            if cancel_event and cancel_event.is_set():
                raise CommandCancelled("任务已取消")
            internal_output.unlink(missing_ok=True)
            command = [
                *base,
                *_encoder_arguments(gpu_mode, bitrate_k, encoder),
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(internal_output),
            ]
            attempt_started = time.perf_counter()
            returncode, _, stderr = run_command(
                command,
                job_id=job_id,
                cancel_event=cancel_event,
                timeout=max(300.0, float(source_meta.get("duration") or 0) * 20.0),
            )
            attempts.append({
                "encoder": encoder,
                "returncode": returncode,
                "seconds": round(time.perf_counter() - attempt_started, 3),
                "error_tail": stderr.strip()[-1000:] if returncode else "",
            })
            if returncode == 0 and internal_output.is_file() and internal_output.stat().st_size > 1024:
                used_encoder = encoder
                break
            last_error = stderr.strip()[-1200:]
            if str(gpu_mode).lower() == "gpu":
                break
        if not used_encoder:
            raise RuntimeError(f"AI横屏编码失败: {last_error or '输出文件缺失'}")
        staged = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        shutil.move(str(internal_output), staged)
        os.replace(staged, output)
        if debug_dir:
            (Path(debug_dir) / "encode.json").write_text(
                json.dumps(
                    {
                        "source": str(source),
                        "output": str(output),
                        "effects": [item["id"] for item in effect_items],
                        "encoder": used_encoder,
                        "attempts": attempts,
                        **details,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return {
            **details,
            "output_path": str(output),
            "output_width": OUTPUT_WIDTH,
            "output_height": OUTPUT_HEIGHT,
            "output_sar": "1:1",
            "effect_ids": [item["id"] for item in effect_items],
            "effect_names": [item["name"] for item in effect_items],
            "encoder": used_encoder,
            "attempts": attempts,
            "encoding_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
