from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the real AI Landscape API smoke test.")
    parser.add_argument("input_video", type=Path)
    parser.add_argument("--output-root", type=Path, default=ROOT / ".test-output")
    parser.add_argument("--timeout", type=float, default=1800.0)
    return parser.parse_args()


def ffprobe_document(path: Path) -> dict:
    from backend.config import ffprobe_path

    command = [
        ffprobe_path(),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def validate_video_frames(path: Path, duration: float) -> list[dict]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not decode output video: {path}")
    checks = []
    try:
        for label, ratio in (("first", 0.02), ("middle", 0.5), ("last", 0.98)):
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, duration * ratio * 1000.0))
            ok, frame = capture.read()
            if not ok or frame is None:
                raise RuntimeError(f"Could not decode the {label} validation frame")
            mean = float(np.mean(frame))
            deviation = float(np.std(frame))
            if mean < 2.0 and deviation < 2.0:
                raise RuntimeError(f"The {label} validation frame is unexpectedly black")
            checks.append({
                "position": label,
                "mean": round(mean, 3),
                "standard_deviation": round(deviation, 3),
            })
    finally:
        capture.release()
    return checks


def validate_debug_files(debug_dir: Path) -> dict:
    detect_path = debug_dir / "detect.json"
    trajectory_path = debug_dir / "trajectory.csv"
    if not detect_path.is_file() or not trajectory_path.is_file():
        raise RuntimeError(f"Missing analysis debug files in {debug_dir}")
    document = json.loads(detect_path.read_text(encoding="utf-8"))
    if document.get("run", {}).get("status") != "completed":
        raise RuntimeError("detect.json does not report completed analysis")
    scenes = document.get("scenes") or []
    samples = document.get("samples") or []
    detected_samples = [item for item in samples if item.get("candidates")]
    face_scenes = [item for item in scenes if item.get("face_width")]
    if not scenes:
        raise RuntimeError("AI analysis produced no scenes")
    if not detected_samples or not face_scenes:
        raise RuntimeError("YuNet did not produce a usable face candidate")
    with trajectory_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise RuntimeError("trajectory.csv is empty")
    expected_first = int(scenes[0]["start_frame"])
    expected_last = int(scenes[-1]["end_frame"]) - 1
    if int(rows[0]["frame_index"]) != expected_first:
        raise RuntimeError("trajectory.csv does not begin at the first analyzed frame")
    if int(rows[-1]["frame_index"]) != expected_last:
        raise RuntimeError("trajectory.csv does not cover the final analyzed frame")
    return {
        "scene_count": len(scenes),
        "sample_count": len(samples),
        "face_sample_count": len(detected_samples),
        "face_scene_count": len(face_scenes),
        "trajectory_rows": len(rows),
        "cache_hit": bool(document.get("run", {}).get("cache_hit")),
        "crop_switch_mode": document.get("run", {}).get("crop_switch_mode"),
    }


def main() -> int:
    args = parse_args()
    source = args.input_video.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    output_dir = (args.output_root / run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ["AI_LANDSCAPE_USER_DATA_DIR"] = str(output_dir / "user-data")

    from backend.app import create_app

    app = create_app()
    client = app.test_client()
    response = client.post("/api/ai-landscape", json={
        "videos": [{"path": str(source)}],
        "output_dir": str(output_dir / "outputs"),
        "gpu_mode": "cpu",
        "workers": 1,
        "output_video_bitrate_k": 2300,
        "ai_crop_bounds": [],
        "subtitle_configs": [],
        "effect_all_template_ids": [],
        "effect_random_template_ids": [],
    })
    payload = response.get_json() or {}
    if response.status_code != 202:
        raise RuntimeError(f"Job submission failed ({response.status_code}): {payload}")
    job_id = payload["job_id"]
    observed_statuses = [payload["job"]["status"]]
    deadline = time.monotonic() + args.timeout
    job = payload["job"]
    while job["status"] not in {"done", "error", "cancelled"}:
        if time.monotonic() >= deadline:
            client.post(f"/api/jobs/{job_id}/cancel")
            raise TimeoutError(f"Smoke test job timed out after {args.timeout:.0f}s")
        time.sleep(0.5)
        poll = client.get(f"/api/jobs/{job_id}")
        if poll.status_code != 200:
            raise RuntimeError(f"Job polling failed: {poll.get_json()}")
        job = poll.get_json()["job"]
        if job["status"] != observed_statuses[-1]:
            observed_statuses.append(job["status"])
    if job["status"] != "done" or job["completed"] != 1 or job["failed"] != 0:
        raise RuntimeError(f"Smoke test job failed: {json.dumps(job, ensure_ascii=False)}")

    result = job["results"][0]
    output_path = Path(result["output_path"])
    debug_dir = Path(result["debug_dir"])
    media = ffprobe_document(output_path)
    video_stream = next(
        (item for item in media.get("streams", []) if item.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (item for item in media.get("streams", []) if item.get("codec_type") == "audio"),
        None,
    )
    if not video_stream or (video_stream.get("width"), video_stream.get("height")) != (1280, 720):
        raise RuntimeError("Output video is not 1280x720")
    if video_stream.get("sample_aspect_ratio") not in {"1:1", None}:
        raise RuntimeError("Output sample aspect ratio is not 1:1")
    if not audio_stream:
        raise RuntimeError("Output video does not contain an audio stream")
    source_duration = float(ffprobe_document(source).get("format", {}).get("duration") or 0)
    output_duration = float(media.get("format", {}).get("duration") or 0)
    if abs(output_duration - source_duration) > max(0.5, source_duration * 0.01):
        raise RuntimeError(
            f"Output duration drift is too large: source={source_duration}, output={output_duration}"
        )
    if list(output_dir.rglob("temp_video.mp4")):
        raise RuntimeError("Legacy temp_video.mp4 was generated")

    report = {
        "job_id": job_id,
        "statuses": observed_statuses,
        "output_path": str(output_path),
        "debug_dir": str(debug_dir),
        "wall_seconds": round(float(job["finished_at"]) - float(job["started_at"]), 3),
        "performance": job.get("performance") or {},
        "encoder": result.get("encoder"),
        "media": {
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "sample_aspect_ratio": video_stream.get("sample_aspect_ratio"),
            "video_codec": video_stream.get("codec_name"),
            "audio_codec": audio_stream.get("codec_name"),
            "source_duration": round(source_duration, 3),
            "output_duration": round(output_duration, 3),
        },
        "frame_checks": validate_video_frames(output_path, output_duration),
        "analysis": validate_debug_files(debug_dir),
    }
    report_path = output_dir / "smoke-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Smoke report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
