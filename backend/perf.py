from __future__ import annotations

import json
from pathlib import Path
import threading
import time

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


class ResourceSampler:
    def __init__(self, interval: float = 0.5) -> None:
        self.interval = max(0.1, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rows: list[dict] = []

    def start(self) -> None:
        if not psutil or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        root = psutil.Process()
        while not self._stop.wait(self.interval):
            try:
                processes = [root, *root.children(recursive=True)]
                rss = sum(item.memory_info().rss for item in processes if item.is_running())
                cpu = sum(item.cpu_percent(None) for item in processes if item.is_running())
                self._rows.append({"rss": rss, "cpu": cpu})
            except Exception:
                continue

    def finish(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if not self._rows:
            return {
                "resource_sample_count": 0,
                "process_rss_average_mb": None,
                "process_rss_peak_mb": None,
                "process_cpu_average_percent": None,
                "process_cpu_peak_percent": None,
            }
        rss_values = [row["rss"] / 1024 / 1024 for row in self._rows]
        cpu_values = [row["cpu"] for row in self._rows]
        return {
            "resource_sample_count": len(self._rows),
            "process_rss_average_mb": round(sum(rss_values) / len(rss_values), 2),
            "process_rss_peak_mb": round(max(rss_values), 2),
            "process_cpu_average_percent": round(sum(cpu_values) / len(cpu_values), 2),
            "process_cpu_peak_percent": round(max(cpu_values), 2),
        }


def append_jsonl(path: str | Path, record: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {"timestamp": time.time(), **record}
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
