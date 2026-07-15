"""Compatibility exports for the original FrameShift module path."""

from .analysis import (
    ProcessingError,
    SceneCropPlan,
    SceneSegment,
    ValidationError,
    VideoMetadata,
    collect_scene_face_samples,
    compute_scene_crop_rect,
    detect_scene_segments,
    normalize_vertical_crop_bounds,
    read_video_metadata,
)

__all__ = [
    "ProcessingError",
    "SceneCropPlan",
    "SceneSegment",
    "ValidationError",
    "VideoMetadata",
    "collect_scene_face_samples",
    "compute_scene_crop_rect",
    "detect_scene_segments",
    "normalize_vertical_crop_bounds",
    "read_video_metadata",
]
