#!/usr/bin/env python3

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"
COMMAND_RUNNER = None


def configure_runtime(ffmpeg_path=None, ffprobe_path=None, command_runner=None):
    """Inject binary paths and a cancellable subprocess runner."""
    global FFMPEG_BIN, FFPROBE_BIN, COMMAND_RUNNER
    if ffmpeg_path:
        FFMPEG_BIN = str(ffmpeg_path)
    if ffprobe_path:
        FFPROBE_BIN = str(ffprobe_path)
    COMMAND_RUNNER = command_runner


SCENE_TIME_RE = re.compile(r"pts_time:([+-]?\d+(?:\.\d+)?)")
FRAME_INDEX_RE = re.compile(r"\bframe:\s*(\d+)")
FRAME_PTS_RE = re.compile(r"\bpts:\s*(-?\d+)")
FRAME_TIME_RE = re.compile(r"pts_time:([+-]?\d+(?:\.\d+)?)")
SCD_SCORE_RE = re.compile(r"lavfi\.scd\.score=(\d+(?:\.\d+)?)")
SILENCE_START_RE = re.compile(r"silence_start: (\d+(?:\.\d+)?)")
SILENCE_END_RE = re.compile(r"silence_end: (\d+(?:\.\d+)?)")
BLACK_START_RE = re.compile(r"black_start:(\d+(?:\.\d+)?)")
BLACK_END_RE = re.compile(r"black_end:(\d+(?:\.\d+)?)")
WHISPER_MODEL_CACHE = {}


@dataclass
class CandidateCut:
    time: float
    score: float
    kind: str = "scene"
    strong: bool = False
    supported: bool = False
    frame_index: Optional[int] = None
    pts: Optional[int] = None


def run_command(command):
    command = [
        FFMPEG_BIN if part == "ffmpeg" else FFPROBE_BIN if part == "ffprobe" else part
        for part in command
    ]
    if COMMAND_RUNNER:
        return COMMAND_RUNNER(command)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "command failed")
    return result


def probe_duration(video_path):
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
    )
    return float(result.stdout.strip())


def detect_scene_times(video_path, threshold):
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(video_path),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )

    scene_times = []
    for match in SCENE_TIME_RE.finditer(result.stderr):
        scene_times.append(float(match.group(1)))

    scene_times.sort()
    return scene_times


def detect_scene_scores(video_path):
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video_path),
            "-vf",
            "scdet,metadata=print",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )

    scores = []
    current_frame_index = None
    current_pts = None
    current_time = None
    first_frame_pts = None
    first_frame_time = None
    last_frame_index = None
    last_frame_pts = None
    last_frame_time = None

    for line in result.stderr.splitlines():
        frame_match = FRAME_INDEX_RE.search(line)
        time_match = FRAME_TIME_RE.search(line)
        if frame_match and time_match:
            current_frame_index = int(frame_match.group(1))
            pts_match = FRAME_PTS_RE.search(line)
            current_pts = int(pts_match.group(1)) if pts_match else None
            current_time = float(time_match.group(1))
            if first_frame_time is None:
                first_frame_pts = current_pts
                first_frame_time = current_time
            last_frame_index = current_frame_index
            last_frame_pts = current_pts
            last_frame_time = current_time
            continue

        score_match = SCD_SCORE_RE.search(line)
        if (
            score_match
            and current_time is not None
            and current_frame_index is not None
        ):
            scores.append({
                "time": current_time,
                "score": float(score_match.group(1)),
                "frame_index": current_frame_index,
                "pts": current_pts,
            })
            current_frame_index = None
            current_pts = None
            current_time = None

    return scores, {
        "decoded_frame_count": (
            int(last_frame_index) + 1 if last_frame_index is not None else 0
        ),
        "first_frame_pts": first_frame_pts,
        "first_frame_time": first_frame_time,
        "last_frame_pts": last_frame_pts,
        "last_frame_time": last_frame_time,
    }


