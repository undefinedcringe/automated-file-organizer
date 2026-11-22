#!/usr/bin/env python3
"""Sort files into extension folders and quarantine exact duplicates.

The default run is a preview. Pass --apply to move files.

Safety guarantees:
  * Files are never deleted and never overwritten.
  * Duplicates are matched by SHA-256 and moved to a review folder.
  * Symlinks, devices, pipes, and hidden files are left alone by default.
  * Files that change between scanning and moving are skipped.
  * System directories, your home directory, hidden config folders, and
    source-control repositories are refused or left intact.

Usage:
  python3 file_organizer.py ~/Downloads            # preview
  python3 file_organizer.py ~/Downloads --apply    # move files
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_OUTPUT_NAME = "Organized"
DUPLICATES_DIR = "_duplicates"
NO_EXTENSION_DIR = "_no_extension"
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
JOURNAL_SUFFIX = ".journal.jsonl"
JOURNAL_VERSION = 1

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_INVALID_USAGE = 2
EXIT_INTERRUPTED = 130

SYSTEM_DIRS = tuple(
    Path(name).resolve()
    for name in (
        "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/opt",
        "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/usr", "/var",
    )
)
VCS_MARKERS = frozenset({".git", ".hg", ".svn"})
MAX_NAME_BYTES = 255

logger = logging.getLogger("file_organizer")


class OrganizerError(Exception):
    """Base class for expected organizer errors."""


class UnsafePathError(OrganizerError):
    """A requested path is unsafe to organize."""


class FileChangedError(OrganizerError):
    """A file changed after it was scanned."""


@dataclass(frozen=True)
class Config:
    source: Path
    output: Path
    apply: bool = False
    recursive: bool = False
    include_hidden: bool = False
    excluded: frozenset[Path] = frozenset()


@dataclass
class Summary:
    scanned: int = 0
    moved: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class PlannedMove:
    entry: FileEntry
    folder: Path
    duplicate_of: Path | None = None


def folder_for(path: Path) -> str:
    """Return a safe folder name for a file's extension."""
    extension = path.suffix.lower().removeprefix(".")
    # Stripping underscores keeps extensions from matching reserved folders.
    cleaned = re.sub(r"[^\w-]", "_", extension).strip("_")
    return cleaned or NO_EXTENSION_DIR


def validate_paths(source: Path, output: Path) -> tuple[Path, Path]:
    """Resolve both paths and reject unsafe combinations."""
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()

    for path in (source, output):
        _reject_protected(path)
    if not source.is_dir():
        raise UnsafePathError(f"Source is not a directory: {str(source)!r}")
    if _is_repository(source):
        raise UnsafePathError(f"Refusing source-control repository: {str(source)!r}")
    if source == output:
        raise UnsafePathError("Source and output must be different folders.")
    if source.is_relative_to(output):
        raise UnsafePathError("Source cannot be inside the output folder.")
    if output.exists() and not output.is_dir():
        raise UnsafePathError(f"Output is not a directory: {str(output)!r}")
    return source, output


def _is_repository(path: Path) -> bool:
    return any(os.path.lexists(path / marker) for marker in VCS_MARKERS)


def _reject_protected(path: Path) -> None:
    home = Path.home().resolve()
    if home.is_relative_to(path) or path.parent == Path("/home"):
        raise UnsafePathError(f"Refusing a home directory or parent: {str(path)!r}")
    if path.is_relative_to(home) and any(
        part.startswith(".") for part in path.relative_to(home).parts
    ):
        raise UnsafePathError(f"Refusing hidden folder in home: {str(path)!r}")
    if any(path.is_relative_to(system_dir) for system_dir in SYSTEM_DIRS):
        raise UnsafePathError(f"Refusing system directory: {str(path)!r}")


def iter_files(
    root: Path,
    *,
    recursive: bool,
    include_hidden: bool,
    excluded: frozenset[Path],
    summary: Summary | None = None,
) -> Iterator[FileEntry]:
    """Yield regular files without following symlinks."""

    def record_skip(message: str, *args: object) -> None:
        logger.info("SKIP " + message, *args)
        if summary is not None:
            summary.skipped += 1

    def on_walk_error(error: OSError) -> None:
        logger.error("SCAN FAILED %r: %s", error.filename, error.strerror)
        if summary is not None:
            summary.failed += 1

    for current, dirnames, filenames in os.walk(root, onerror=on_walk_error):
        directory = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            path = directory / name
            if path.is_symlink():
                record_skip("symlinked folder: %r", str(path))
            elif name in VCS_MARKERS or _is_repository(path):
                record_skip("source-control data: %r", str(path))
            elif path not in excluded and (include_hidden or not name.startswith(".")):
                kept_dirs.append(name)
        dirnames[:] = kept_dirs if recursive else []

        for name in sorted(filenames):
            path = directory / name
            if path in excluded:
                record_skip("protected path: %r", str(path))
            elif name.startswith(".") and not include_hidden:
                record_skip("hidden file: %r", str(path))
            else:
                entry = _read_entry(path, record_skip, summary)
                if entry is not None:
                    yield entry


