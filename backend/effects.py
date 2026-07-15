from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
import uuid

from .config import ffmpeg_path, ffprobe_path, user_data_root
from .media import EFFECT_EXTENSIONS
from .processes import run_command


_LOCK = threading.RLock()


def effect_root() -> Path:
    path = user_data_root() / "effects"
    path.mkdir(parents=True, exist_ok=True)
    (path / "files").mkdir(exist_ok=True)
    (path / "previews").mkdir(exist_ok=True)
    return path


def _manifest_path() -> Path:
    return effect_root() / "manifest.json"


def _default_manifest() -> dict:
    return {"version": 1, "effects": [], "presets": []}


def _load() -> dict:
    path = _manifest_path()
    if not path.is_file():
        return _default_manifest()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        value.setdefault("effects", [])
        value.setdefault("presets", [])
        return value
    except (OSError, ValueError, TypeError):
        return _default_manifest()


def _save(document: dict) -> None:
    path = _manifest_path()
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _probe(path: Path) -> dict:
    command = [
        ffprobe_path(), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json", str(path),
    ]
    returncode, stdout, stderr = run_command(command, timeout=30)
    if returncode != 0:
        raise ValueError(f"无法读取特效文件: {stderr.strip()[-400:]}")
    value = json.loads(stdout or "{}")
    stream = next(iter(value.get("streams") or []), {})
    duration = stream.get("duration") or (value.get("format") or {}).get("duration") or 0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": round(float(duration or 0), 3),
    }


def _preview(effect_id: str, source: Path) -> Path:
    target = effect_root() / "previews" / f"{effect_id}.png"
    command = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-frames:v", "1",
        "-vf", "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2:color=black@0",
        str(target),
    ]
    returncode, _, stderr = run_command(command, timeout=30)
    if returncode != 0 or not target.is_file():
        raise ValueError(f"无法生成特效预览: {stderr.strip()[-400:]}")
    return target


def list_effects() -> dict:
    with _LOCK:
        document = _load()
        items = []
        changed = False
        for item in document["effects"]:
            copy = dict(item)
            source = effect_root() / "files" / copy.get("stored_name", "")
            copy["available"] = source.is_file()
            copy["source_url"] = f"/api/effects/{copy['id']}/source"
            copy["preview_url"] = f"/api/effects/{copy['id']}/preview"
            if not source.is_file() and copy.get("available", True):
                changed = True
            items.append(copy)
        if changed:
            _save(document)
        return {"effects": items, "presets": list(document["presets"])}


def import_effect_file(path: str | Path, display_name: str | None = None) -> dict:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"特效文件不存在: {source}")
    extension = source.suffix.lower()
    if extension not in EFFECT_EXTENSIONS:
        raise ValueError("特效仅支持 GIF、MOV 或 WebM")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    effect_id = digest[:24]
    with _LOCK:
        document = _load()
        existing = next((item for item in document["effects"] if item["id"] == effect_id), None)
        if existing:
            return dict(existing)
        stored_name = f"{effect_id}{extension}"
        target = effect_root() / "files" / stored_name
        shutil.copy2(source, target)
        metadata = _probe(target)
        try:
            _preview(effect_id, target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        item = {
            "id": effect_id,
            "name": str(display_name or source.stem).strip() or source.stem,
            "original_name": source.name,
            "stored_name": stored_name,
            "created_at": int(time.time()),
            **metadata,
        }
        document["effects"].append(item)
        _save(document)
        return dict(item)


def import_effect_bytes(data: bytes, original_name: str) -> dict:
    suffix = Path(original_name).suffix.lower()
    if suffix not in EFFECT_EXTENSIONS:
        raise ValueError("特效仅支持 GIF、MOV 或 WebM")
    temporary = effect_root() / f"upload-{uuid.uuid4().hex}{suffix}"
    temporary.write_bytes(data)
    try:
        return import_effect_file(temporary, Path(original_name).stem)
    finally:
        temporary.unlink(missing_ok=True)


def effect_item(effect_id: str) -> dict:
    normalized = str(effect_id or "").strip()
    with _LOCK:
        item = next((entry for entry in _load()["effects"] if entry["id"] == normalized), None)
    if not item:
        raise FileNotFoundError("特效不存在")
    return dict(item)


def effect_source(effect_id: str) -> Path:
    item = effect_item(effect_id)
    path = effect_root() / "files" / item["stored_name"]
    if not path.is_file():
        raise FileNotFoundError(f"特效资源缺失: {item['name']}")
    return path


def effect_preview(effect_id: str) -> Path:
    effect_item(effect_id)
    path = effect_root() / "previews" / f"{effect_id}.png"
    if not path.is_file():
        raise FileNotFoundError("特效预览不存在")
    return path


def ensure_effects(effect_ids: list[str]) -> dict[str, dict]:
    result = {}
    for effect_id in effect_ids:
        normalized = str(effect_id or "").strip()
        if not normalized or normalized in result:
            continue
        item = effect_item(normalized)
        item["path"] = str(effect_source(normalized))
        result[normalized] = item
    return result


def rename_effect(effect_id: str, name: str) -> dict:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("特效名称不能为空")
    with _LOCK:
        document = _load()
        item = next((entry for entry in document["effects"] if entry["id"] == effect_id), None)
        if not item:
            raise FileNotFoundError("特效不存在")
        item["name"] = normalized_name[:100]
        _save(document)
        return dict(item)


def delete_effects(effect_ids: list[str]) -> int:
    requested = {str(item or "").strip() for item in effect_ids if str(item or "").strip()}
    with _LOCK:
        document = _load()
        removed = [item for item in document["effects"] if item["id"] in requested]
        document["effects"] = [item for item in document["effects"] if item["id"] not in requested]
        for preset in document["presets"]:
            preset["fixed_ids"] = [item for item in preset.get("fixed_ids", []) if item not in requested]
            preset["random_ids"] = [item for item in preset.get("random_ids", []) if item not in requested]
        _save(document)
        for item in removed:
            (effect_root() / "files" / item["stored_name"]).unlink(missing_ok=True)
            (effect_root() / "previews" / f"{item['id']}.png").unlink(missing_ok=True)
        return len(removed)


def save_preset(name: str, fixed_ids: list[str], random_ids: list[str]) -> dict:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("配置名称不能为空")
    available = ensure_effects(list(fixed_ids or []) + list(random_ids or []))
    fixed = [item for item in dict.fromkeys(fixed_ids or []) if item in available]
    random = [item for item in dict.fromkeys(random_ids or []) if item in available]
    with _LOCK:
        document = _load()
        preset = next((item for item in document["presets"] if item["name"] == normalized_name), None)
        if not preset:
            preset = {"id": uuid.uuid4().hex[:16], "name": normalized_name}
            document["presets"].append(preset)
        preset.update({"fixed_ids": fixed, "random_ids": random, "updated_at": int(time.time())})
        _save(document)
        return dict(preset)


def delete_preset(preset_id: str) -> bool:
    with _LOCK:
        document = _load()
        before = len(document["presets"])
        document["presets"] = [item for item in document["presets"] if item["id"] != preset_id]
        changed = len(document["presets"]) != before
        if changed:
            _save(document)
        return changed
