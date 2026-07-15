"""FrameShift analysis cache and debug artifact integration."""

from __future__ import annotations

import csv
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Callable, Optional

from .analysis import (
    ProcessingError,
    SceneCropPlan,
    VideoMetadata,
    collect_scene_face_samples,
    compute_scene_crop_rect,
    detect_scene_segments,
    normalize_vertical_crop_bounds,
    read_video_metadata,
)
from .scene_split import configure_runtime
from .utils.crop import compute_largest_in_bounds_crop
from .utils.yunet import (
    DEFAULT_MAX_INPUT_HEIGHT,
    DEFAULT_MAX_INPUT_WIDTH,
    DEFAULT_NMS_THRESHOLD,
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TOP_K,
    YuNetFaceDetector,
    yunet_input_geometry,
)


ALGORITHM_VERSION = "ai-landscape-v1-yunet-540x960-s065-frame-index"
CROP_SWITCH_MODE = "frame_index"
DEFAULT_MODEL_NAME = "face_detection_yunet_2023mar.onnx"

FACE_DETECTION_DEFAULTS = {
    "score_threshold": DEFAULT_SCORE_THRESHOLD,
    "nms_threshold": DEFAULT_NMS_THRESHOLD,
    "top_k": DEFAULT_TOP_K,
    "max_input_width": DEFAULT_MAX_INPUT_WIDTH,
    "max_input_height": DEFAULT_MAX_INPUT_HEIGHT,
}


def default_model_path() -> Path:
    return Path(__file__).resolve().parent / "models" / DEFAULT_MODEL_NAME


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _analysis_cache_key(input_path: Path, model_path: Path) -> str:
    stat = input_path.stat()
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "path": str(input_path.resolve()),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "model_sha256": _sha256_file(model_path),
        "face_detection": FACE_DETECTION_DEFAULTS,
        "scene_defaults": {
            "candidate_score": 6.0,
            "strong_score": 18.0,
            "burst_window": 0.4,
            "min_duration": 0.8,
            "strong_min_duration": 0.4,
        },
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _face_detection_metadata(width: int, height: int) -> dict:
    return {
        **FACE_DETECTION_DEFAULTS,
        **yunet_input_geometry(
            width,
            height,
            max_input_width=DEFAULT_MAX_INPUT_WIDTH,
            max_input_height=DEFAULT_MAX_INPUT_HEIGHT,
        ),
    }


def _scene_from_json(item: dict) -> SceneCropPlan:
    fields = SceneCropPlan.__dataclass_fields__
    return SceneCropPlan(**{key: item.get(key) for key in fields})


def _metadata_from_json(item: dict) -> VideoMetadata:
    fields = VideoMetadata.__dataclass_fields__
    return VideoMetadata(**{key: item.get(key) for key in fields})


def _completed_process(command, runner):
    returncode, stdout, stderr = runner(command)
    result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    if returncode != 0:
        raise RuntimeError(str(stderr or stdout or "command failed").strip())
    return result