def detect_silence_points(video_path, noise_db, min_duration):
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(video_path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ]
    )

    points = []
    for line in result.stderr.splitlines():
        start_match = SILENCE_START_RE.search(line)
        if start_match:
            points.append(float(start_match.group(1)))

        end_match = SILENCE_END_RE.search(line)
        if end_match:
            points.append(float(end_match.group(1)))

    return dedupe_points(points)


def detect_black_points(video_path, min_duration, pix_threshold):
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(video_path),
            "-vf",
            f"blackdetect=d={min_duration}:pix_th={pix_threshold}",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )

    points = []
    for line in result.stderr.splitlines():
        start_match = BLACK_START_RE.search(line)
        if start_match:
            points.append(float(start_match.group(1)))

        end_match = BLACK_END_RE.search(line)
        if end_match:
            points.append(float(end_match.group(1)))

    return dedupe_points(points)


def extract_audio_wav(video_path, wav_path, sample_rate):
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
    )


def transcribe_speech_segments(audio_path, args):
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError(f"faster_whisper unavailable: {exc}") from exc

    warnings.filterwarnings("ignore", module="faster_whisper")

    model_key = (args.whisper_model, args.whisper_device, args.whisper_compute_type)
    model = WHISPER_MODEL_CACHE.get(model_key)
    if model is None:
        model = WhisperModel(
            args.whisper_model,
            device=args.whisper_device,
            compute_type=args.whisper_compute_type,
        )
        WHISPER_MODEL_CACHE[model_key] = model

    segments, info = model.transcribe(
        str(audio_path),
        vad_filter=True,
        beam_size=1,
    )

    speech_segments = []
    for segment in segments:
        speech_segments.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": segment.text.strip(),
            }
        )

    return speech_segments, info


def hz_to_mel(value):
    return 2595.0 * np.log10(1.0 + value / 700.0)


def mel_to_hz(value):
    return 700.0 * (10.0 ** (value / 2595.0) - 1.0)


def build_mel_filterbank(sample_rate, n_fft, n_mels):
    mel_min = hz_to_mel(0.0)
    mel_max = hz_to_mel(sample_rate / 2.0)
    mel_points = np.linspace(mel_min, mel_max, n_mels + 2)
    hz_points = mel_to_hz(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)

    filterbank = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for mel_index in range(1, n_mels + 1):
        left = bins[mel_index - 1]
        center = bins[mel_index]
        right = bins[mel_index + 1]

        if center <= left:
            center = left + 1
        if right <= center:
            right = center + 1

        for freq_index in range(left, center):
            filterbank[mel_index - 1, freq_index] = (freq_index - left) / (center - left)
        for freq_index in range(center, min(right, filterbank.shape[1])):
            filterbank[mel_index - 1, freq_index] = (right - freq_index) / (right - center)

    return filterbank


