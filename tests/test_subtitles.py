from pathlib import Path

import pytest

from backend.subtitles import (
    DEFAULT_FONT_ID,
    clip_cues,
    normalize_layout,
    normalize_style,
    parse_srt_text,
    write_ass,
)


SRT = """1
00:00:00,500 --> 00:00:02,000
第一行
第二行

2
00:00:03,000 --> 00:00:07,000
下一条字幕
"""


def test_srt_parser_normalizes_to_single_line():
    cues = parse_srt_text(SRT)
    assert cues[0]["text"] == "第一行 第二行"
    assert cues[1]["start"] == 3.0


def test_cues_are_clipped_to_source_duration():
    cues = clip_cues(parse_srt_text(SRT), 4.0)
    assert cues[-1]["end"] == 4.0


def test_layout_always_centers_horizontally():
    layout = normalize_layout({
        "center_x": 12,
        "center_y": 360,
        "font_size": 44,
        "basis_width": 1280,
        "basis_height": 720,
    }, 1920, 1080)
    assert layout["center_x"] == 960
    assert layout["center_y"] == 540
    assert layout["font_size"] == 66


def test_style_validation():
    assert normalize_style({})["font_id"] == DEFAULT_FONT_ID
    with pytest.raises(ValueError):
        normalize_style({"outline_width": 11})
    with pytest.raises(ValueError):
        normalize_style({"shadow_opacity_percent": -1})


def test_ass_uses_proportional_shadow_distance(tmp_path: Path):
    config = {
        "layout": {"center_y": 640, "font_size": 88, "basis_width": 1280, "basis_height": 720},
        "style": {"font_id": DEFAULT_FONT_ID, "outline_width": 0, "shadow_opacity_percent": 100},
    }
    ass_path, _, _, _ = write_ass(
        tmp_path,
        config,
        [{"start": 0, "end": 1, "text": "测试字幕"}],
    )
    text = ass_path.read_text(encoding="utf-8-sig")
    assert ",0,4,5,20,20,20,1" in text
    assert r"\q2" in text