def analyze_video(
    input_video,
    ffmpeg_path,
    ffprobe_path,
    runner: Callable,
    cache_root,
    safe_input_path=None,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    performance_callback: Optional[Callable[[dict], None]] = None,
):
    """Analyze one portrait video and return fixed per-scene crop plans."""
    analysis_started = time.perf_counter()
    performance = {
        "status": "running",
        "cache_hit": False,
        "cache_lookup_seconds": 0.0,
        "metadata_seconds": 0.0,
        "scene_split_seconds": 0.0,
        "sample_extract_seconds": 0.0,
        "yunet_detect_seconds": 0.0,
        "face_sampling_wall_seconds": 0.0,
        "result_finalize_seconds": 0.0,
        "sample_frames_planned": 0,
        "sample_frames_read": 0,
        "sample_frames_failed": 0,
        "detection_frame_count": 0,
        "face_detected_frame_count": 0,
        "face_candidate_count": 0,
    }
    try:
        source_path = Path(input_video).resolve()
        read_path = Path(safe_input_path or source_path)
        model_path = default_model_path()
        if not model_path.is_file():
            raise ProcessingError(f"YuNet model not found: {model_path}")

        if progress_callback:
            progress_callback({"stage": "cache_lookup"})
        cache_started = time.perf_counter()
        try:
            cache_key = _analysis_cache_key(source_path, model_path)
            cache_dir = Path(cache_root)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / f"{cache_key}.json"
            if cache_path.is_file():
                try:
                    document = json.loads(cache_path.read_text(encoding="utf-8"))
                    metadata = _metadata_from_json(document["metadata"])
                    scene_plans = [
                        _scene_from_json(item) for item in document["scene_plans"]
                    ]
                    performance["cache_hit"] = True
                    performance["status"] = "completed"
                    if progress_callback:
                        progress_callback({"stage": "cache_hit"})
                    return {
                        "algorithm_version": ALGORITHM_VERSION,
                        "cache_key": cache_key,
                        "cache_hit": True,
                        "metadata": metadata,
                        "scene_plans": scene_plans,
                        "samples": list(document.get("samples") or []),
                        "crop_width": int(document["crop_width"]),
                        "crop_height": int(document["crop_height"]),
                        "scene_frame_metadata": dict(
                            document.get("scene_frame_metadata") or {}
                        ),
                        "face_detection": dict(
                            document.get("face_detection")
                            or _face_detection_metadata(metadata.width, metadata.height)
                        ),
                        "crop_switch_mode": CROP_SWITCH_MODE,
                        "performance": performance,
                    }
                except (OSError, ValueError, KeyError, TypeError):
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass
        finally:
            performance["cache_lookup_seconds"] = (
                time.perf_counter() - cache_started
            )

        if cancel_check and cancel_check():
            raise ProcessingError("Landscape analysis cancelled")
        configure_runtime(
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            command_runner=lambda command: _completed_process(command, runner),
        )
        if progress_callback:
            progress_callback({"stage": "metadata"})
        stage_started = time.perf_counter()
        try:
            metadata = read_video_metadata(read_path, ffmpeg_path)
        finally:
            performance["metadata_seconds"] = time.perf_counter() - stage_started
        if metadata.height <= metadata.width:
            raise ProcessingError("AI横屏仅支持竖屏源视频")

        if progress_callback:
            progress_callback({"stage": "scene_splitting"})
        stage_started = time.perf_counter()
        try:
            scenes, scene_stats = detect_scene_segments(read_path, metadata)
        finally:
            performance["scene_split_seconds"] = (
                time.perf_counter() - stage_started
            )
        if cancel_check and cancel_check():
            raise ProcessingError("Landscape analysis cancelled")

        detector = YuNetFaceDetector(model_path)
        face_detection = detector.input_metadata(metadata.width, metadata.height)
        samples, scene_plans = collect_scene_face_samples(
            read_path,
            metadata,
            detector,
            scenes,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            performance_stats=performance,
        )
        if progress_callback:
            progress_callback({"stage": "finalizing"})
        finalize_started = time.perf_counter()
        try:
            crop_width, crop_height = compute_largest_in_bounds_crop(
                metadata.width,
                metadata.height,
                16.0 / 9.0,
            )
            document = {
                "algorithm_version": ALGORITHM_VERSION,
                "cache_key": cache_key,
                "metadata": asdict(metadata),
                "scene_plans": [asdict(item) for item in scene_plans],
                "samples": samples,
                "face_detection": face_detection,
                "crop_width": crop_width,
                "crop_height": crop_height,
                "scene_frame_metadata": {
                    "decoded_frame_count": int(
                        scene_stats.get("decoded_frame_count", 0) or 0
                    ),
                    "first_frame_pts": scene_stats.get("first_frame_pts"),
                    "first_frame_time": scene_stats.get("first_frame_time"),
                    "last_frame_pts": scene_stats.get("last_frame_pts"),
                    "last_frame_time": scene_stats.get("last_frame_time"),
                },
                "crop_switch_mode": CROP_SWITCH_MODE,
            }
            temp_path = cache_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, cache_path)
        finally:
            performance["result_finalize_seconds"] = (
                time.perf_counter() - finalize_started
            )
        performance["status"] = "completed"
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "cache_key": cache_key,
            "cache_hit": False,
            "metadata": metadata,
            "scene_plans": scene_plans,
            "samples": samples,
            "crop_width": crop_width,
            "crop_height": crop_height,
            "scene_frame_metadata": dict(document["scene_frame_metadata"]),
            "face_detection": dict(face_detection),
            "crop_switch_mode": CROP_SWITCH_MODE,
            "performance": performance,
        }
    except Exception as exc:
        performance["status"] = (
            "cancelled" if "cancel" in str(exc).lower() else "error"
        )
        performance["error"] = str(exc)
        raise
    finally:
        total_seconds = time.perf_counter() - analysis_started
        performance["analysis_total_seconds"] = total_seconds
        measured_seconds = sum(float(performance.get(key, 0.0) or 0.0) for key in (
            "cache_lookup_seconds",
            "metadata_seconds",
            "scene_split_seconds",
            "sample_extract_seconds",
            "yunet_detect_seconds",
            "result_finalize_seconds",
        ))
        performance["analysis_overhead_seconds"] = max(
            0.0, total_seconds - measured_seconds
        )
        detection_count = int(performance.get("detection_frame_count", 0) or 0)
        performance["yunet_average_frame_seconds"] = (
            float(performance.get("yunet_detect_seconds", 0.0) or 0.0)
            / detection_count
            if detection_count > 0 else 0.0
        )
        for key, value in list(performance.items()):
            if key.endswith("_seconds") and isinstance(value, (int, float)):
                performance[key] = round(float(value), 6)
        if performance_callback:
            try:
                performance_callback(dict(performance))
            except Exception:
                pass