def _read_entry(
    path: Path, record_skip: Callable[..., None], summary: Summary | None
) -> FileEntry | None:
    try:
        info = path.lstat()
    except OSError as error:
        logger.error("STAT FAILED %r: %s", str(path), error)
        if summary is not None:
            summary.failed += 1
        return None
    if not stat.S_ISREG(info.st_mode):
        record_skip("not a regular file: %r", str(path))
        return None
    return FileEntry(path, info.st_size, info.st_mtime_ns)


def _open_regular(path: Path) -> io.BufferedReader:
    """Open a regular file, refusing symlinks and blocking special files."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise FileChangedError(f"No longer a regular file: {str(path)!r}")
    return os.fdopen(descriptor, "rb")


def hash_file(path: Path) -> str:
    with _open_regular(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_plan(
    config: Config, new_files: list[FileEntry], summary: Summary
) -> list[PlannedMove]:
    """Assign each file a destination, marking exact duplicates."""
    existing = _existing_output_files(config.output, config.excluded)
    size_counts = Counter(entry.size for entry in [*existing, *new_files])

    def needs_hash(entry: FileEntry) -> bool:
        # Empty files are often meaningful placeholders, so never flag them.
        return entry.size > 0 and size_counts[entry.size] > 1

    originals: dict[tuple[int, str], Path] = {}
    for entry in existing:
        if needs_hash(entry):
            try:
                originals.setdefault((entry.size, hash_file(entry.path)), entry.path)
            except (OSError, FileChangedError) as error:
                logger.warning("HASH FAILED %r: %s", str(entry.path), error)

    plan: list[PlannedMove] = []
    # The oldest copy is kept as the original.
    for entry in sorted(new_files, key=lambda item: (item.mtime_ns, str(item.path))):
        folder_name = folder_for(entry.path)
        folder = config.output / folder_name
        duplicate_of: Path | None = None
        if needs_hash(entry):
            try:
                key = (entry.size, hash_file(entry.path))
            except (OSError, FileChangedError) as error:
                logger.error("HASH FAILED %r: %s", str(entry.path), error)
                summary.failed += 1
                continue
            duplicate_of = originals.get(key)
            if duplicate_of is None:
                originals[key] = entry.path
            else:
                folder = config.output / DUPLICATES_DIR / folder_name
        plan.append(PlannedMove(entry, folder, duplicate_of))
    return plan


def _existing_output_files(
    output: Path, excluded: frozenset[Path]
) -> list[FileEntry]:
    if not output.is_dir():
        return []
    return list(
        iter_files(
            output,
            recursive=True,
            include_hidden=True,
            excluded=excluded | {output / DUPLICATES_DIR},
        )
    )


def _numbered_name(original: Path, counter: int) -> str:
    """Add a counter, trimming whole characters to fit the filename limit."""
    ending = f" ({counter}){original.suffix}"
    budget = MAX_NAME_BYTES - len(os.fsencode(ending))
    stem = original.stem
    while stem and len(os.fsencode(stem)) > budget:
        stem = stem[:-1]
    return f"{stem}{ending}"


def unique_destination(folder: Path, name: str, reserved: set[Path]) -> Path:
    """Return an unused path, adding ' (n)' before the suffix if needed."""
    candidate = folder / name
    original = Path(name)
    counter = 1
    while candidate in reserved or os.path.lexists(candidate):
        candidate = folder / _numbered_name(original, counter)
        counter += 1
    return candidate


def ensure_unchanged(entry: FileEntry) -> None:
    info = entry.path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size != entry.size
        or info.st_mtime_ns != entry.mtime_ns
    ):
        raise FileChangedError(f"File changed after scan: {str(entry.path)!r}")


def move_without_overwrite(entry: FileEntry, destination: Path) -> None:
    """Move a file, raising FileExistsError rather than replacing anything."""
    ensure_unchanged(entry)
    try:
        # link() fails atomically if the destination already exists.
        os.link(entry.path, destination, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError:
        # Hard links can fail across drives or on some filesystems.
        _copy_verified(entry, destination)
    try:
        entry.path.unlink()
    except OSError:
        # Roll back only while the source survives; otherwise this is the last copy.
        if os.path.lexists(entry.path):
            with suppress(OSError):
                destination.unlink()
        raise


def _copy_verified(entry: FileEntry, destination: Path) -> None:
    source = entry.path
    # Created outside the try so an existing file is never cleaned up.
    # Mode 0600 keeps private content private until permissions are restored.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target, _open_regular(source) as reader:
            shutil.copyfileobj(reader, target)
        shutil.copystat(source, destination, follow_symlinks=False)
        # A copy must not gain setuid/setgid under a different owner.
        safe_mode = stat.S_IMODE(destination.lstat().st_mode)
        destination.chmod(safe_mode & ~(stat.S_ISUID | stat.S_ISGID))
        if hash_file(source) != hash_file(destination):
            raise FileChangedError(f"Copy verification failed: {str(source)!r}")
        ensure_unchanged(entry)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


class MoveJournal:
    """Durable, private record of a run, used by undo_organizer.py.

    Each move is recorded before it happens, so a crash cannot leave a moved
    file that undo does not know about.
    """

    def __init__(self, path: Path, source: Path, output: Path) -> None:
        self.path = path
        self._source = source
        self._output = output
        self._stream: io.TextIOWrapper | None = None

    @property
    def used(self) -> bool:
        return self._stream is not None

    def _write(self, record: dict[str, object]) -> None:
        if self._stream is None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            self._stream = os.fdopen(descriptor, "w", encoding="ascii")
            self._write(
                {
                    "type": "header",
                    "version": JOURNAL_VERSION,
                    "source": str(self._source),
                    "output": str(self._output),
                }
            )
        # ASCII JSON escapes newlines and non-UTF-8 file names losslessly.
        self._stream.write(json.dumps(record) + "\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def record_folder(self, folder: Path) -> None:
        self._write({"type": "folder", "path": str(folder)})

    def begin(self, entry: FileEntry, destination: Path) -> None:
        self._write(
            {
                "type": "move",
                "source": str(entry.path),
                "destination": str(destination),
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
            }
        )

    def finish(self, destination: Path) -> None:
        try:
            info = destination.lstat()
            self._write(
                {
                    "type": "done",
                    "destination": str(destination),
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
            )
        except OSError as error:
            logger.error("JOURNAL FAILED %r: %s", str(destination), error)

    def abort(self, destination: Path) -> None:
        try:
            self._write({"type": "abort", "destination": str(destination)})
        except OSError as error:
            logger.error("JOURNAL FAILED %r: %s", str(destination), error)

    def close(self) -> None:
        if self._stream is not None:
            with suppress(OSError):
                self._stream.close()


def reject_symlinks(root: Path, path: Path) -> None:
    """Raise if any folder from root down to path is a symlink."""
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise UnsafePathError(f"Refusing symlinked folder: {str(current)!r}")


def _missing_folders(folder: Path) -> list[Path]:
    """Return folders that must be created, outermost first."""
    missing: list[Path] = []
    current = folder
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    return missing[::-1]


def prepare_folder(
    output: Path, folder: Path, journal: MoveJournal | None = None
) -> None:
    """Create a destination folder, refusing symlinks below the output root."""
    reject_symlinks(output, folder)
    if not folder.is_dir():
        if journal is not None:
            for missing in _missing_folders(folder):
                journal.record_folder(missing)
        folder.mkdir(parents=True, exist_ok=True)
        logger.info("APPLY CREATE FOLDER %r", str(folder))


def execute_plan(
    config: Config,
    plan: Iterable[PlannedMove],
    summary: Summary,
    journal: MoveJournal | None = None,
) -> None:
    prefix = "APPLY" if config.apply else "DRY-RUN"
    reserved: set[Path] = set()

    for move in plan:
        source = move.entry.path
        destination = unique_destination(move.folder, source.name, reserved)
        try:
            if config.apply:
                prepare_folder(config.output, move.folder, journal)
                destination = _move_with_retry(move, destination, reserved, journal)
        except (OSError, OrganizerError) as error:
            logger.error("FAILED %r: %s", str(source), error)
            summary.failed += 1
            continue

        reserved.add(destination)
        if move.duplicate_of is None:
            summary.moved += 1
            logger.info("%s MOVE %r -> %r", prefix, str(source), str(destination))
        else:
            summary.duplicates += 1
            logger.warning(
                "%s DUPLICATE %r (same as %r) -> %r",
                prefix, str(source), str(move.duplicate_of), str(destination),
            )


def _move_with_retry(
    move: PlannedMove,
    destination: Path,
    reserved: set[Path],
    journal: MoveJournal | None = None,
) -> Path:
    """Pick a new name if another process claims the destination first."""
    while True:
        # A journal failure raises here, before the file is touched.
        if journal is not None:
            journal.begin(move.entry, destination)
        try:
            move_without_overwrite(move.entry, destination)
        except FileExistsError:
            if journal is not None:
                journal.abort(destination)
            reserved.add(destination)
            destination = unique_destination(
                move.folder, move.entry.path.name, reserved
            )
        except (OSError, OrganizerError):
            # If the source vanished, the destination may be the last copy.
            if journal is not None and os.path.lexists(move.entry.path):
                journal.abort(destination)
            raise
        else:
            if journal is not None:
                journal.finish(destination)
            return destination


def organize(config: Config, journal: MoveJournal | None = None) -> Summary:
    """Scan, plan, and either preview or apply the file moves."""
    summary = Summary()
    files = list(
        iter_files(
            config.source,
            recursive=config.recursive,
            include_hidden=config.include_hidden,
            excluded=config.excluded | {config.output},
            summary=summary,
        )
    )
    summary.scanned = len(files)
    logger.info("Scanned %d file(s) in %r", summary.scanned, str(config.source))
    execute_plan(config, build_plan(config, files, summary), summary, journal)
    return summary


class ConsoleHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Console output that stops quietly if its pipe closes (e.g. `| head`)."""

    def handleError(self, record: logging.LogRecord) -> None:
        if not isinstance(sys.exc_info()[1], BrokenPipeError):
            super().handleError(record)
            return
        logger.removeHandler(self)
        # Redirect stdout so Python's exit-time flush cannot raise again.
        with suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)


