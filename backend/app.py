from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

from flask import Flask, jsonify, request, send_file

from .config import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_VIDEO_BITRATE_K,
    MAX_WORKERS,
    cache_root,
    ffmpeg_path,
    ffprobe_path,
    static_root,
)
from .effects import (
    delete_effects,
    delete_preset,
    effect_preview,
    effect_source,
    import_effect_bytes,
    import_effect_file,
    list_effects,
    rename_effect,
    save_preset,
)
from .jobs import cancel_all, cancel_job, get_job, start_job
from .media import (
    RANGE_RE,
    VIDEO_EXTENSIONS,
    cached_thumbnail,
    probe_video,
    thumbnail_path,
    validate_output_dir,
    video_mimetype,
)
from .renderer import nvenc_listed
from .subtitles import (
    FONTS,
    MAX_SUBTITLE_BYTES,
    cache_subtitle_bytes,
    cache_subtitle_file,
    font_catalog,
    normalize_subtitle_configs,
    preview_path,
    render_preview,
    resolve_font,
)


ALLOWED_JOB_FIELDS = {
    "videos",
    "output_dir",
    "gpu_mode",
    "workers",
    "output_video_bitrate_k",
    "ai_crop_bounds",
    "subtitle_configs",
    "effect_all_template_ids",
    "effect_random_template_ids",
}
REMOVED_FIELD_PREFIXES = (
    "blur_",
    "frame_drop_",
    "speedup_",
    "trim_",
    "output_duration_",
)
REMOVED_FIELDS = {"tail_video_path", "video_transforms"}


def _json_error(message: str, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


def _request_json() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return body


def _upload_directory(kind: str) -> Path:
    path = cache_root() / "uploads" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_upload(upload, kind: str, extensions: set[str], max_bytes: int) -> Path:
    original_name = Path(upload.filename or "").name
    extension = Path(original_name).suffix.lower()
    if extension not in extensions:
        raise ValueError(f"不支持的文件格式: {extension or original_name}")
    temporary = _upload_directory(kind) / f"upload-{uuid.uuid4().hex}{extension}"
    digest = hashlib.sha256()
    total = 0
    with temporary.open("wb") as handle:
        while True:
            chunk = upload.stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"文件不能超过 {max_bytes // 1024 // 1024}MB")
            digest.update(chunk)
            handle.write(chunk)
    if total == 0:
        temporary.unlink(missing_ok=True)
        raise ValueError("上传文件为空")
    target = _upload_directory(kind) / f"{digest.hexdigest()}{extension}"
    if target.is_file():
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(target)
    return target


def _video_response(path: Path):
    file_size = path.stat().st_size
    range_header = request.headers.get("Range", "")
    match = RANGE_RE.match(range_header)
    if not match:
        return send_file(path, mimetype=video_mimetype(path), conditional=True)
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return _json_error("Range 请求无效", 416)
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    else:
        suffix_length = int(end_text)
        start = max(0, file_size - suffix_length)
        end = file_size - 1
    if start >= file_size or start < 0 or end < start:
        response = _json_error("Range 超出文件范围", 416)[0]
        response.headers["Content-Range"] = f"bytes */{file_size}"
        return response, 416
    end = min(end, file_size - 1)
    length = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(length)
    response = Flask.response_class(data, 206, mimetype=video_mimetype(path), direct_passthrough=True)
    response.headers.update({
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
    })
    return response


def _normalize_video_inputs(raw_videos) -> list[dict]:
    if not isinstance(raw_videos, list) or not raw_videos:
        raise ValueError("请至少导入一个视频")
    videos = []
    seen = set()
    for raw in raw_videos:
        path_text = raw.get("path") if isinstance(raw, dict) else raw
        path = str(Path(str(path_text or "")).expanduser().resolve())
        if not path or path in seen:
            continue
        metadata = probe_video(path)
        if metadata["width"] >= metadata["height"]:
            raise ValueError(f"AI横屏仅支持竖屏源视频: {metadata['name']}")
        videos.append(metadata)
        seen.add(path)
    if not videos:
        raise ValueError("没有可处理的竖屏视频")
    return videos


