from __future__ import annotations

import sys

from backend.processes import run_command


def test_run_command_drains_large_stdout_while_process_is_running():
    payload_size = 256 * 1024
    returncode, stdout, stderr = run_command(
        [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {payload_size})"],
        timeout=10,
    )

    assert returncode == 0
    assert len(stdout) == payload_size
    assert stderr == ""
