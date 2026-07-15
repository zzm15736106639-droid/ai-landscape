import pytest

from backend.renderer import scene_frame_expression


def records(count=4):
    return [
        {"end_frame": (index + 1) * 10, "crop_x": index * 100, "crop_y": index * 20}
        for index in range(count)
    ]


def test_balanced_expression_uses_exact_boundaries():
    expression = scene_frame_expression(records(), "crop_x")
    assert expression == "if(lt(n,20),if(lt(n,10),0,100),if(lt(n,30),200,300))"
    assert "lt(n,10)" in expression
    assert "lt(n,20)" in expression
    assert "lt(n,30)" in expression


def test_expression_rejects_non_increasing_boundaries():
    with pytest.raises(ValueError, match="严格递增"):
        scene_frame_expression([
            {"end_frame": 10, "crop_x": 0},
            {"end_frame": 10, "crop_x": 2},
        ], "crop_x")


def test_large_expression_has_logarithmic_nesting():
    expression = scene_frame_expression(records(128), "crop_y")
    depth = 0
    maximum = 0
    for character in expression:
        if character == "(":
            depth += 1
            maximum = max(maximum, depth)
        elif character == ")":
            depth -= 1
    assert maximum < 20
