from __future__ import annotations

from collections import defaultdict
import os
import subprocess
import threading
import time
from typing import Iterable


class CommandCancelled(RuntimeError):
    pass


class ProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, set[subprocess.Popen]] = defaultdict(set)

    def add(self, job_id: str, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes[job_id].add(process)

    def remove(self, job_id: str, process: subprocess.Popen) -> None:
        with self._lock:
            group = self._processes.get(job_id)
            if not group:
                return
            group.discard(process)
            if not group:
                self._processes.pop(job_id, None)

    def terminate_job(self, job_id: str) -> None:
        with self._lock:
            processes = list(self._processes.get(job_id) or [])
        for process in processes:
            _terminate_process_tree(process)


PROCESS_REGISTRY = ProcessRegistry()


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except Exception:
            pass
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_command(
    command: Iterable[str],
    *,
    job_id: str = "",
    cancel_event: threading.Event | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    command = [str(item) for item in command]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    if job_id:
        PROCESS_REGISTRY.add(job_id, process)
    started = time.monotonic()
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                _terminate_process_tree(process)
                process.communicate()
                raise CommandCancelled("任务已取消")
            elapsed = time.monotonic() - started
            if timeout and elapsed > timeout:
                _terminate_process_tree(process)
                process.communicate()
                raise TimeoutError(f"命令执行超时（{timeout:.0f}s）")
            wait_seconds = min(0.1, max(0.01, timeout - elapsed)) if timeout else 0.1
            try:
                stdout, stderr = process.communicate(timeout=wait_seconds)
                break
            except subprocess.TimeoutExpired:
                continue
        return int(process.returncode or 0), stdout or "", stderr or ""
    finally:
        if job_id:
            PROCESS_REGISTRY.remove(job_id, process)
