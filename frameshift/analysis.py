"""Face-aware shot analysis for the AI Landscape renderer."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Optional, Sequence

import cv2

from .scene_split import probe_duration, process_hybrid_mode
from .utils.crop import clamp_crop_to_bounds
from .utils.ffmpeg_ops import detect_audio_stream
from .utils.yunet import FaceCandidate, YuNetFaceDetector, select_largest_face


logger = logging.getLogger("frameshift.analysis")


class ValidationError(ValueError):
    pass


class ProcessingError(RuntimeError):
    pass


@dataclass
class VideoMetadata:
    width: int
    height: int
    fps: float
    frame_count: int
    duration_seconds: float
    has_audio: Optional[bool]
    is_portrait: bool


@dataclass(frozen=True)
class SceneSegment:
    scene_index: int
    start_seconds: float
    end_seconds: float
    start_frame: int
    end_frame: int
    start_pts: Optional[int] = None
    end_pts: Optional[int] = None


@dataclass(frozen=True)
class SceneCropPlan:
    scene_index: int
    start_seconds: float
    end_seconds: float
    start_frame: int
    end_frame: int
    center_x: float
    center_y: float
    fallback_reason: Optional[str]
    selected_sample_index: Optional[int]
    selected_candidate_index: Optional[int]
    sample_count: int
    face_x: Optional[float] = None
    face_y: Optional[float] = None
    face_width: Optional[float] = None
    face_height: Optional[float] = None
    right_eye_x: Optional[float] = None
    right_eye_y: Optional[float] = None
    left_eye_x: Optional[float] = None
    left_eye_y: Optional[float] = None
    start_pts: Optional[int] = None
    end_pts: Optional[int] = None


def open_video_capture(input_video: Path) -> cv2.VideoCapture:
    params = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 15000,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000,
    ]
    capture = cv2.VideoCapture(str(input_video), cv2.CAP_FFMPEG, params)
    if not capture.isOpened():
        capture.release()
        capture = cv2.VideoCapture(str(input_video))
    return capture


def read_video_metadata(input_video: Path, ffmpeg_binary: Optional[str]) -> VideoMetadata:
    capture = open_video_capture(input_video)
    if not capture.isOpened():
        raise ValidationError(f"Could not open input video: {input_video}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    capture.release()
    if width <= 0 or height <= 0 or frame_count <= 0 or fps <= 0:
        raise ValidationError(
            f"Invalid video metadata width={width}, height={height}, "
            f"frame_count={frame_count}, fps={fps}"
        )
    return VideoMetadata(
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        duration_seconds=frame_count / fps,
        has_audio=detect_audio_stream(input_video, ffmpeg_binary),
        is_portrait=height >= width,
    )


def build_scene_split_args(metadata: VideoMetadata):
    return SimpleNamespace(
        threshold=0.35,
        candidate_score=6.0,
        strong_score=18.0,
        burst_window=0.4,
        min_duration=0.8,
        strong_min_duration=0.4,
        supported_min_duration=1.5,
        support_window=0.4,
        silence_noise=-35.0,
        silence_duration=0.2,
        black_duration=0.15,
        black_pix_threshold=0.10,
        no_silence=metadata.has_audio is not True,
        no_black=False,
        enable_asr_speaker=False,
        whisper_model="medium",
        whisper_device="cpu",
        whisper_compute_type="int8",
        audio_sample_rate=16000,
        speaker_distance_threshold=0.22,
        speaker_mad_multiplier=2.5,
        speaker_min_segment_duration=0.8,
        speaker_margin=0.15,
        speaker_cut_min_duration=2.0,
    )


def build_exact_scene_segments(
    metadata: VideoMetadata,
    duration_seconds: float,
    accepted_candidates: Sequence,
    frame_metadata: dict,
) -> list[SceneSegment]:
    duration = max(0.0, float(duration_seconds))
    decoded_frame_count = int(frame_metadata.get("decoded_frame_count", 0) or 0)
    if decoded_frame_count <= 0:
        raise ProcessingError("场景检测没有返回 FFmpeg 实际解码帧序号")
    cuts = []
    seen = set()
    for accepted in accepted_candidates:
        candidate = accepted[0] if isinstance(accepted, tuple) else accepted
        frame_index = getattr(candidate, "frame_index", None)
        if frame_index is None:
            raise ProcessingError("场景边界缺少 FFmpeg 实际解码帧序号")
        frame_index = int(frame_index)
        if frame_index <= 0 or frame_index >= decoded_frame_count or frame_index in seen:
            continue
        seen.add(frame_index)
        cuts.append(candidate)
    cuts.sort(key=lambda candidate: int(candidate.frame_index))

    scenes: list[SceneSegment] = []
    start_seconds = 0.0
    start_frame = 0
    start_pts = frame_metadata.get("first_frame_pts")
    for candidate in cuts:
        end_frame = int(candidate.frame_index)
        end_seconds = max(start_seconds, min(duration, float(candidate.time)))
        if end_frame <= start_frame:
            raise ProcessingError("场景边界帧序号没有严格递增")
        scenes.append(SceneSegment(
            scene_index=len(scenes),
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            start_frame=start_frame,
            end_frame=end_frame,
            start_pts=int(start_pts) if start_pts is not None else None,
            end_pts=int(candidate.pts) if candidate.pts is not None else None,
        ))
        start_seconds = end_seconds
        start_frame = end_frame
        start_pts = candidate.pts
    if start_frame < decoded_frame_count:
        scenes.append(SceneSegment(
            scene_index=len(scenes),
            start_seconds=start_seconds,
            end_seconds=duration,
            start_frame=start_frame,
            end_frame=decoded_frame_count,
            start_pts=int(start_pts) if start_pts is not None else None,
            end_pts=None,
        ))
    if not scenes:
        raise ProcessingError("场景检测没有生成有效帧区间")
    return scenes


def detect_scene_segments(input_video: Path, metadata: VideoMetadata):
    try:
        duration = probe_duration(input_video)
        boundaries, stats = process_hybrid_mode(
            input_video, duration, build_scene_split_args(metadata)
        )
    except Exception as exc:
        raise ProcessingError(f"Scene split failed: {exc}") from exc
    del boundaries
    scenes = build_exact_scene_segments(
        metadata,
        duration,
        stats.get("accepted") or [],
        stats,
    )
    logger.info("Scene detection complete: %d scenes", len(scenes))
    return scenes, stats


def scene_sample_frame_indices(scene: SceneSegment, fps: float) -> list[int]:
    step = max(1, int(round(fps)))
    values = list(range(scene.start_frame, scene.end_frame, step))
    return values or [scene.start_frame]


def collect_scene_face_samples(
    input_video: Path,
    metadata: VideoMetadata,
    detector: YuNetFaceDetector,
    scenes: Sequence[SceneSegment],
    cancel_check=None,
    progress_callback=None,
    performance_stats=None,
):
    stats = performance_stats if isinstance(performance_stats, dict) else {}
    defaults = {
        "sample_extract_seconds": 0.0,
        "yunet_detect_seconds": 0.0,
        "sample_frames_planned": 0,
        "sample_frames_read": 0,
        "sample_frames_failed": 0,
        "detection_frame_count": 0,
        "face_detected_frame_count": 0,
        "face_candidate_count": 0,
    }
    for key, value in defaults.items():
        stats.setdefault(key, value)
    stats["sample_frames_planned"] = sum(
        len(scene_sample_frame_indices(scene, metadata.fps)) for scene in scenes
    )
    sampling_started = time.perf_counter()
    capture = open_video_capture(input_video)
    if not capture.isOpened():
        raise ProcessingError(f"Could not open input video for sampling: {input_video}")

    samples: list[dict] = []
    plans: list[SceneCropPlan] = []
    sample_index = 0
    try:
        for scene in scenes:
            if cancel_check and cancel_check():
                raise ProcessingError("Landscape analysis cancelled")
            best_candidate: Optional[FaceCandidate] = None
            best_sample_index = None
            best_candidate_index = None
            scene_sample_count = 0
            for frame_index in scene_sample_frame_indices(scene, metadata.fps):
                if cancel_check and cancel_check():
                    raise ProcessingError("Landscape analysis cancelled")
                extract_started = time.perf_counter()
                try:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                    read_ok, frame = capture.read()
                finally:
                    stats["sample_extract_seconds"] += time.perf_counter() - extract_started
                if not read_ok:
                    stats["sample_frames_failed"] += 1
                    candidates = []
                else:
                    stats["sample_frames_read"] += 1
                    stats["detection_frame_count"] += 1
                    detection_started = time.perf_counter()
                    try:
                        candidates = detector.detect(frame)
                    finally:
                        stats["yunet_detect_seconds"] += time.perf_counter() - detection_started
                    stats["face_candidate_count"] += len(candidates)
                    if candidates:
                        stats["face_detected_frame_count"] += 1
                selected_index = select_largest_face(candidates)
                selected = candidates[selected_index] if selected_index is not None else None
                if selected is not None and (
                    best_candidate is None or selected.area > best_candidate.area
                ):
                    best_candidate = selected
                    best_sample_index = sample_index
                    best_candidate_index = selected_index
                samples.append({
                    "sample_index": sample_index,
                    "scene_index": scene.scene_index,
                    "scene_start_seconds": scene.start_seconds,
                    "scene_end_seconds": scene.end_seconds,
                    "timestamp_seconds": frame_index / metadata.fps,
                    "frame_index": frame_index,
                    "candidates": [candidate.to_json() for candidate in candidates],
                    "selected_candidate_index": selected_index,
                    "selected_center_x": selected.center_x if selected else None,
                    "selected_center_y": selected.center_y if selected else None,
                    "fallback_reason": None if selected else "no_face",
                })
                sample_index += 1
                scene_sample_count += 1
                if progress_callback:
                    progress_callback({
                        "stage": "face_sampling",
                        "scene_index": scene.scene_index,
                        "scene_count": len(scenes),
                        "sample_count": sample_index,
                        "sample_total": stats["sample_frames_planned"],
                    })

            if best_candidate is None:
                values = {
                    "center_x": metadata.width / 2.0,
                    "center_y": metadata.height / 2.0,
                    "fallback_reason": "center_crop",
                    "face_x": None,
                    "face_y": None,
                    "face_width": None,
                    "face_height": None,
                    "right_eye_x": None,
                    "right_eye_y": None,
                    "left_eye_x": None,
                    "left_eye_y": None,
                }
            else:
                values = {
                    "center_x": best_candidate.center_x,
                    "center_y": best_candidate.center_y,
                    "fallback_reason": None,
                    "face_x": best_candidate.x,
                    "face_y": best_candidate.y,
                    "face_width": best_candidate.width,
                    "face_height": best_candidate.height,
                    "right_eye_x": best_candidate.right_eye_x,
                    "right_eye_y": best_candidate.right_eye_y,
                    "left_eye_x": best_candidate.left_eye_x,
                    "left_eye_y": best_candidate.left_eye_y,
                }
            plans.append(SceneCropPlan(
                scene_index=scene.scene_index,
                start_seconds=scene.start_seconds,
                end_seconds=scene.end_seconds,
                start_frame=scene.start_frame,
                end_frame=scene.end_frame,
                selected_sample_index=best_sample_index,
                selected_candidate_index=best_candidate_index,
                sample_count=scene_sample_count,
                start_pts=scene.start_pts,
                end_pts=scene.end_pts,
                **values,
            ))
    finally:
        capture.release()
        stats["face_sampling_wall_seconds"] = time.perf_counter() - sampling_started
    return samples, plans


def normalize_vertical_crop_bounds(
    metadata: VideoMetadata,
    crop_height: int,
    vertical_bounds: Optional[dict] = None,
):
    if not vertical_bounds:
        return 0, metadata.height
    try:
        basis_height = int(round(float(vertical_bounds.get("basis_height", metadata.height))))
        upper_value = float(vertical_bounds.get("upper_y", 0))
        lower_value = float(vertical_bounds.get("lower_y", basis_height))
    except (TypeError, ValueError) as exc:
        raise ProcessingError("AI横屏取景上下限无效") from exc
    if basis_height <= 0 or not math.isfinite(upper_value) or not math.isfinite(lower_value):
        raise ProcessingError("AI横屏取景上下限无效")
    scale_y = metadata.height / float(basis_height)
    upper_y = max(0, min(metadata.height, int(round(upper_value * scale_y))))
    lower_y = max(0, min(metadata.height, int(round(lower_value * scale_y))))
    if lower_y - upper_y < crop_height:
        raise ProcessingError(
            f"AI横屏取景范围高度为 {max(0, lower_y - upper_y)}px，"
            f"小于横屏裁剪所需的 {crop_height}px"
        )
    return upper_y, lower_y


def compute_scene_crop_rect(
    metadata: VideoMetadata,
    scene: SceneCropPlan,
    crop_width: int,
    crop_height: int,
    vertical_bounds: Optional[dict] = None,
):
    crop_x, crop_y, final_width, final_height = clamp_crop_to_bounds(
        scene.center_x,
        scene.center_y,
        crop_width,
        crop_height,
        metadata.width,
        metadata.height,
    )
    if scene.face_y is not None and scene.face_height is not None:
        if scene.face_height > crop_height:
            eyes = (scene.right_eye_y, scene.left_eye_y)
            if all(
                value is not None
                and math.isfinite(float(value))
                and 0 <= float(value) < metadata.height
                for value in eyes
            ):
                eye_center_y = (float(scene.right_eye_y) + float(scene.left_eye_y)) / 2
                crop_y = max(0, min(metadata.height - crop_height, int(round(eye_center_y - crop_height / 2))))
        elif scene.face_height <= crop_height / 3:
            crop_y = max(0, min(metadata.height - crop_height, int(round(scene.face_y - crop_height / 4))))
    upper_y, lower_y = normalize_vertical_crop_bounds(metadata, final_height, vertical_bounds)
    crop_y = max(upper_y, min(lower_y - final_height, crop_y))
    return crop_x, crop_y, final_width, final_height
