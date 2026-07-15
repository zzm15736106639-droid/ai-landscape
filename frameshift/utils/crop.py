"""Crop rectangle geometry used by face-aware reframing."""

from __future__ import annotations


def compute_largest_in_bounds_crop(frame_width: int, frame_height: int, aspect_ratio: float):
    if frame_width <= 0 or frame_height <= 0 or aspect_ratio <= 0:
        raise ValueError("Frame dimensions and aspect ratio must be positive")
    crop_width = frame_width
    crop_height = int(round(crop_width / aspect_ratio))
    if crop_height > frame_height:
        crop_height = frame_height
        crop_width = int(round(crop_height * aspect_ratio))
    return max(1, min(frame_width, crop_width)), max(1, min(frame_height, crop_height))


def clamp_crop_to_bounds(
    center_x: float,
    center_y: float,
    crop_width: int,
    crop_height: int,
    frame_width: int,
    frame_height: int,
):
    if crop_width <= 0 or crop_height <= 0:
        raise ValueError("Crop dimensions must be positive")
    crop_width = min(crop_width, frame_width)
    crop_height = min(crop_height, frame_height)
    x = max(0, min(frame_width - crop_width, int(round(center_x - crop_width / 2))))
    y = max(0, min(frame_height - crop_height, int(round(center_y - crop_height / 2))))
    return x, y, crop_width, crop_height
