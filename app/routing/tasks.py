"""The Map data screen's work: downloading extracts and building maps, one
task at a time, each a list of `python -m app manage ...` steps run in a child
process.

A child process rather than a thread because a build holds a gigabyte or more
while it runs and hands it all back when it exits, and because a download or
build that is cancelled is simply killed. Its output is kept (the last
MAX_LINES lines) for the screen to show while it runs.
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Where `python -m app` finds the app package: this checkout (or /app in Docker).
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

MAX_LINES = 500


@dataclass
class Task:
    id: int
    title: str
    steps: list[list[str]]
    state: str = "running"          # running, done, failed, cancelled
    step: int = 0
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=MAX_LINES))
    then: Callable[[], str] | None = None   # run after the last step succeeds; returns a line to log
    process: subprocess.Popen | None = None


class Runner:
    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or PROJECT_ROOT
        self.task: Task | None = None
        self._next = 1
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self.task is not None and self.task.state == "running"

    def start(self, title: str, steps: list[list[str]], then: Callable[[], str] | None = None) -> Task:
        """steps are argument lists for `python -m app`."""
        with self._lock:
            if self.busy():
                raise RuntimeError("another map task is still running")
            task = Task(id=self._next, title=title, steps=steps, then=then)
            self._next += 1
            self.task = task
        threading.Thread(target=self._run, args=(task,), name="map-task", daemon=True).start()
        return task

    def cancel(self) -> bool:
        task = self.task
        if task is None or task.state != "running":
            return False
        task.state = "cancelled"
        if task.process is not None and task.process.poll() is None:
            task.process.terminate()
        return True

    def _run(self, task: Task) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        for index, argv in enumerate(task.steps):
            if task.state != "running":
                break
            task.step = index
            task.lines.append("$ manage " + " ".join(argv[1:]))
            try:
                task.process = subprocess.Popen(  # noqa: S603 -- our own interpreter and arguments
                    [sys.executable, "-m", "app", *argv], cwd=self.cwd, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
            except OSError as error:
                task.lines.append(f"could not start: {error}")
                task.state = "failed"
                break
            assert task.process.stdout is not None
            for line in task.process.stdout:
                task.lines.append(line.rstrip())
            code = task.process.wait()
            if task.state != "running":
                break
            if code != 0:
                task.lines.append(f"stopped: exit code {code}")
                task.state = "failed"
                break
        if task.state == "running" and task.then is not None:
            try:
                task.lines.append(task.then())
            except Exception as error:  # noqa: BLE001 -- shown on the screen
                task.lines.append(f"could not switch to the new map: {error}")
                task.state = "failed"
        if task.state == "running":
            task.state = "done"
        if task.state == "cancelled":
            task.lines.append("cancelled")
        task.finished = time.time()
        task.process = None