def frame_audio(samples, frame_length, hop_length):
    if len(samples) < frame_length:
        pad_width = frame_length - len(samples)
        samples = np.pad(samples, (0, pad_width))

    frame_count = 1 + max(0, (len(samples) - frame_length) // hop_length)
    if frame_count <= 0:
        return np.empty((0, frame_length), dtype=np.float32)

    frames = []
    for index in range(frame_count):
        start = index * hop_length
        end = start + frame_length
        frames.append(samples[start:end])

    return np.asarray(frames, dtype=np.float32)


def compute_segment_signature(audio_samples, sample_rate, start, end, args, mel_filterbank):
    margin = args.speaker_margin
    start_time = max(0.0, start + margin)
    end_time = max(start_time, end - margin)

    if end_time - start_time < args.speaker_min_segment_duration:
        start_time = start
        end_time = end

    start_index = int(start_time * sample_rate)
    end_index = int(end_time * sample_rate)
    segment = audio_samples[start_index:end_index]

    if len(segment) < int(args.speaker_min_segment_duration * sample_rate):
        return None

    segment = segment.astype(np.float64)
    segment -= np.mean(segment)
    peak = np.max(np.abs(segment))
    if peak > 0:
        segment /= peak

    frame_length = int(0.025 * sample_rate)
    hop_length = int(0.010 * sample_rate)
    n_fft = 512

    frames = frame_audio(segment, frame_length, hop_length)
    if len(frames) == 0:
        return None

    window = np.hamming(frame_length).astype(np.float64)
    frames = frames * window
    spectrum = np.abs(np.fft.rfft(frames, n=n_fft, axis=1)) ** 2
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        mel_spec = spectrum @ mel_filterbank.T
    mel_spec = np.nan_to_num(mel_spec, nan=1e-10, posinf=1e10, neginf=1e-10)
    mel_spec = np.maximum(mel_spec, 1e-10)
    log_mel = np.log(mel_spec)
    mfcc = dct(log_mel, type=2, axis=1, norm="ortho")[:, :13]

    delta = np.diff(mfcc, axis=0)
    if len(delta) == 0:
        delta = np.zeros_like(mfcc[:1])

    signature = np.concatenate(
        [
            np.mean(mfcc, axis=0),
            np.std(mfcc, axis=0),
            np.mean(delta, axis=0),
            np.std(delta, axis=0),
        ]
    )
    return signature.astype(np.float32)


def cosine_distance(left, right):
    left_norm = np.linalg.norm(left)
    right_norm = np.linalg.norm(right)
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return 1.0 - float(np.dot(left, right) / (left_norm * right_norm))


def detect_asr_speaker_points(video_path, args):
    try:
        from scipy.fftpack import dct
        from scipy.io import wavfile
    except Exception as exc:
        raise RuntimeError(
            "ASR speaker mode requires scipy, but scipy is not available in this Python environment."
        ) from exc

    with tempfile.TemporaryDirectory(prefix="scene_split_asr_") as temp_dir:
        audio_path = Path(temp_dir) / "audio.wav"
        extract_audio_wav(video_path, audio_path, args.audio_sample_rate)
        sample_rate, audio_samples = wavfile.read(audio_path)

        if audio_samples.ndim > 1:
            audio_samples = np.mean(audio_samples, axis=1)

        speech_segments, info = transcribe_speech_segments(audio_path, args)
        mel_filterbank = build_mel_filterbank(sample_rate, 512, 26)

        signatures = []
        for segment in speech_segments:
            signature = compute_segment_signature(
                audio_samples,
                sample_rate,
                segment["start"],
                segment["end"],
                args,
                mel_filterbank,
            )
            if signature is None:
                continue
            signatures.append(
                {
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": segment["text"],
                    "signature": signature,
                }
            )

        transitions = []
        for previous, current in zip(signatures, signatures[1:]):
            boundary = (previous["end"] + current["start"]) / 2.0
            distance = cosine_distance(previous["signature"], current["signature"])
            transitions.append((boundary, distance))

        if not transitions:
            return {
                "points": [],
                "threshold": args.speaker_distance_threshold,
                "transitions": [],
                "speech_segments": speech_segments,
                "language": getattr(info, "language", None),
            }

        distances = np.asarray([distance for _, distance in transitions], dtype=np.float32)
        median_distance = float(np.median(distances))
        mad_distance = float(np.median(np.abs(distances - median_distance)))
        adaptive_threshold = median_distance + args.speaker_mad_multiplier * mad_distance
        threshold = max(args.speaker_distance_threshold, adaptive_threshold)

        points = []
        for boundary, distance in transitions:
            if distance >= threshold:
                points.append((boundary, distance))

        return {
            "points": points,
            "threshold": threshold,
            "transitions": transitions,
            "speech_segments": speech_segments,
            "language": getattr(info, "language", None),
        }


def dedupe_points(points, epsilon=0.05):
    ordered = sorted(points)
    deduped = []

    for point in ordered:
        if not deduped or point - deduped[-1] > epsilon:
            deduped.append(point)

    return deduped


def build_scene_boundaries(duration, scene_times, min_duration):
    boundaries = [0.0]

    for scene_time in scene_times:
        if scene_time <= 0.0 or scene_time >= duration:
            continue
        if scene_time - boundaries[-1] < min_duration:
            continue
        boundaries.append(scene_time)

    if duration - boundaries[-1] > 0.001:
        boundaries.append(duration)

    return boundaries


def build_score_candidates(frame_scores, candidate_score, strong_score, burst_window):
    filtered = [
        dict(item)
        for item in frame_scores
        if float(item.get("score", 0) or 0) >= candidate_score
    ]
    if not filtered:
        return [], []

    clusters = []
    current_cluster = [filtered[0]]

    for item in filtered[1:]:
        if item["time"] - current_cluster[-1]["time"] <= burst_window:
            current_cluster.append(item)
        else:
            clusters.append(current_cluster)
            current_cluster = [item]

    clusters.append(current_cluster)

    candidates = []
    for cluster in clusters:
        selected = max(
            cluster,
            key=lambda item: (float(item["score"]), -float(item["time"])),
        )
        candidates.append(
            CandidateCut(
                time=float(selected["time"]),
                score=float(selected["score"]),
                kind="scene",
                strong=float(selected["score"]) >= strong_score,
                frame_index=int(selected["frame_index"]),
                pts=(
                    int(selected["pts"])
                    if selected.get("pts") is not None else None
                ),
            )
        )

    return filtered, candidates


def point_is_supported(time_value, auxiliary_points, support_window):
    return any(abs(time_value - point) <= support_window for point in auxiliary_points)


def build_hybrid_boundaries(duration, candidates, auxiliary_points, args):
    boundaries = [0.0]
    accepted = []

    for candidate in candidates:
        if candidate.time <= 0.0 or candidate.time >= duration:
            continue

        candidate.supported = point_is_supported(
            candidate.time,
            auxiliary_points,
            args.support_window,
        )

        gap = candidate.time - boundaries[-1]
        required_gap = args.min_duration
        reasons = [candidate.kind]

        if candidate.kind == "speaker":
            required_gap = min(required_gap, args.speaker_cut_min_duration)

        if candidate.kind == "scene" and candidate.strong:
            required_gap = min(required_gap, args.strong_min_duration)
            reasons = ["strong"]

        if candidate.supported:
            required_gap = min(required_gap, args.supported_min_duration)
            if "aux" not in reasons:
                reasons.append("aux")

        if gap >= required_gap:
            boundaries.append(candidate.time)
            accepted.append((candidate, "+".join(reasons)))

    if duration - boundaries[-1] > 0.001:
        boundaries.append(duration)

    return boundaries, accepted


def merge_hybrid_candidates(scene_candidates, speaker_points, args):
    candidates = list(scene_candidates)

    for time_value, distance in speaker_points:
        if any(abs(candidate.time - time_value) <= args.support_window for candidate in candidates):
            continue
        candidates.append(
            CandidateCut(
                time=time_value,
                score=distance,
                kind="speaker",
                strong=False,
            )
        )

    candidates.sort(key=lambda candidate: candidate.time)
    return candidates


def split_video(video_path, output_dir, boundaries, overwrite):
    output_dir.mkdir(parents=True, exist_ok=True)
    parts = []

    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), start=1):
        duration = end - start
        if duration <= 0.001:
            continue

        output_path = output_dir / f"part{index:03d}.mp4"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y" if overwrite else "-n",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        run_command(command)
        parts.append((output_path, start, end))

    return parts


