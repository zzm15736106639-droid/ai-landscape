from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import os
from pathlib import Path
import random
import re
import shutil
import tempfile
import threading
import time
import uuid

from frameshift.integration import analyze_video, write_debug_artifacts

from .config import cache_root, ffmpeg_path, ffprobe_path, user_data_root
from .effects import ensure_effects
from .perf import ResourceSampler, append_jsonl
from .processes import CommandCancelled, PROCESS_REGISTRY, run_command
from .renderer import render_output


_JOBS: dict[str, dict] = {}
_LOCK = threading.RLock()
_QUEUE: list[str] = []
_QUEUE_EVENT = threading.Event()
_QUEUE_THREAD: threading.Thread | None = None


def _public_job(job: dict) -> dict:
    result = {}
    for key, value in job.items():
        if key.startswith("_"):
            continue
        result[key] = copy.deepcopy(value)
    return result


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        return _public_job(job) if job else None


def list_jobs() -> list[dict]:
    with _LOCK:
        return [_public_job(item) for item in _JOBS.values()]


def _update_queue_positions() -> None:
    position = 1
    for job_id in _QUEUE:
        job = _JOBS.get(job_id)
        if job and job["status"] == "pending":
            job["queue_position"] = position
            position += 1


def cancel_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        if not job:
            return None
        if job["status"] in {"done", "error", "cancelled"}:
            return _public_job(job)
        job["cancelled"] = True
        job["_cancel_event"].set()
        if job["status"] == "pending":
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            job["queue_position"] = 0
        else:
            job["status"] = "cancelling"
        snapshot = _public_job(job)
    PROCESS_REGISTRY.terminate_job(job_id)
    _QUEUE_EVENT.set()
    return snapshot


def cancel_all() -> int:
    with _LOCK:
        identifiers = [
            job_id for job_id, job in _JOBS.items()
            if job["status"] not in {"done", "error", "cancelled"}
        ]
    for job_id in identifiers:
        cancel_job(job_id)
    return len(identifiers)


def _safe_filename(value: str, fallback: str = "video") -> str:
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or "")).strip(" ._")
    return text[:100] or fallback


def _unique_output_path(directory: Path, stem: str, reserved: set[str]) -> Path:
    base = _safe_filename(stem)
    candidate = directory / f"{base}.mp4"
    index = 2
    while str(candidate).casefold() in reserved or candidate.exists():
        candidate = directory / f"{base} ({index}).mp4"
        index += 1
    reserved.add(str(candidate).casefold())
    return candidate


def _analysis_safe_input(source: Path, directory: Path) -> Path:
    target = directory / f"source{source.suffix.lower()}"
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target


def _debug_dir(output_dir: Path, job_id: str, video: dict, index: int) -> Path:
    digest = hashlib.sha256(str(video["path"]).encode("utf-8")).hexdigest()[:8]
    stem = _safe_filename(Path(video["name"]).stem)
    return output_dir / ".ai_landscape_debug" / job_id / f"{index + 1:03d}_{stem}_{digest}"


def _build_combinations(
    job_id: str,
    videos: list[dict],
    output_dir: Path,
    fixed_ids: list[str],
    random_ids: list[str],
    effect_map: dict[str, dict],
) -> list[dict]:
    fixed_slots = fixed_ids or [""]
    combinations = []
    for video_index, video in enumerate(videos):
        for fixed_id in fixed_slots:
            combinations.append({
                "video_index": video_index,
                "video": video,
                "fixed_id": fixed_id,
                "random_id": "",
            })
    if random_ids:
        shuffled = list(random_ids)
        random.Random(job_id).shuffle(shuffled)
        for index, combination in enumerate(combinations):
            combination["random_id"] = shuffled[index % len(shuffled)]
    reserved: set[str] = set()
    for index, combination in enumerate(combinations):
        effect_ids = list(dict.fromkeys(
            item for item in (combination["fixed_id"], combination["random_id"]) if item
        ))
        labels = [_safe_filename(effect_map[item]["name"], "effect") for item in effect_ids]
        source_stem = Path(combination["video"]["name"]).stem
        stem = source_stem if not labels else f"{source_stem}_{'_'.join(labels)}"
        combination.update({
            "output_index": index,
            "effect_ids": effect_ids,
            "output_path": str(_unique_output_path(output_dir, stem, reserved)),
        })
    return combinations


def _log_performance(record: dict) -> None:
    append_jsonl(user_data_root() / "logs" / "performance.jsonl", record)