def _normalize_crop_bounds(raw_bounds, videos: list[dict]) -> dict[str, dict]:
    if raw_bounds in (None, "", [], {}):
        return {}
    if not isinstance(raw_bounds, list):
        raise ValueError("ai_crop_bounds 必须是数组")
    by_path = {item["path"]: item for item in videos}
    result = {}
    for raw in raw_bounds:
        if not isinstance(raw, dict):
            raise ValueError("ai_crop_bounds 每项必须是对象")
        path = str(Path(str(raw.get("video_path") or "")).expanduser().resolve())
        metadata = by_path.get(path)
        if not metadata:
            raise ValueError(f"取景上下限目标视频不在任务列表中: {path}")
        if path in result:
            raise ValueError(f"同一视频重复设置取景上下限: {metadata['name']}")
        try:
            basis_height = float(raw.get("basis_height", metadata["height"]))
            upper_y = float(raw.get("upper_y", 0))
            lower_y = float(raw.get("lower_y", basis_height))
        except (TypeError, ValueError) as exc:
            raise ValueError("取景上下限必须是数字") from exc
        if basis_height <= 0 or not all(map(__import__("math").isfinite, (basis_height, upper_y, lower_y))):
            raise ValueError("取景上下限无效")
        source_height = metadata["height"]
        scaled_upper = max(0.0, min(source_height, upper_y * source_height / basis_height))
        scaled_lower = max(0.0, min(source_height, lower_y * source_height / basis_height))
        minimum_height = metadata["width"] * 9.0 / 16.0
        if scaled_lower - scaled_upper + 0.5 < minimum_height:
            raise ValueError(
                f"{metadata['name']} 的取景高度不足，至少需要 {int(round(minimum_height))}px"
            )
        result[path] = {
            "upper_y": round(upper_y, 3),
            "lower_y": round(lower_y, 3),
            "basis_height": round(basis_height, 3),
        }
    return result


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False

    @app.errorhandler(Exception)
    def handle_error(exc):
        if isinstance(exc, (ValueError, FileNotFoundError, PermissionError)):
            return _json_error(str(exc), 400)
        app.logger.exception("Unhandled request error")
        return _json_error(str(exc) or "服务器内部错误", 500)

    @app.get("/api/health")
    def health():
        ffmpeg = ""
        ffprobe = ""
        error = ""
        try:
            ffmpeg = ffmpeg_path()
            ffprobe = ffprobe_path()
        except Exception as exc:
            error = str(exc)
        fonts = []
        for item in font_catalog():
            try:
                resolve_font(item["font_id"])
                available = True
            except FileNotFoundError:
                available = False
            fonts.append({**item, "available": available})
        return jsonify({
            "ok": not error,
            "name": APP_NAME,
            "version": APP_VERSION,
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "error": error,
            "nvenc_listed": nvenc_listed() if not error else False,
            "fonts": fonts,
        })

    @app.post("/api/probe")
    def probe():
        body = _request_json()
        metadata = probe_video(body.get("path"))
        if metadata["width"] >= metadata["height"]:
            raise ValueError("AI横屏仅支持显示方向为竖屏的视频")
        return jsonify({"ok": True, "video": metadata})

    @app.post("/api/videos/upload")
    def upload_videos():
        uploads = request.files.getlist("files")
        if not uploads:
            raise ValueError("请选择视频文件")
        items = []
        errors = []
        for upload in uploads:
            try:
                path = _save_upload(upload, "videos", VIDEO_EXTENSIONS, 20 * 1024 * 1024 * 1024)
                metadata = probe_video(path)
                if metadata["width"] >= metadata["height"]:
                    raise ValueError("仅支持竖屏视频")
                items.append(metadata)
            except Exception as exc:
                errors.append({"name": upload.filename, "error": str(exc)})
        return jsonify({"ok": True, "videos": items, "errors": errors})

    @app.get("/api/video")
    def stream_video():
        path = Path(request.args.get("path", "")).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise FileNotFoundError("视频不存在或格式不受支持")
        return _video_response(path)

    @app.post("/api/thumbnail")
    def thumbnail():
        body = _request_json()
        key, _ = thumbnail_path(body.get("path"), body.get("time_seconds"))
        return jsonify({"ok": True, "url": f"/api/thumbnail/{key}.jpg"})

    @app.get("/api/thumbnail/<filename>")
    def thumbnail_file(filename):
        key = Path(filename).stem
        return send_file(cached_thumbnail(key), mimetype="image/jpeg", conditional=True)

    @app.get("/api/drives")
    def drives():
        items = []
        if os.name == "nt":
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                path = Path(f"{letter}:\\")
                if path.exists():
                    items.append({"name": f"{letter}:", "path": str(path)})
        else:
            items.append({"name": "/", "path": "/"})
        return jsonify({"ok": True, "items": items})

    @app.get("/api/browse")
    def browse():
        path = Path(request.args.get("path") or Path.home()).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError("目录不存在")
        items = []
        try:
            children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        except PermissionError as exc:
            raise PermissionError("没有权限读取该目录") from exc
        for child in children[:1000]:
            if child.is_dir() or child.suffix.lower() in VIDEO_EXTENSIONS | {".srt"}:
                items.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "extension": child.suffix.lower(),
                })
        return jsonify({
            "ok": True,
            "path": str(path),
            "parent": str(path.parent) if path.parent != path else "",
            "items": items,
        })

    @app.post("/api/directories")
    def create_directory():
        body = _request_json()
        parent = Path(body.get("parent", "")).expanduser().resolve()
        name = str(body.get("name") or "").strip()
        if not parent.is_dir() or not name or Path(name).name != name:
            raise ValueError("新目录参数无效")
        target = parent / name
        target.mkdir(exist_ok=False)
        return jsonify({"ok": True, "path": str(target)})

    @app.get("/api/effects")
    def effects_list():
        return jsonify({"ok": True, **list_effects()})

    @app.post("/api/effects/import-paths")
    def effects_import_paths():
        body = _request_json()
        paths = body.get("paths")
        if not isinstance(paths, list):
            raise ValueError("paths 必须是数组")
        items = [import_effect_file(path) for path in paths]
        return jsonify({"ok": True, "effects": items})

    @app.post("/api/effects/upload")
    def effects_upload():
        uploads = request.files.getlist("files")
        if not uploads:
            raise ValueError("请选择特效文件")
        items = [import_effect_bytes(item.read(), item.filename or "effect") for item in uploads]
        return jsonify({"ok": True, "effects": items})

    @app.patch("/api/effects/<effect_id>")
    def effects_rename(effect_id):
        return jsonify({"ok": True, "effect": rename_effect(effect_id, _request_json().get("name"))})

    @app.delete("/api/effects")
    def effects_delete():
        body = _request_json()
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise ValueError("ids 必须是数组")
        return jsonify({"ok": True, "deleted": delete_effects(ids)})

    @app.get("/api/effects/<effect_id>/source")
    def effects_source(effect_id):
        path = effect_source(effect_id)
        return send_file(path, mimetype=mimetypes.guess_type(path)[0], conditional=True)

    @app.get("/api/effects/<effect_id>/preview")
    def effects_preview(effect_id):
        return send_file(effect_preview(effect_id), mimetype="image/png", conditional=True)

    @app.post("/api/effect-presets")
    def preset_save():
        body = _request_json()
        preset = save_preset(body.get("name"), body.get("fixed_ids") or [], body.get("random_ids") or [])
        return jsonify({"ok": True, "preset": preset})

    @app.delete("/api/effect-presets/<preset_id>")
    def preset_delete(preset_id):
        if not delete_preset(preset_id):
            raise FileNotFoundError("特效配置不存在")
        return jsonify({"ok": True})

    @app.get("/api/subtitle-fonts")
    def subtitle_fonts():
        return jsonify({"ok": True, "fonts": font_catalog()})

    @app.get("/api/subtitle-fonts/<font_id>")
    def subtitle_font_file(font_id):
        path, spec = resolve_font(font_id)
        return send_file(path, mimetype=spec["mimetype"], conditional=True)

    @app.post("/api/subtitles/import-paths")
    def subtitle_import_paths():
        body = _request_json()
        paths = body.get("paths")
        if not isinstance(paths, list):
            raise ValueError("paths 必须是数组")
        return jsonify({"ok": True, "subtitles": [cache_subtitle_file(path) for path in paths]})

    @app.post("/api/subtitles/upload")
    def subtitle_upload():
        uploads = request.files.getlist("files")
        if not uploads:
            raise ValueError("请选择 SRT 字幕")
        items = []
        for upload in uploads:
            data = upload.stream.read(MAX_SUBTITLE_BYTES + 1)
            items.append(cache_subtitle_bytes(data, upload.filename or "subtitle.srt"))
        return jsonify({"ok": True, "subtitles": items})

    @app.post("/api/ai-landscape/subtitle-preview-frame")
    def subtitle_preview():
        body = _request_json()
        result = render_preview(body.get("text"), body.get("layout"), body.get("style"))
        return jsonify({
            "ok": True,
            "cache_key": result["cache_key"],
            "generated": result["generated"],
            "url": f"/api/ai-landscape/subtitle-preview-frame/{result['cache_key']}.png",
        })

    @app.get("/api/ai-landscape/subtitle-preview-frame/<filename>")
    def subtitle_preview_file(filename):
        return send_file(preview_path(Path(filename).stem), mimetype="image/png", conditional=True)

    @app.post("/api/ai-landscape")
    def submit_job():
        body = _request_json()
        removed = sorted(
            key for key in body
            if key in REMOVED_FIELDS or any(key.startswith(prefix) for prefix in REMOVED_FIELD_PREFIXES)
        )
        if removed:
            raise ValueError(f"独立版不支持已移除参数: {', '.join(removed)}")
        unknown = sorted(set(body) - ALLOWED_JOB_FIELDS)
        if unknown:
            raise ValueError(f"请求包含未知参数: {', '.join(unknown)}")
        videos = _normalize_video_inputs(body.get("videos"))
        output_dir = validate_output_dir(body.get("output_dir"))
        try:
            workers = int(body.get("workers", 1))
            bitrate = int(body.get("output_video_bitrate_k", DEFAULT_VIDEO_BITRATE_K))
        except (TypeError, ValueError) as exc:
            raise ValueError("并发数和码率必须是整数") from exc
        if not 1 <= workers <= MAX_WORKERS:
            raise ValueError(f"编码并发必须在 1 到 {MAX_WORKERS} 之间")
        if not 300 <= bitrate <= 100000:
            raise ValueError("输出码率必须在 300 到 100000 kbps 之间")
        gpu_mode = str(body.get("gpu_mode") or "auto").strip().lower()
        if gpu_mode not in {"auto", "gpu", "cpu"}:
            raise ValueError("GPU模式只能是 auto、gpu 或 cpu")
        video_paths = [item["path"] for item in videos]
        bounds = _normalize_crop_bounds(body.get("ai_crop_bounds"), videos)
        subtitles = normalize_subtitle_configs(body.get("subtitle_configs"), video_paths)
        fixed_ids = body.get("effect_all_template_ids") or []
        random_ids = body.get("effect_random_template_ids") or []
        if not isinstance(fixed_ids, list) or not isinstance(random_ids, list):
            raise ValueError("特效 ID 列表必须是数组")
        job_id = start_job(
            videos,
            output_dir,
            workers=workers,
            gpu_mode=gpu_mode,
            output_video_bitrate_k=bitrate,
            ai_crop_bounds=bounds,
            subtitle_configs=subtitles,
            effect_all_template_ids=fixed_ids,
            effect_random_template_ids=random_ids,
        )
        return jsonify({"ok": True, "job_id": job_id, "job": get_job(job_id)}), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        job = get_job(job_id)
        if not job:
            return _json_error("任务不存在", 404)
        return jsonify({"ok": True, "job": job})

    @app.post("/api/jobs/<job_id>/cancel")
    def job_cancel(job_id):
        job = cancel_job(job_id)
        if not job:
            return _json_error("任务不存在", 404)
        return jsonify({"ok": True, "job": job})

    @app.post("/api/jobs/cancel-all")
    def jobs_cancel_all():
        return jsonify({"ok": True, "cancelled": cancel_all()})

    @app.get("/")
    @app.get("/<path:filename>")
    def frontend(filename="index.html"):
        root = static_root()
        requested = root / filename
        if filename != "index.html" and requested.is_file():
            return send_file(requested)
        index = root / "index.html"
        if index.is_file():
            return send_file(index)
        return _json_error("前端尚未构建，请先在 frontend 运行 npm run build", 503)

    return app


def run() -> None:
    app = create_app()
    port = int(os.environ.get("AI_LANDSCAPE_PORT", "5688"))
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    run()
