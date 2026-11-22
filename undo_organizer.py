#!/usr/bin/env python3
"""Reverse a file_organizer.py run using its private journal.

The default run is a preview. Pass --apply to restore files.

Safety guarantees:
  * Files are never deleted and never overwritten.
  * A file edited since it was organized stays where it is.
  * Only empty folders created by the undone run are removed.
  * Journals that are tampered with or unsafe are rejected before any move.

Usage:
  python3 undo_organizer.py                # preview undoing the latest run
  python3 undo_organizer.py --apply        # undo the latest run
  python3 undo_organizer.py --list         # show runs that can be undone
  python3 undo_organizer.py JOURNAL --apply
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import file_organizer as fo

logger = fo.logger


class JournalError(fo.OrganizerError):
    """A journal is unreadable, malformed, or unsafe to trust."""


class MoveState(Enum):
    RESTORABLE = "restorable"
    INTERRUPTED = "same file at both paths after a crash"
    ALREADY_RESTORED = "already restored"
    MISSING = "missing"
    CONFLICT = "original path is occupied"
    CHANGED = "changed since it was organized"


RESTORABLE_STATES = frozenset({MoveState.RESTORABLE, MoveState.INTERRUPTED})


@dataclass(frozen=True)
class RecordedMove:
    source: Path
    destination: Path
    size: int
    mtime_ns: int

    @property
    def entry(self) -> fo.FileEntry:
        return fo.FileEntry(self.destination, self.size, self.mtime_ns)


@dataclass(frozen=True)
class Journal:
    path: Path
    source: Path
    output: Path
    moves: tuple[RecordedMove, ...]
    folders: tuple[Path, ...]


@dataclass
class UndoSummary:
    restored: int = 0
    already_restored: int = 0
    missing: int = 0
    conflicts: int = 0
    failed: int = 0
    folders_removed: int = 0


def _read_private_file(path: Path) -> str:
    """Read a journal only if it is a regular file only this user can change."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise JournalError(f"Cannot open journal {str(path)!r}: {error}") from error
    with os.fdopen(descriptor, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise JournalError(f"Journal is not a regular file: {str(path)!r}")
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise JournalError(f"Journal has unsafe ownership: {str(path)!r}")
        try:
            return stream.read()
        except UnicodeDecodeError as error:
            raise JournalError(f"Journal is corrupt: {str(path)!r}") from error


def _checked_path(value: Any) -> Path:
    if not isinstance(value, str) or "\0" in value:
        raise JournalError("Journal contains an invalid path.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise JournalError(f"Journal contains an unsafe path: {value!r}")
    return path


def _checked_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise JournalError("Journal contains an invalid number.")
    return value


def _inside(path: Path, root: Path) -> bool:
    return path != root and path.is_relative_to(root)


def _parse_move(record: dict[str, Any], source: Path, output: Path) -> RecordedMove:
    original = _checked_path(record["source"])
    destination = _checked_path(record["destination"])
    if not _inside(original, source) or original.is_relative_to(output):
        raise JournalError(f"Restore path is outside the source: {str(original)!r}")
    if not _inside(destination, output):
        raise JournalError(f"Moved file is outside output: {str(destination)!r}")
    return RecordedMove(
        original,
        destination,
        _checked_int(record["size"]),
        _checked_int(record["mtime_ns"]),
    )


def _parse_folder(record: dict[str, Any], output: Path) -> Path:
    folder = _checked_path(record["path"])
    if not folder.is_relative_to(output) and not output.is_relative_to(folder):
        raise JournalError(f"Folder is outside output: {str(folder)!r}")
    return folder


def _parse_header(record: dict[str, Any]) -> tuple[Path, Path]:
    if record.get("type") != "header" or record.get("version") != fo.JOURNAL_VERSION:
        raise JournalError("Journal header is missing or unsupported.")
    recorded = (_checked_path(record["source"]), _checked_path(record["output"]))
    if fo.validate_paths(*recorded) != recorded:
        raise JournalError("Journal folders now resolve somewhere else.")
    return recorded


def load_journal(path: Path) -> Journal:
    """Parse and fully validate a journal before anything is moved."""
    # A crash can leave a partial final line, which never ends in a newline.
    lines = _read_private_file(path).split("\n")[:-1]
    try:
        records = [json.loads(line) for line in lines]
        if not records or not all(isinstance(item, dict) for item in records):
            raise JournalError(f"Journal is empty or malformed: {str(path)!r}")
        source, output = _parse_header(records[0])

        moves: dict[Path, RecordedMove] = {}
        folders: list[Path] = []
        for record in records[1:]:
            kind = record.get("type")
            if kind == "move":
                move = _parse_move(record, source, output)
                moves[move.destination] = move
            elif kind == "folder":
                folders.append(_parse_folder(record, output))
            elif kind == "abort":
                moves.pop(_checked_path(record["destination"]), None)
            elif kind == "done":
                destination = _checked_path(record["destination"])
                moves[destination] = RecordedMove(
                    moves[destination].source,
                    destination,
                    _checked_int(record["size"]),
                    _checked_int(record["mtime_ns"]),
                )
            else:
                raise JournalError(f"Unknown journal record: {kind!r}")
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise JournalError(f"Journal is corrupt: {str(path)!r}") from error
    return Journal(path, source, output, tuple(moves.values()), tuple(folders))


def _is_same_file(first: Path, second: Path) -> bool:
    """Return True if two paths are hard links to one regular file."""
    try:
        one, two = first.lstat(), second.lstat()
    except OSError:
        return False
    return stat.S_ISREG(one.st_mode) and (one.st_dev, one.st_ino) == (
        two.st_dev,
        two.st_ino,
    )


def classify(move: RecordedMove) -> MoveState:
    has_original = os.path.lexists(move.source)
    has_moved = os.path.lexists(move.destination)
    if not has_moved:
        return MoveState.ALREADY_RESTORED if has_original else MoveState.MISSING
    if has_original:
        return (
            MoveState.INTERRUPTED
            if _is_same_file(move.source, move.destination)
            else MoveState.CONFLICT
        )
    try:
        fo.ensure_unchanged(move.entry)
    except (OSError, fo.FileChangedError):
        return MoveState.CHANGED
    return MoveState.RESTORABLE


def has_pending_moves(journal: Journal) -> bool:
    done = {MoveState.ALREADY_RESTORED, MoveState.MISSING}
    return any(classify(move) not in done for move in journal.moves)


def find_latest_journal(log_dir: Path) -> Journal | None:
    """Return the newest journal that still has files to restore."""
    paths = sorted(log_dir.glob(f"*{fo.JOURNAL_SUFFIX}"), reverse=True)
    for path in paths:
        journal = load_journal(path)
        if has_pending_moves(journal):
            return journal
    return None


def _restore(
    journal: Journal, move: RecordedMove, state: MoveState, apply: bool
) -> None:
    fo.reject_symlinks(journal.output, move.destination.parent)
    fo.reject_symlinks(journal.source, move.source.parent)
    if not apply:
        return
    if state is MoveState.INTERRUPTED:
        # Re-check immediately: only the extra link is removed, never the data.
        if not _is_same_file(move.source, move.destination):
            raise fo.FileChangedError(
                f"Paths no longer match: {str(move.destination)!r}"
            )
        move.destination.unlink()
        return
    move.source.parent.mkdir(parents=True, exist_ok=True)
    fo.move_without_overwrite(move.entry, move.source)


def _remove_created_folders(journal: Journal, apply: bool, summary: UndoSummary) -> None:
    deepest_first = sorted(
        set(journal.folders), key=lambda path: len(path.parts), reverse=True
    )
    for folder in deepest_first:
        if not apply:
            if folder.is_dir():
                logger.info("DRY-RUN REMOVE FOLDER IF EMPTY %r", str(folder))
            continue
        try:
            folder.rmdir()
        except FileNotFoundError:
            continue
        except OSError as error:
            if error.errno != errno.ENOTEMPTY:
                logger.warning("KEEP FOLDER %r: %s", str(folder), error)
            continue
        summary.folders_removed += 1
        logger.info("APPLY REMOVE EMPTY FOLDER %r", str(folder))


def undo(journal: Journal, apply: bool) -> UndoSummary:
    """Restore every eligible file, newest move first."""
    summary = UndoSummary()
    prefix = "APPLY" if apply else "DRY-RUN"

    for move in reversed(journal.moves):
        state = classify(move)
        if state is MoveState.ALREADY_RESTORED:
            summary.already_restored += 1
        elif state is MoveState.MISSING:
            summary.missing += 1
            logger.warning("MISSING %r", str(move.destination))
        elif state not in RESTORABLE_STATES:
            summary.conflicts += 1
            logger.error(
                "LEFT IN PLACE %r: %s (%r)",
                str(move.destination), state.value, str(move.source),
            )
        else:
            try:
                _restore(journal, move, state, apply)
            except (OSError, fo.OrganizerError) as error:
                summary.failed += 1
                logger.error("FAILED %r: %s", str(move.destination), error)
            else:
                summary.restored += 1
                logger.info(
                    "%s RESTORE %r -> %r",
                    prefix, str(move.destination), str(move.source),
                )

    _remove_created_folders(journal, apply, summary)
    return summary


def list_journals(log_dir: Path) -> None:
    paths = sorted(log_dir.glob(f"*{fo.JOURNAL_SUFFIX}"), reverse=True)
    if not paths:
        print("No organizer runs found.")
    for path in paths:
        try:
            journal = load_journal(path)
        except fo.OrganizerError as error:
            print(f"{path.name}: cannot be used ({error})")
            continue
        pending = sum(
            classify(move) in RESTORABLE_STATES for move in journal.moves
        )
        print(
            f"{path.name}: {pending} of {len(journal.moves)} file(s) "
            f"restorable from {str(journal.output)!r}"
        )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely reverse a file_organizer.py run."
    )
    parser.add_argument(
        "journal", type=Path, nargs="?",
        help="journal to undo (default: the latest run with files to restore)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="restore files (default is a preview)"
    )
    parser.add_argument(
        "--list", action="store_true", help="show runs that can be undone"
    )
    parser.add_argument(
        "--log-dir", type=Path, default=fo.DEFAULT_LOG_DIR,
        help="folder holding logs and journals (default: logs/ beside this script)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    log_dir = args.log_dir.expanduser().resolve()
    if args.list:
        list_journals(log_dir)
        return fo.EXIT_OK

    try:
        log_path = fo.configure_logging(log_dir)
    except OSError as error:
        print(f"ERROR: cannot create log file in {log_dir}: {error}", file=sys.stderr)
        return fo.EXIT_INVALID_USAGE

    try:
        journal = (
            load_journal(args.journal.expanduser())
            if args.journal
            else find_latest_journal(log_dir)
        )
        if journal is None:
            logger.info("Nothing to undo.")
            return fo.EXIT_OK

        logger.info("Undo mode: %s", "APPLY" if args.apply else "DRY-RUN (no changes)")
        logger.info("Journal: %r", str(journal.path))
        summary = undo(journal, args.apply)
        logger.info(
            "Summary: restored=%d already_restored=%d missing=%d "
            "left_in_place=%d failed=%d folders_removed=%d",
            summary.restored, summary.already_restored, summary.missing,
            summary.conflicts, summary.failed, summary.folders_removed,
        )
        if not args.apply:
            logger.info("Preview only. Re-run with --apply to restore files.")
        logger.info("Log saved to %r", str(log_path))
        has_problems = summary.conflicts or summary.failed
        return fo.EXIT_PARTIAL_FAILURE if has_problems else fo.EXIT_OK
    except fo.OrganizerError as error:
        logger.error("%s", error)
        return fo.EXIT_INVALID_USAGE
    except KeyboardInterrupt:
        logger.warning("Interrupted. Run undo again to finish restoring.")
        return fo.EXIT_INTERRUPTED
    finally:
        fo.close_logging()


if __name__ == "__main__":
    raise SystemExit(main())
