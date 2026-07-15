from frameshift.utils.yunet import DEFAULT_SCORE_THRESHOLD, yunet_input_geometry


def test_yunet_defaults():
    assert DEFAULT_SCORE_THRESHOLD == 0.65
    assert yunet_input_geometry(1080, 1920)["input_width"] == 540
    assert yunet_input_geometry(1080, 1920)["input_height"] == 960


def test_yunet_resize_preserves_aspect_ratio_and_never_upscales():
    assert yunet_input_geometry(720, 900)["input_width"] == 540
    assert yunet_input_geometry(720, 900)["input_height"] == 675
    small = yunet_input_geometry(360, 640)
    assert (small["input_width"], small["input_height"]) == (360, 640)
    assert small["resized"] is False
