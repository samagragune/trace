"""Subprocess execution for tools: no shell, enforced timeout, process-tree kill, cancellation.

* ``argv`` is executed directly (``shell=False``); the program is resolved with ``shutil.which``
  so Windows ``.cmd``/``.bat`` shims (npm, pnpm, yarn...) work.
* stdin is closed (``DEVNULL``) so a command waiting for input fails fast instead of hanging.
* ``CI=true`` is set so test runners don't enter interactive or watch modes.
* Environment variables named in ``scrub_env`` (Trace's own provider API keys) are removed, so
  project code run by the agent can't read the user's model key.
* On timeout or cancellation the **whole process tree** is killed (``taskkill /T`` on Windows,
  a process group on POSIX), so test runners can't leave orphaned workers behind.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

POLL_SECONDS = 0.2


@dataclass(frozen=True)
class ProcessResult:
    argv: list[str]
    exit_code: int | None  # None when killed (timeout/cancel)
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not (self.timed_out or self.cancelled)

    def combined_output(self) -> str:
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append(("[stderr]\n" if self.stdout.strip() else "") + self.stderr.rstrip())
        return "\n".join(parts)


def build_env(scrub: Iterable[str], base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    scrub_upper = {name.upper() for name in scrub}
    for name in list(env):
        if name.upper() in scrub_upper:
            del env[name]
    env.setdefault("CI", "true")
    return env


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_process(
    argv: list[str],
    cwd: Path,
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    scrub_env: Iterable[str] = (),
) -> ProcessResult:
    env = build_env(scrub_env)
    program = shutil.which(argv[0], path=env.get("PATH"))
    if program is None:
        return ProcessResult(argv, None, "", f"command not found: {argv[0]}", 0.0, not_found=True)
    started = time.perf_counter()
    kwargs: dict[str, object] = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True  # own process group -> killpg kills the tree
    proc = subprocess.Popen(
        [program, *argv[1:]],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,  # type: ignore[arg-type]
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def pump(stream, sink: list[bytes]) -> None:  # noqa: ANN001
        for chunk in iter(lambda: stream.read(8192), b""):
            sink.append(chunk)

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, stderr_chunks), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = cancelled = False
    deadline = started + timeout
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _kill_tree(proc)
            break
        if time.perf_counter() >= deadline:
            timed_out = True
            _kill_tree(proc)
            break
        time.sleep(POLL_SECONDS)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
    for reader in readers:
        reader.join(timeout=5)
    return ProcessResult(
        argv=argv,
        exit_code=None if (timed_out or cancelled) else proc.returncode,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        duration_s=time.perf_counter() - started,
        timed_out=timed_out,
        cancelled=cancelled,
    )