def _run_analysis(job: dict, video: dict, index: int) -> tuple[dict, Path, dict]:
    source = Path(video["path"]).resolve()
    job_id = job["id"]
    cancel_event: threading.Event = job["_cancel_event"]
    debug_dir = _debug_dir(Path(job["output_dir"]), job_id, video, index)
    debug_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ai_landscape_analysis_"))
    sampler = ResourceSampler()
    sampler.start()
    performance: dict = {}
    started = time.perf_counter()
    try:
        safe_source = _analysis_safe_input(source, work)

        def runner(command):
            return run_command(
                command,
                job_id=job_id,
                cancel_event=cancel_event,
                timeout=max(300.0, float(video.get("duration") or 0) * 15.0),
            )

        def progress(info):
            with _LOCK:
                job["preprocess"]["stage"] = str(info.get("stage") or "")
                job["preprocess"]["current"] = video["name"]

        analysis = analyze_video(
            source,
            ffmpeg_path(),
            ffprobe_path(),
            runner=runner,
            cache_root=cache_root() / "analysis",
            safe_input_path=safe_source,
            cancel_check=cancel_event.is_set,
            progress_callback=progress,
            performance_callback=lambda value: performance.update(value),
        )
        bounds = job["_bounds_by_path"].get(str(source))
        write_debug_artifacts(
            debug_dir,
            source,
            analysis,
            vertical_bounds=bounds,
            cancel_check=cancel_event.is_set,
        )
        resources = sampler.finish()
        analysis_seconds = round(time.perf_counter() - started, 3)
        metrics = {
            **performance,
            **resources,
            "analysis_total_seconds": analysis_seconds,
            "job_id": job_id,
            "stage": "ai_analysis",
            "video_path": str(source),
            "video_name": video["name"],
            "cache_hit": bool(analysis.get("cache_hit")),
        }
        _log_performance(metrics)
        (debug_dir / "performance.json").write_text(
            __import__("json").dumps(metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return analysis, debug_dir, metrics
    except Exception:
        resources = sampler.finish()
        _log_performance({
            **performance,
            **resources,
            "analysis_total_seconds": round(time.perf_counter() - started, 3),
            "job_id": job_id,
            "stage": "ai_analysis",
            "video_path": str(source),
            "video_name": video["name"],
            "status": "cancelled" if cancel_event.is_set() else "error",
        })
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        if job["cancelled"]:
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            return
        job["status"] = "running"
        job["started_at"] = time.time()
        job["queue_position"] = 0
    analyses: dict[str, dict] = {}
    debug_dirs: dict[str, Path] = {}
    analysis_errors: dict[str, str] = {}
    analysis_metrics: list[dict] = []
    try:
        for index, video in enumerate(job["_videos"]):
            if job["_cancel_event"].is_set():
                break
            source = str(Path(video["path"]).resolve())
            with _LOCK:
                job["preprocess"]["current"] = video["name"]
                job["preprocess"]["stage"] = "starting"
            try:
                analysis, debug_dir, metrics = _run_analysis(job, video, index)
                analyses[source] = analysis
                debug_dirs[source] = debug_dir
                analysis_metrics.append(metrics)
                with _LOCK:
                    job["preprocess"]["completed"] += 1
                    if analysis.get("cache_hit"):
                        job["preprocess"]["cache_hits"] += 1
            except Exception as exc:
                if job["_cancel_event"].is_set():
                    break
                analysis_errors[source] = str(exc)
                with _LOCK:
                    job["preprocess"]["failed"] += 1
        with _LOCK:
            job["preprocess"]["current"] = ""
            job["preprocess"]["stage"] = ""
            job["preprocess"]["status"] = (
                "cancelled" if job["_cancel_event"].is_set() else "done"
            )
        if job["_cancel_event"].is_set():
            with _LOCK:
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
            return

        encoding_metrics: list[dict] = []

        def encode_one(combination: dict) -> None:
            video = combination["video"]
            source = str(Path(video["path"]).resolve())
            output_path = combination["output_path"]
            output_index = combination["output_index"]
            started = time.perf_counter()
            with _LOCK:
                job["running_items"].append({
                    "output_index": output_index,
                    "name": Path(output_path).name,
                    "source_name": video["name"],
                    "started_at": time.time(),
                })
            status = "error"
            error = ""
            details = {}
            sampler = ResourceSampler()
            sampler.start()
            try:
                if source in analysis_errors:
                    raise RuntimeError(f"AI分析失败: {analysis_errors[source]}")
                if job["_cancel_event"].is_set():
                    raise CommandCancelled("任务已取消")
                details = render_output(
                    video,
                    analyses[source],
                    output_path,
                    effect_ids=combination["effect_ids"],
                    subtitle_config=job["_subtitles_by_path"].get(source),
                    vertical_bounds=job["_bounds_by_path"].get(source),
                    gpu_mode=job["gpu_mode"],
                    bitrate_k=job["output_video_bitrate_k"],
                    job_id=job_id,
                    cancel_event=job["_cancel_event"],
                    debug_dir=debug_dirs.get(source),
                )
                status = "ok"
            except Exception as exc:
                error = str(exc)
                Path(output_path).unlink(missing_ok=True)
            resources = sampler.finish()
            encoding_seconds = round(time.perf_counter() - started, 3)
            metric = {
                **resources,
                "job_id": job_id,
                "stage": "encoding",
                "video_path": source,
                "video_name": video["name"],
                "output_path": output_path if status == "ok" else "",
                "encoding_seconds": encoding_seconds,
                "status": status,
                "encoder": details.get("encoder", ""),
            }
            encoding_metrics.append(metric)
            _log_performance(metric)
            result = {
                "output_index": output_index,
                "name": Path(output_path).name,
                "source_name": video["name"],
                "source_path": source,
                "status": status,
                "error": error,
                "run_seconds": encoding_seconds,
                "output_path": output_path if status == "ok" else "",
                "debug_dir": str(debug_dirs.get(source, "")),
                "effect_ids": combination["effect_ids"],
                **details,
            }
            with _LOCK:
                job["running_items"] = [
                    item for item in job["running_items"]
                    if item["output_index"] != output_index
                ]
                job["results"].append(result)
                if status == "ok":
                    job["completed"] += 1
                else:
                    job["failed"] += 1

        with ThreadPoolExecutor(max_workers=job["workers"]) as executor:
            futures = [executor.submit(encode_one, item) for item in job["_combinations"]]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass
                if job["_cancel_event"].is_set():
                    PROCESS_REGISTRY.terminate_job(job_id)

        with _LOCK:
            job["results"].sort(key=lambda item: item["output_index"])
            job["running_items"] = []
            analysis_total = round(sum(float(item.get("analysis_total_seconds") or 0) for item in analysis_metrics), 3)
            encoding_total = round(sum(float(item.get("encoding_seconds") or 0) for item in encoding_metrics), 3)
            job["performance"] = {
                "analysis_total_seconds": analysis_total,
                "encoding_total_worker_seconds": encoding_total,
                "analysis_video_count": len(analysis_metrics),
                "encoding_output_count": len(encoding_metrics),
            }
            job["status"] = "cancelled" if job["_cancel_event"].is_set() else "done"
            job["finished_at"] = time.time()
        _log_performance({
            "job_id": job_id,
            "stage": "job_summary",
            "status": job["status"],
            **job["performance"],
            "wall_seconds": round(job["finished_at"] - job["started_at"], 3),
            "completed": job["completed"],
            "failed": job["failed"],
        })
    except Exception as exc:
        with _LOCK:
            job["status"] = "cancelled" if job["_cancel_event"].is_set() else "error"
            job["error"] = str(exc)
            job["finished_at"] = time.time()
            job["running_items"] = []


def _queue_worker() -> None:
    while True:
        _QUEUE_EVENT.wait()
        while True:
            with _LOCK:
                while _QUEUE and _JOBS[_QUEUE[0]]["status"] == "cancelled":
                    _QUEUE.pop(0)
                if not _QUEUE:
                    _QUEUE_EVENT.clear()
                    break
                job_id = _QUEUE.pop(0)
                _update_queue_positions()
            _run_job(job_id)


def _ensure_queue_thread() -> None:
    global _QUEUE_THREAD
    with _LOCK:
        if _QUEUE_THREAD and _QUEUE_THREAD.is_alive():
            return
        _QUEUE_THREAD = threading.Thread(target=_queue_worker, daemon=True, name="ai-landscape-jobs")
        _QUEUE_THREAD.start()


def start_job(
    videos: list[dict],
    output_dir: str | Path,
    *,
    workers: int,
    gpu_mode: str,
    output_video_bitrate_k: int,
    ai_crop_bounds: dict[str, dict],
    subtitle_configs: dict[str, dict],
    effect_all_template_ids: list[str],
    effect_random_template_ids: list[str],
) -> str:
    job_id = uuid.uuid4().hex[:12]
    output_dir = Path(output_dir).resolve()
    fixed_ids = list(dict.fromkeys(effect_all_template_ids or []))
    random_ids = list(dict.fromkeys(effect_random_template_ids or []))
    effect_map = ensure_effects(fixed_ids + random_ids)
    combinations = _build_combinations(
        job_id, videos, output_dir, fixed_ids, random_ids, effect_map
    )
    cancel_event = threading.Event()
    job = {
        "id": job_id,
        "type": "ai_landscape",
        "status": "pending",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "queue_position": 0,
        "output_dir": str(output_dir),
        "workers": int(workers),
        "gpu_mode": gpu_mode,
        "output_video_bitrate_k": int(output_video_bitrate_k),
        "total": len(combinations),
        "completed": 0,
        "failed": 0,
        "cancelled": False,
        "error": "",
        "running_items": [],
        "results": [],
        "preprocess": {
            "status": "pending",
            "total": len(videos),
            "completed": 0,
            "failed": 0,
            "cache_hits": 0,
            "current": "",
            "stage": "",
        },
        "effect_all_template_ids": fixed_ids,
        "effect_random_template_ids": random_ids,
        "performance": {},
        "_cancel_event": cancel_event,
        "_videos": videos,
        "_bounds_by_path": ai_crop_bounds,
        "_subtitles_by_path": subtitle_configs,
        "_combinations": combinations,
    }
    with _LOCK:
        _JOBS[job_id] = job
        _QUEUE.append(job_id)
        _update_queue_positions()
    _ensure_queue_thread()
    _QUEUE_EVENT.set()
    return job_id
