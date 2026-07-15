"""OpenCV YuNet face detection helpers for the landscape reframe MVP."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np


logger = logging.getLogger("frameshift.utils.yunet")

DEFAULT_SCORE_THRESHOLD = 0.65
DEFAULT_NMS_THRESHOLD = 0.3
DEFAULT_TOP_K = 5000
DEFAULT_MAX_INPUT_WIDTH = 540
DEFAULT_MAX_INPUT_HEIGHT = 960


class YuNetUnavailableError(RuntimeError):
    """Raised when OpenCV YuNet support or the model file is unavailable."""


@dataclass(frozen=True)
class FaceCandidate:
    x: float
    y: float
    width: float
    height: float
    center_x: float
    center_y: float
    confidence: Optional[float]
    area: float
    right_eye_x: Optional[float] = None
    right_eye_y: Optional[float] = None
    left_eye_x: Optional[float] = None
    left_eye_y: Optional[float] = None

    @property
    def eye_center_y(self) -> Optional[float]:
        if self.right_eye_y is None or self.left_eye_y is None:
            return None
        return (self.right_eye_y + self.left_eye_y) / 2.0

    def to_json(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "confidence": self.confidence,
            "area": self.area,
            "right_eye_x": self.right_eye_x,
            "right_eye_y": self.right_eye_y,
            "left_eye_x": self.left_eye_x,
            "left_eye_y": self.left_eye_y,
            "eye_center_y": self.eye_center_y,
        }


def ensure_yunet_capability(cv_module=cv2) -> None:
    """Validate that the installed OpenCV build exposes FaceDetectorYN."""
    if not hasattr(cv_module, "FaceDetectorYN"):
        raise YuNetUnavailableError(
            "Installed OpenCV does not expose cv2.FaceDetectorYN; install opencv-python>=4.8.0."
        )
    if not hasattr(cv_module.FaceDetectorYN, "create"):
        raise YuNetUnavailableError(
            "Installed OpenCV FaceDetectorYN does not expose create(); install opencv-python>=4.8.0."
        )


def resolve_yunet_model(model_path: Path | str) -> Path:
    """Return an existing YuNet model path or raise a clear error."""
    path = Path(model_path).expanduser()
    if not path.is_file():
        raise YuNetUnavailableError(f"YuNet model file not found: {path}")
    return path


def select_largest_face(candidates: List[FaceCandidate]) -> Optional[int]:
    """Return the index of the largest candidate by area."""
    if not candidates:
        return None
    return max(range(len(candidates)), key=lambda idx: candidates[idx].area)


def yunet_input_geometry(
    width: int,
    height: int,
    max_input_width: int = DEFAULT_MAX_INPUT_WIDTH,
    max_input_height: int = DEFAULT_MAX_INPUT_HEIGHT,
) -> dict:
    """Return the aspect-preserving YuNet input geometry for a source frame."""
    width = int(width)
    height = int(height)
    max_input_width = int(max_input_width)
    max_input_height = int(max_input_height)
    if width <= 0 or height <= 0:
        raise ValueError("YuNet source dimensions must be positive")
    if max_input_width <= 0 or max_input_height <= 0:
        raise ValueError("YuNet maximum input dimensions must be positive")

    requested_scale = min(
        1.0,
        max_input_width / float(width),
        max_input_height / float(height),
    )
    if requested_scale >= 1.0:
        input_width = width
        input_height = height
    else:
        input_width = min(
            max_input_width,
            max(1, int(round(width * requested_scale))),
        )
        input_height = min(
            max_input_height,
            max(1, int(round(height * requested_scale))),
        )

    return {
        "source_width": width,
        "source_height": height,
        "input_width": input_width,
        "input_height": input_height,
        "scale_x": input_width / float(width),
        "scale_y": input_height / float(height),
        "resized": input_width != width or input_height != height,
    }


class YuNetFaceDetector:
    """YuNet wrapper that detects on bounded frames and returns source coordinates."""

    def __init__(
        self,
        model_path: Path | str,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        nms_threshold: float = DEFAULT_NMS_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
        max_input_width: int = DEFAULT_MAX_INPUT_WIDTH,
        max_input_height: int = DEFAULT_MAX_INPUT_HEIGHT,
    ) -> None:
        ensure_yunet_capability()
        self.model_path = resolve_yunet_model(model_path)
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        self.top_k = int(top_k)
        self.max_input_width = int(max_input_width)
        self.max_input_height = int(max_input_height)
        if not math.isfinite(self.score_threshold) or not 0.0 <= self.score_threshold <= 1.0:
            raise ValueError("YuNet score threshold must be between 0 and 1")
        if self.max_input_width <= 0 or self.max_input_height <= 0:
            raise ValueError("YuNet maximum input dimensions must be positive")
        try:
            model_buffer = self.model_path.read_bytes()
        except OSError as exc:
            raise YuNetUnavailableError(f"无法读取 YuNet 模型文件: {exc}") from exc
        if not model_buffer:
            raise YuNetUnavailableError(f"YuNet 模型文件为空: {self.model_path}")
        try:
            # OpenCV's Windows ONNX path reader cannot reliably open Unicode paths.
            self._detector = cv2.FaceDetectorYN.create(
                "onnx",
                model_buffer,
                b"",
                (320, 320),
                self.score_threshold,
                self.nms_threshold,
                self.top_k,
            )
        except Exception as exc:
            raise YuNetUnavailableError(f"YuNet 模型解析失败: {exc}") from exc

    def input_metadata(self, width: int, height: int) -> dict:
        geometry = yunet_input_geometry(
            width,
            height,
            max_input_width=self.max_input_width,
            max_input_height=self.max_input_height,
        )
        return {
            "score_threshold": self.score_threshold,
            "nms_threshold": self.nms_threshold,
            "top_k": self.top_k,
            "max_input_width": self.max_input_width,
            "max_input_height": self.max_input_height,
            **geometry,
        }

    def detect(self, frame: np.ndarray) -> List[FaceCandidate]:
        if frame is None or frame.size == 0:
            return []

        height, width = frame.shape[:2]
        geometry = self.input_metadata(width, height)
        input_width = int(geometry["input_width"])
        input_height = int(geometry["input_height"])
        scale_x = float(geometry["scale_x"])
        scale_y = float(geometry["scale_y"])
        detection_frame = frame
        if geometry["resized"]:
            detection_frame = cv2.resize(
                frame,
                (input_width, input_height),
                interpolation=cv2.INTER_AREA,
            )

        self._detector.setInputSize((input_width, input_height))
        _, faces = self._detector.detect(detection_frame)
        if faces is None:
            return []

        candidates: List[FaceCandidate] = []
        for face in faces:
            x, y, w, h = [float(v) for v in face[:4]]
            confidence = float(face[-1]) if len(face) else None
            right_eye_x = right_eye_y = None
            left_eye_x = left_eye_y = None
            if len(face) >= 8:
                eye_values = [float(v) for v in face[4:8]]
                if (
                    all(np.isfinite(value) for value in eye_values)
                    and 0.0 <= eye_values[0] < input_width
                    and 0.0 <= eye_values[1] < input_height
                    and 0.0 <= eye_values[2] < input_width
                    and 0.0 <= eye_values[3] < input_height
                ):
                    right_eye_x = eye_values[0] / scale_x
                    right_eye_y = eye_values[1] / scale_y
                    left_eye_x = eye_values[2] / scale_x
                    left_eye_y = eye_values[3] / scale_y
            x /= scale_x
            y /= scale_y
            w /= scale_x
            h /= scale_y
            x = max(0.0, min(x, width - 1.0))
            y = max(0.0, min(y, height - 1.0))
            w = max(0.0, min(w, width - x))
            h = max(0.0, min(h, height - y))
            if w <= 0 or h <= 0:
                continue
            candidates.append(
                FaceCandidate(
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    center_x=x + w / 2.0,
                    center_y=y + h / 2.0,
                    confidence=confidence,
                    area=w * h,
                    right_eye_x=right_eye_x,
                    right_eye_y=right_eye_y,
                    left_eye_x=left_eye_x,
                    left_eye_y=left_eye_y,
                )
            )
        logger.debug("YuNet detected %d face candidates", len(candidates))
        return candidates