def default_inputs(root, output_root):
    videos = []
    for path in sorted(root.glob("*.mp4")):
        if output_root in path.parents:
            continue
        videos.append(path)
    return videos


def parse_args():
    parser = argparse.ArgumentParser(
        description="Batch split MP4 files by scene changes with optional auxiliary signals."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Video files to split. If omitted, split all top-level .mp4 files in the current directory.",
    )
    parser.add_argument(
        "--mode",
        choices=["hybrid", "scene"],
        default="hybrid",
        help="scene: original threshold-only mode. hybrid: scene score + silence/black support + burst suppression. Default: hybrid",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.35,
        help="Scene-only mode threshold for ffmpeg select(scene). Lower means more cuts. Default: 0.35",
    )
    parser.add_argument(
        "--candidate-score",
        type=float,
        default=10.0,
        help="Hybrid mode minimum scdet score to consider a cut candidate. Default: 10.0",
    )
    parser.add_argument(
        "--strong-score",
        type=float,
        default=24.0,
        help="Hybrid mode scdet score treated as a strong cut. Default: 24.0",
    )
    parser.add_argument(
        "--burst-window",
        type=float,
        default=1.2,
        help="Merge consecutive candidate cuts inside this many seconds and keep the strongest one. Default: 1.2",
    )
    parser.add_argument(
        "--min-duration",
        type=float,
        default=3.0,
        help="Default minimum segment duration in seconds. Default: 3.0",
    )
    parser.add_argument(
        "--strong-min-duration",
        type=float,
        default=2.0,
        help="Hybrid mode minimum duration for strong cuts. Default: 2.0",
    )
    parser.add_argument(
        "--supported-min-duration",
        type=float,
        default=1.5,
        help="Hybrid mode minimum duration for cuts near silence or black points. Default: 1.5",
    )
    parser.add_argument(
        "--support-window",
        type=float,
        default=0.4,
        help="Hybrid mode maximum distance in seconds between a cut and silence/black support point. Default: 0.4",
    )
    parser.add_argument(
        "--silence-noise",
        type=float,
        default=-35.0,
        help="Silencedetect noise threshold in dB. Default: -35.0",
    )
    parser.add_argument(
        "--silence-duration",
        type=float,
        default=0.2,
        help="Minimum silence duration in seconds. Default: 0.2",
    )
    parser.add_argument(
        "--black-duration",
        type=float,
        default=0.15,
        help="Minimum black frame duration in seconds. Default: 0.15",
    )
    parser.add_argument(
        "--black-pix-threshold",
        type=float,
        default=0.10,
        help="Blackdetect pixel threshold. Default: 0.10",
    )
    parser.add_argument(
        "--no-silence",
        action="store_true",
        help="Hybrid mode: ignore silencedetect support points.",
    )
    parser.add_argument(
        "--no-black",
        action="store_true",
        help="Hybrid mode: ignore blackdetect support points.",
    )
    parser.add_argument(
        "--enable-asr-speaker",
        action="store_true",
        help="Hybrid mode: add approximate speaker-change points from faster-whisper speech segments and audio feature distance.",
    )
    parser.add_argument(
        "--whisper-model",
        default="medium",
        help="faster-whisper model name or local path. Default: medium",
    )
    parser.add_argument(
        "--whisper-device",
        default="cpu",
        help="faster-whisper device. Default: cpu",
    )
    parser.add_argument(
        "--whisper-compute-type",
        default="int8",
        help="faster-whisper compute type. Default: int8",
    )
    parser.add_argument(
        "--audio-sample-rate",
        type=int,
        default=16000,
        help="Sample rate used for ASR/speaker analysis. Default: 16000",
    )
    parser.add_argument(
        "--speaker-distance-threshold",
        type=float,
        default=0.22,
        help="Minimum cosine distance between adjacent speech-segment signatures to mark a speaker change. Default: 0.22",
    )
    parser.add_argument(
        "--speaker-mad-multiplier",
        type=float,
        default=2.5,
        help="Adaptive threshold multiplier based on median absolute deviation of adjacent speech distances. Default: 2.5",
    )
    parser.add_argument(
        "--speaker-min-segment-duration",
        type=float,
        default=0.8,
        help="Minimum speech segment duration used to compute speaker signatures. Default: 0.8",
    )
    parser.add_argument(
        "--speaker-margin",
        type=float,
        default=0.15,
        help="Trim this many seconds from both ends of a speech segment before computing its signature. Default: 0.15",
    )
    parser.add_argument(
        "--speaker-cut-min-duration",
        type=float,
        default=2.0,
        help="Minimum duration required for speaker-only cut points. Default: 2.0",
    )
    parser.add_argument(
        "--output-root",
        default="scene_split_output",
        help="Directory used to store per-video split folders. Default: scene_split_output",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output folders for the selected videos.",
    )
    return parser.parse_args()


