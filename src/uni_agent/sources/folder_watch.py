"""Watches the course folders, decides lecture vs lab, and hands stable new files to a callback.

Kind rules, in order:
1. A `lectures/` or `labs/` folder anywhere under the course folder forces the kind.
2. Filename keywords (lab/tutorial/practice... vs lecture/L01/slides...).
3. Otherwise None, and the orchestrator asks "Lecture or Lab?" on Telegram.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..extract import SUPPORTED

log = logging.getLogger(__name__)

LECTURE_DIRS = {"lecture", "lectures"}
LAB_DIRS = {"lab", "labs"}
# Words must not be glued to a preceding letter, so "syllabus"/"collaboration" are not labs.
LAB_WORDS = re.compile(r"(?:^|[^a-z])(?:lab|tutorial|tut[\s_-]*\d|practical|practice|worksheet|exercise|assignment)")
LECTURE_WORDS = re.compile(r"lecture|slides|chapter|(?:^|[^a-z])l\d{1,2}(?:[^0-9]|$)")
TEMP_PATTERNS = ["~$*", "*.tmp", "*.crdownload", "*.part", "*.download", ".~lock*"]


@dataclass(frozen=True)
class FoundFile:
    path: Path
    course_folder: Path
    kind: str | None  # 'lecture' | 'lab' | None (unsure)


def guess_kind(path: Path, course_folder: Path) -> str | None:
    try:
        rel_dirs = [p.lower() for p in path.relative_to(course_folder).parts[:-1]]
    except ValueError:
        rel_dirs = []
    for d in rel_dirs:
        if d in LAB_DIRS:
            return "lab"
        if d in LECTURE_DIRS:
            return "lecture"
    name = path.stem.lower()
    is_lab = bool(LAB_WORDS.search(name))
    is_lecture = bool(LECTURE_WORDS.search(name))
    if is_lab and not is_lecture:
        return "lab"
    if is_lecture and not is_lab:
        return "lecture"
    return None


def is_candidate(path: Path, ignore_patterns: list[str]) -> bool:
    name = path.name
    if path.suffix.lower() not in SUPPORTED:
        return False
    return not any(fnmatch.fnmatch(name.lower(), p.lower()) for p in [*TEMP_PATTERNS, *ignore_patterns])


def course_folder_for(path: Path, course_folders: list[Path]) -> Path | None:
    for folder in course_folders:
        try:
            path.resolve().relative_to(folder.resolve())
            return folder
        except ValueError:
            continue
    return None


def scan(course_folders: list[Path], ignore_patterns: list[str]) -> list[FoundFile]:
    found = []
    for folder in course_folders:
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*")):
            if p.is_file() and is_candidate(p, ignore_patterns):
                found.append(FoundFile(p, folder, guess_kind(p, folder)))
    return found


class _Handler(FileSystemEventHandler):
    def __init__(self, on_path: Callable[[Path], None]):
        self.on_path = on_path

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.on_path(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.on_path(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:  # browser downloads: x.crdownload -> x.pdf
        if not event.is_directory:
            self.on_path(Path(event.dest_path))


class FolderWatcher:
    """Bridges watchdog's thread to asyncio. A file is handed on once its size is stable for `settle` s."""

    def __init__(self, course_folders: list[Path], ignore_patterns: list[str],
                 callback: Callable[[FoundFile], Awaitable[None]], settle: float = 3.0):
        self.course_folders = [f for f in course_folders if f.is_dir()]
        self.ignore_patterns = ignore_patterns
        self.callback = callback
        self.settle = settle
        self._pending: set[Path] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observer: Observer | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._observer = Observer()
        handler = _Handler(lambda p: self._loop.call_soon_threadsafe(self._enqueue, p))
        for folder in self.course_folders:
            self._observer.schedule(handler, str(folder), recursive=True)
        self._observer.start()
        log.info("Watching %d course folders", len(self.course_folders))

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)

    def _enqueue(self, path: Path) -> None:
        if path in self._pending or not is_candidate(path, self.ignore_patterns):
            return
        folder = course_folder_for(path, self.course_folders)
        if folder is None:
            return
        self._pending.add(path)
        asyncio.ensure_future(self._settle_then_handle(path, folder))

    async def _settle_then_handle(self, path: Path, folder: Path) -> None:
        try:
            last = -1
            for _ in range(60):  # give up after ~3 min of the file still changing
                await asyncio.sleep(self.settle)
                if not path.exists():
                    return
                size = path.stat().st_size
                if size == last and size > 0:
                    break
                last = size
            await self.callback(FoundFile(path, folder, guess_kind(path, folder)))
        except Exception:
            log.exception("Failed handling %s", path.name)
        finally:
            self._pending.discard(path)
