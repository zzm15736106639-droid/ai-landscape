from types import SimpleNamespace

from frameshift.analysis import (
    SceneCropPlan,
    VideoMetadata,
    build_exact_scene_segments,
    compute_scene_crop_rect,
)
from frameshift.utils.crop import compute_largest_in_bounds_crop


def metadata():
    return VideoMetadata(
        width=1080,
        height=1920,
        fps=30.0,
        frame_count=300,
        duration_seconds=10.0,
        has_audio=True,
        is_portrait=True,
    )


def scene(**overrides):
    values = {
        "scene_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 10.0,
        "start_frame": 0,
        "end_frame": 300,
        "center_x": 540,
        "center_y": 1000,
        "fallback_reason": None,
        "selected_sample_index": 0,
        "selected_candidate_index": 0,
        "sample_count": 1,
        "face_x": 200,
        "face_y": 100,
        "face_width": 680,
        "face_height": 1000,
        "right_eye_y": 400,
        "left_eye_y": 500,
    }
    values.update(overrides)
    return SceneCropPlan(**values)


def test_oversized_face_uses_eye_center():
    crop_width, crop_height = compute_largest_in_bounds_crop(1080, 1920, 16 / 9)
    _, crop_y, _, _ = compute_scene_crop_rect(metadata(), scene(), crop_width, crop_height)
    assert crop_height == 608
    assert crop_y == 146


def test_vertical_bounds_shift_full_crop_without_clipping():
    crop_width, crop_height = compute_largest_in_bounds_crop(1080, 1920, 16 / 9)
    bounds = {"upper_y": 300, "lower_y": 1200, "basis_height": 1920}
    _, crop_y, _, final_height = compute_scene_crop_rect(
        metadata(), scene(), crop_width, crop_height, bounds
    )
    assert crop_y == 300
    assert final_height == crop_height
    assert crop_y + final_height <= 1200


def test_small_face_places_top_near_quarter_height():
    crop_width, crop_height = compute_largest_in_bounds_crop(1080, 1920, 16 / 9)
    target = scene(face_y=500, face_height=100, center_y=550, right_eye_y=520, left_eye_y=520)
    _, crop_y, _, _ = compute_scene_crop_rect(metadata(), target, crop_width, crop_height)
    assert crop_y == round(500 - crop_height / 4)


def test_exact_scene_boundary_frame_belongs_to_new_scene():
    first_cut = SimpleNamespace(frame_index=90, time=3.0, pts=9000)
    second_cut = SimpleNamespace(frame_index=180, time=6.0, pts=18000)
    scenes = build_exact_scene_segments(
        metadata(),
        10.0,
        [first_cut, second_cut],
        {"decoded_frame_count": 300, "first_frame_pts": 0},
    )
    assert [(item.start_frame, item.end_frame) for item in scenes] == [
        (0, 90), (90, 180), (180, 300),
    ]
