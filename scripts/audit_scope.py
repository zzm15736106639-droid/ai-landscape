from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = [ROOT / "backend", ROOT / "frameshift", ROOT / "frontend/src", ROOT / "desktop"]
FORBIDDEN_EVERYWHERE = [
    "VideoBatchPage",
    "start_video_batch_job",
    "temp_video.mp4",
    "magic-ai-landscape",
    "../app.py",
    "../engine.py",
]
FORBIDDEN_FRONTEND = [
    "blur_regions",
    "frame_drop",
    "speedup_",
    "trim_head",
    "trim_tail",
    "output_duration",
    "tail_video",
    "video_transforms",
]


def source_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".py", ".js", ".jsx", ".css"}:
            yield path


def main() -> int:
    errors = []
    for root in SOURCE_ROOTS:
        for path in source_files(root):
            text = path.read_text(encoding="utf-8", errors="replace")
            lowered = text.lower()
            for token in FORBIDDEN_EVERYWHERE:
                if token.lower() in lowered:
                    errors.append(f"{path.relative_to(ROOT)} contains {token}")
            if root == ROOT / "frontend/src":
                for token in FORBIDDEN_FRONTEND:
                    if token in lowered:
                        errors.append(f"{path.relative_to(ROOT)} contains removed feature {token}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Open-source scope audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