def configure_logging(log_dir: Path) -> Path:
    """Log details to a timestamped file and progress to the console."""
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = log_dir / f"organizer_{timestamp}.log"
    # Create privately first: logs reveal file names and folder structure.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    os.close(os.open(log_path, flags, 0o600))
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    )
    console_handler = ConsoleHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers[:] = [file_handler, console_handler]
    return log_path


def close_logging() -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sort files by extension and quarantine exact duplicates."
    )
    parser.add_argument("source", type=Path, help="folder to organize")
    parser.add_argument(
        "-o", "--output", type=Path,
        help=f"destination folder (default: SOURCE/{DEFAULT_OUTPUT_NAME})",
    )
    parser.add_argument(
        "--apply", action="store_true", help="move files (default is a preview)"
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="include subfolders"
    )
    parser.add_argument(
        "--include-hidden", action="store_true", help="include dotfiles and dot folders"
    )
    parser.add_argument(
        "--log-dir", type=Path, default=DEFAULT_LOG_DIR,
        help="folder for log files (default: logs/ beside this script)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    log_dir = args.log_dir.expanduser().resolve()
    try:
        log_path = configure_logging(log_dir)
    except OSError as error:
        print(f"ERROR: cannot create log file in {log_dir}: {error}", file=sys.stderr)
        return EXIT_INVALID_USAGE

    journal: MoveJournal | None = None
    try:
        output = args.output or args.source.expanduser() / DEFAULT_OUTPUT_NAME
        source, output = validate_paths(args.source, output)
        if source.is_relative_to(log_dir):
            raise UnsafePathError("Source cannot be inside the log folder.")
        config = Config(
            source=source,
            output=output,
            apply=args.apply,
            recursive=args.recursive,
            include_hidden=args.include_hidden,
            excluded=frozenset({Path(__file__).resolve(), log_dir}),
        )
        logger.info("Mode: %s", "APPLY" if config.apply else "DRY-RUN (no changes)")
        logger.info("Source: %r | Output: %r", str(source), str(output))

        if config.apply:
            journal_path = log_path.with_name(log_path.stem + JOURNAL_SUFFIX)
            journal = MoveJournal(journal_path, source, output)
        summary = organize(config, journal)

        logger.info(
            "Summary: scanned=%d moved=%d duplicates=%d skipped=%d failed=%d",
            summary.scanned, summary.moved, summary.duplicates,
            summary.skipped, summary.failed,
        )
        if not config.apply:
            logger.info("Preview only. Re-run with --apply to move files.")
        logger.info("Log saved to %r", str(log_path))
        if journal is not None and journal.used:
            logger.info("To reverse this run: python3 undo_organizer.py")
        return EXIT_PARTIAL_FAILURE if summary.failed else EXIT_OK
    except UnsafePathError as error:
        logger.error("%s", error)
        return EXIT_INVALID_USAGE
    except KeyboardInterrupt:
        logger.warning("Interrupted. Files already moved remain in place.")
        return EXIT_INTERRUPTED
    finally:
        if journal is not None:
            journal.close()
        close_logging()


if __name__ == "__main__":
    raise SystemExit(main())