def process_scene_mode(video, duration, args):
    scene_times = detect_scene_times(video, args.threshold)
    boundaries = build_scene_boundaries(duration, scene_times, args.min_duration)
    stats = {
        "mode": "scene",
        "scene_times": scene_times,
    }
    return boundaries, stats


def process_hybrid_mode(video, duration, args):
    frame_scores, frame_metadata = detect_scene_scores(video)
    raw_candidates, merged_candidates = build_score_candidates(
        frame_scores,
        args.candidate_score,
        args.strong_score,
        args.burst_window,
    )

    silence_points = []
    black_points = []
    speaker_info = {
        "points": [],
        "threshold": None,
        "transitions": [],
        "speech_segments": [],
        "language": None,
    }
    auxiliary_points = []

    if not args.no_silence:
        silence_points = detect_silence_points(
            video,
            args.silence_noise,
            args.silence_duration,
        )
        auxiliary_points.extend(silence_points)

    if not args.no_black:
        black_points = detect_black_points(
            video,
            args.black_duration,
            args.black_pix_threshold,
        )
        auxiliary_points.extend(black_points)

    if args.enable_asr_speaker:
        speaker_info = detect_asr_speaker_points(video, args)
        auxiliary_points.extend([time_value for time_value, _ in speaker_info["points"]])

    auxiliary_points = dedupe_points(auxiliary_points)
    candidates = merge_hybrid_candidates(
        merged_candidates,
        speaker_info["points"],
        args,
    )
    boundaries, accepted = build_hybrid_boundaries(
        duration,
        candidates,
        auxiliary_points,
        args,
    )

    stats = {
        "mode": "hybrid",
        "raw_candidates": raw_candidates,
        "merged_candidates": merged_candidates,
        "final_candidates": candidates,
        "accepted": accepted,
        "silence_points": silence_points,
        "black_points": black_points,
        "speaker_points": speaker_info["points"],
        "speaker_threshold": speaker_info["threshold"],
        "speaker_transitions": speaker_info["transitions"],
        "speech_segments": speaker_info["speech_segments"],
        "speech_language": speaker_info["language"],
        **frame_metadata,
    }
    return boundaries, stats


