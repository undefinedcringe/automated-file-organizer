"""Behavior tests for undo_organizer. Run with: python3 -m unittest -v"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import file_organizer as fo
import undo_organizer as uo


class UndoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "inbox"
        self.source.mkdir()
        self.output = self.source / fo.DEFAULT_OUTPUT_NAME
        self.logs = self.root / "logs"

    def write(self, relative: str, content: str = "content") -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def organize(self, *args: str) -> int:
        command = [str(self.source), "--apply", "--log-dir", str(self.logs), *args]
        with contextlib.redirect_stdout(io.StringIO()):
            return fo.main(command)

    def undo(self, *args: str, apply: bool = True) -> int:
        command = ["--log-dir", str(self.logs), *args]
        if apply:
            command.append("--apply")
        with contextlib.redirect_stdout(io.StringIO()):
            return uo.main(command)

    def journals(self) -> list[Path]:
        return sorted(self.logs.glob("*.journal.jsonl"))

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.source)): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }


class RestoreTests(UndoTestCase):
    def test_restores_sorted_files_and_duplicates(self) -> None:
        self.write("a.txt", "same")
        self.write("b.txt", "same")
        self.write("sub/c.pdf", "pdf")
        before = self.snapshot()
        self.organize("-r")
        self.assertNotEqual(self.snapshot(), before)
        self.assertEqual(self.undo(), 0)
        self.assertEqual(self.snapshot(), before)

    def test_removes_only_folders_created_by_the_run(self) -> None:
        self.write("a.txt")
        self.organize()
        self.undo()
        self.assertFalse(self.output.exists())

    def test_keeps_empty_folder_that_existed_before_the_run(self) -> None:
        (self.output / "txt").mkdir(parents=True)
        self.write("a.txt")
        self.organize()
        self.undo()
        self.assertTrue((self.output / "txt").is_dir())

    def test_keeps_created_folder_holding_a_new_user_file(self) -> None:
        self.write("a.txt")
        self.organize()
        keep = self.output / "txt" / "mine.txt"
        keep.write_text("mine", encoding="utf-8")
        self.undo()
        self.assertEqual(keep.read_text(encoding="utf-8"), "mine")

    def test_preserves_timestamp_and_permissions(self) -> None:
        path = self.write("a.txt")
        path.chmod(0o640)
        os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        self.organize()
        self.undo()
        info = path.stat()
        self.assertEqual(info.st_mtime_ns, 1_600_000_000_000_000_000)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o640)

    def test_recreates_deleted_source_subfolder(self) -> None:
        path = self.write("sub/a.txt")
        self.organize("-r")
        path.parent.rmdir()
        self.assertEqual(self.undo(), 0)
        self.assertTrue(path.exists())

    def test_restores_non_utf8_file_name(self) -> None:
        raw = os.fsencode(self.source) + b"/bad\xff.txt"
        with open(raw, "wb") as handle:
            handle.write(b"x")
        self.organize()
        self.assertFalse(os.path.exists(raw))
        self.assertEqual(self.undo(), 0)
        self.assertTrue(os.path.exists(raw))

    def test_restores_across_filesystems(self) -> None:
        path = self.write("a.txt", "payload")
        with mock.patch("file_organizer.os.link", side_effect=OSError(18, "EXDEV")):
            self.organize()
            self.assertEqual(self.undo(), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "payload")

    def test_undo_is_safe_to_repeat(self) -> None:
        self.write("a.txt")
        before = self.snapshot()
        self.organize()
        journal = str(self.journals()[-1])
        self.assertEqual(self.undo(journal), 0)
        self.assertEqual(self.undo(journal), 0)
        self.assertEqual(self.snapshot(), before)

    def test_repeated_undo_steps_back_through_runs(self) -> None:
        first = self.write("first.txt", "1")
        self.organize()
        second = self.write("second.txt", "2")
        self.organize()
        self.undo()
        self.assertTrue(second.exists())
        self.assertFalse(first.exists())
        self.undo()
        self.assertTrue(first.exists())

    def test_nothing_to_undo_succeeds(self) -> None:
        self.assertEqual(self.undo(), 0)

    def test_preview_changes_nothing(self) -> None:
        self.write("a.txt")
        self.organize()
        after_organize = self.snapshot()
        self.assertEqual(self.undo(apply=False), 0)
        self.assertEqual(self.snapshot(), after_organize)

    def test_organizer_preview_writes_no_journal(self) -> None:
        self.write("a.txt")
        with contextlib.redirect_stdout(io.StringIO()):
            fo.main([str(self.source), "--log-dir", str(self.logs)])
        self.assertEqual(self.journals(), [])


class ConflictTests(UndoTestCase):
    def test_never_overwrites_new_file_at_original_path(self) -> None:
        path = self.write("a.txt", "old")
        self.organize()
        path.write_text("new", encoding="utf-8")
        self.assertEqual(self.undo(), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "new")
        self.assertEqual((self.output / "txt" / "a.txt").read_text(), "old")

    def test_file_edited_after_organizing_is_left_in_place(self) -> None:
        path = self.write("a.txt", "old")
        self.organize()
        moved = self.output / "txt" / "a.txt"
        moved.write_text("edited content", encoding="utf-8")
        self.assertEqual(self.undo(), 1)
        self.assertFalse(path.exists())
        self.assertEqual(moved.read_text(encoding="utf-8"), "edited content")

    def test_deleted_file_does_not_block_other_restores(self) -> None:
        kept = self.write("a.txt", "a")
        self.write("b.pdf", "b")
        self.organize()
        (self.output / "pdf" / "b.pdf").unlink()
        self.assertEqual(self.undo(), 0)
        self.assertTrue(kept.exists())

    def test_symlinked_restore_folder_cannot_redirect_file(self) -> None:
        path = self.write("sub/a.txt")
        self.organize("-r")
        outside = self.root / "outside"
        outside.mkdir()
        path.parent.rmdir()
        path.parent.symlink_to(outside)
        self.assertEqual(self.undo(), 1)
        self.assertEqual(list(outside.iterdir()), [])


class JournalSafetyTests(UndoTestCase):
    def rewrite_first_move(self, field: str, value: str) -> Path:
        journal = self.journals()[-1]
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        next(r for r in records if r["type"] == "move")[field] = value
        journal.write_text("".join(json.dumps(r) + "\n" for r in records))
        return journal

    def test_rejects_restore_path_outside_source(self) -> None:
        self.write("a.txt")
        self.organize()
        escaped = str(self.root / "escaped.txt")
        self.rewrite_first_move("source", escaped)
        self.assertEqual(self.undo(), 2)
        self.assertFalse(Path(escaped).exists())
        self.assertTrue((self.output / "txt" / "a.txt").exists())

    def test_rejects_moving_file_from_outside_output(self) -> None:
        victim = self.root / "victim.txt"
        victim.write_text("private", encoding="utf-8")
        self.write("a.txt")
        self.organize()
        self.rewrite_first_move("destination", str(victim))
        self.assertEqual(self.undo(), 2)
        self.assertTrue(victim.exists())

    def test_rejects_parent_traversal_in_path(self) -> None:
        self.write("a.txt")
        self.organize()
        self.rewrite_first_move("source", f"{self.source}/../escaped.txt")
        self.assertEqual(self.undo(), 2)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_rejects_group_writable_journal(self) -> None:
        self.write("a.txt")
        self.organize()
        self.journals()[-1].chmod(0o660)
        self.assertEqual(self.undo(), 2)

    def test_rejects_symlinked_journal(self) -> None:
        self.write("a.txt")
        self.organize()
        link = self.root / "link.journal.jsonl"
        link.symlink_to(self.journals()[-1])
        self.assertEqual(self.undo(str(link)), 2)

    def test_rejects_corrupt_journal_without_moving_anything(self) -> None:
        self.write("a.txt")
        self.organize()
        journal = self.journals()[-1]
        lines = journal.read_text().splitlines()
        lines.insert(1, "{not json")
        journal.write_text("\n".join(lines) + "\n")
        self.assertEqual(self.undo(), 2)
        self.assertTrue((self.output / "txt" / "a.txt").exists())

    def test_tolerates_partial_last_line_from_a_crash(self) -> None:
        path = self.write("a.txt")
        self.organize()
        with self.journals()[-1].open("a", encoding="ascii") as stream:
            stream.write('{"type": "mo')
        self.assertEqual(self.undo(), 0)
        self.assertTrue(path.exists())

    def test_journal_is_private(self) -> None:
        self.write("a.txt")
        self.organize()
        mode = stat.S_IMODE(self.journals()[-1].stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_failed_move_is_not_restored(self) -> None:
        path = self.write("a.txt")
        with mock.patch(
            "file_organizer.move_without_overwrite", side_effect=PermissionError
        ):
            self.organize()
        self.assertEqual(self.undo(), 0)
        self.assertTrue(path.exists())

    def test_interrupted_move_can_still_be_restored(self) -> None:
        path = self.write("a.txt")
        real_move = fo.move_without_overwrite

        def move_then_interrupt(entry: fo.FileEntry, destination: Path) -> None:
            real_move(entry, destination)
            raise KeyboardInterrupt

        with mock.patch("file_organizer.move_without_overwrite", move_then_interrupt):
            self.assertEqual(self.organize(), fo.EXIT_INTERRUPTED)
        self.assertFalse(path.exists())
        self.assertEqual(self.undo(), 0)
        self.assertTrue(path.exists())


class CrashRecoveryTests(UndoTestCase):
    def test_crash_between_link_and_unlink_is_recovered(self) -> None:
        path = self.write("a.txt", "payload")

        def link_then_crash(entry: fo.FileEntry, destination: Path) -> None:
            os.link(entry.path, destination)
            raise KeyboardInterrupt

        with mock.patch("file_organizer.move_without_overwrite", link_then_crash):
            self.organize()
        moved = self.output / "txt" / "a.txt"
        self.assertTrue(moved.exists())
        self.assertEqual(self.undo(), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "payload")
        self.assertFalse(moved.exists())

    def test_separate_file_at_both_paths_is_still_a_conflict(self) -> None:
        path = self.write("a.txt", "same")
        self.organize()
        path.write_text("same", encoding="utf-8")
        self.assertEqual(self.undo(), 1)
        self.assertTrue((self.output / "txt" / "a.txt").exists())


class ListTests(UndoTestCase):
    def test_list_shows_runs_without_changing_files(self) -> None:
        self.write("a.txt")
        self.organize()
        after_organize = self.snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = uo.main(["--list", "--log-dir", str(self.logs)])
        self.assertEqual(code, 0)
        self.assertIn(self.journals()[-1].name, output.getvalue())
        self.assertEqual(self.snapshot(), after_organize)


if __name__ == "__main__":
    unittest.main()