def scene_crop_records(analysis, vertical_bounds=None):
    metadata = analysis["metadata"]
    crop_width = int(analysis["crop_width"])
    crop_height = int(analysis["crop_height"])
    upper_y, lower_y = normalize_vertical_crop_bounds(
        metadata,
        crop_height,
        vertical_bounds,
    )
    records = []
    for scene in analysis["scene_plans"]:
        _, unconstrained_crop_y, _, _ = compute_scene_crop_rect(
            metadata,
            scene,
            crop_width,
            crop_height,
        )
        crop_x, crop_y, final_w, final_h = compute_scene_crop_rect(
            metadata,
            scene,
            crop_width,
            crop_height,
            vertical_bounds=vertical_bounds,
        )
        boundary_shift_y = int(crop_y) - int(unconstrained_crop_y)
        records.append({
            **asdict(scene),
            "crop_x": int(crop_x),
            "crop_y": int(crop_y),
            "crop_width": int(final_w),
            "crop_height": int(final_h),
            "crop_upper_y": int(upper_y),
            "crop_lower_y": int(lower_y),
            "unconstrained_crop_y": int(unconstrained_crop_y),
            "boundary_shift_y": boundary_shift_y,
            "boundary_constraint_applied": boundary_shift_y != 0,
        })
    return records


def write_debug_artifacts(
    debug_dir,
    input_video,
    analysis,
    status="completed",
    error="",
    vertical_bounds=None,
    cancel_check: Optional[Callable[[], bool]] = None,
):
    def ensure_not_cancelled():
        if cancel_check and cancel_check():
            raise ProcessingError("Landscape analysis cancelled")

    ensure_not_cancelled()
    directory = Path(debug_dir)
    directory.mkdir(parents=True, exist_ok=True)
    metadata = analysis["metadata"]
    scenes = scene_crop_records(analysis, vertical_bounds=vertical_bounds)
    detect_path = directory / "detect.json"
    trajectory_path = directory / "trajectory.csv"
    detect_temp_path = directory / "detect.json.tmp"
    trajectory_temp_path = directory / "trajectory.csv.tmp"
    detect_document = {
        "run": {
            "status": status,
            "algorithm_version": ALGORITHM_VERSION,
            "crop_switch_mode": CROP_SWITCH_MODE,
            "cache_key": analysis.get("cache_key", ""),
            "cache_hit": bool(analysis.get("cache_hit")),
            "failure_message": error or None,
        },
        "input_video": str(Path(input_video).resolve()),
        "video": asdict(metadata),
        "scene_frame_metadata": dict(
            analysis.get("scene_frame_metadata") or {}
        ),
        "face_detection": dict(analysis.get("face_detection") or {}),
        "crop_bounds": {
            "upper_y": scenes[0]["crop_upper_y"] if scenes else 0,
            "lower_y": scenes[0]["crop_lower_y"] if scenes else metadata.height,
            "minimum_height": int(analysis["crop_height"]),
        },
        "scenes": scenes,
        "samples": list(analysis.get("samples") or []),
    }
    columns = [
        "frame_index",
        "timestamp_seconds",
        "scene_index",
        "scene_start_seconds",
        "scene_end_seconds",
        "scene_start_frame",
        "scene_end_frame",
        "scene_start_pts",
        "scene_end_pts",
        "crop_x",
        "crop_y",
        "crop_width",
        "crop_height",
        "crop_upper_y",
        "crop_lower_y",
        "unconstrained_crop_y",
        "boundary_shift_y",
        "boundary_constraint_applied",
        "fallback_reason",
    ]
    try:
        with trajectory_temp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            row_count = 0
            for scene in scenes:
                ensure_not_cancelled()
                for frame_index in range(int(scene["start_frame"]), int(scene["end_frame"])):
                    if row_count % 256 == 0:
                        ensure_not_cancelled()
                    writer.writerow({
                        "frame_index": frame_index,
                        "timestamp_seconds": round(frame_index / metadata.fps, 6),
                        "scene_index": scene["scene_index"],
                        "scene_start_seconds": scene["start_seconds"],
                        "scene_end_seconds": scene["end_seconds"],
                        "scene_start_frame": scene["start_frame"],
                        "scene_end_frame": scene["end_frame"],
                        "scene_start_pts": scene.get("start_pts"),
                        "scene_end_pts": scene.get("end_pts"),
                        "crop_x": scene["crop_x"],
                        "crop_y": scene["crop_y"],
                        "crop_width": scene["crop_width"],
                        "crop_height": scene["crop_height"],
                        "crop_upper_y": scene["crop_upper_y"],
                        "crop_lower_y": scene["crop_lower_y"],
                        "unconstrained_crop_y": scene["unconstrained_crop_y"],
                        "boundary_shift_y": scene["boundary_shift_y"],
                        "boundary_constraint_applied": scene["boundary_constraint_applied"],
                        "fallback_reason": scene.get("fallback_reason") or "",
                    })
                    row_count += 1
        ensure_not_cancelled()
        detect_temp_path.write_text(
            json.dumps(detect_document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        ensure_not_cancelled()
        os.replace(trajectory_temp_path, trajectory_path)
        os.replace(detect_temp_path, detect_path)
    finally:
        for temp_path in (detect_temp_path, trajectory_temp_path):
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return {
        "detect_json": str(detect_path),
        "trajectory_csv": str(trajectory_path),
    }
