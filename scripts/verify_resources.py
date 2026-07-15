from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    ROOT / "frameshift/models/face_detection_yunet_2023mar.onnx": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ROOT / "assets/fonts/SourceHanSansSC-Heavy.otf": "6374b11bc4c2cd4bd7be1a1d64cf5047906c8a6a025c64e023c6792e50ba985e",
    ROOT / "assets/fonts/SourceHanSerifSC-Heavy.otf": "d033af54f96530476faed924ab5d5e9e6ef0833495670fd57bab9a7758398048",
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    errors = []
    for path, expected in EXPECTED.items():
        if not path.is_file():
            errors.append(f"missing: {path.relative_to(ROOT)}")
        elif digest(path) != expected:
            errors.append(f"sha256 mismatch: {path.relative_to(ROOT)}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Resource hashes verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