def main():
    args = parse_args()
    root = Path.cwd()
    output_root = (root / args.output_root).resolve()

    if args.inputs:
        videos = [Path(video).resolve() for video in args.inputs]
    else:
        videos = default_inputs(root, output_root)

    if not videos:
        print("No input videos found.", file=sys.stderr)
        return 1

    for video in videos:
        if not video.exists():
            print(f"Missing input video: {video}", file=sys.stderr)
            return 1

    for video in videos:
        duration = probe_duration(video)
        output_dir = output_root / video.stem

        if output_dir.exists():
            if not args.overwrite:
                print(
                    f"Skip {video.name}: output directory already exists at {output_dir}",
                    file=sys.stderr,
                )
                continue
            shutil.rmtree(output_dir)

        if args.mode == "scene":
            boundaries, stats = process_scene_mode(video, duration, args)
        else:
            boundaries, stats = process_hybrid_mode(video, duration, args)

        parts = split_video(video, output_dir, boundaries, overwrite=args.overwrite)

        print(f"{video.name}")
        print(f"  mode: {stats['mode']}")
        print(f"  duration: {duration:.3f}s")

        if stats["mode"] == "scene":
            print(f"  detected scene cuts: {len(stats['scene_times'])}")
        else:
            print(f"  raw score candidates: {len(stats['raw_candidates'])}")
            print(f"  burst-merged candidates: {len(stats['merged_candidates'])}")
            print(f"  silence support points: {len(stats['silence_points'])}")
            print(f"  black support points: {len(stats['black_points'])}")
            print(f"  speaker change points: {len(stats['speaker_points'])}")
            if stats["speaker_threshold"] is not None:
                print(f"  speaker threshold: {stats['speaker_threshold']:.3f}")
            print(f"  accepted cuts: {len(stats['accepted'])}")
            for candidate, reason in stats["accepted"]:
                print(
                    f"    cut @ {candidate.time:.3f}s kind={candidate.kind} score={candidate.score:.3f} reason={reason}"
                )

        print(f"  exported parts: {len(parts)}")
        for output_path, start, end in parts:
            print(f"    {output_path.name}: {start:.3f} -> {end:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
