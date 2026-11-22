"""Behavior tests for file_organizer. Run with: python3 -m unittest -v"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import random
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import file_organizer as fo


class OrganizerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name).resolve() / "inbox"
        self.source.mkdir()
        self.output = self.source / fo.DEFAULT_OUTPUT_NAME
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def write(self, relative: str, content: str = "content") -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_organizer(self, **options: object) -> fo.Summary:
        options.setdefault("apply", True)
        config = fo.Config(self.source, self.output, **options)
        return fo.organize(config)

    def tree(self) -> set[str]:
        return {
            str(path.relative_to(self.source))
            for path in self.source.rglob("*")
            if not path.is_dir()
        }


class FolderNamingTests(unittest.TestCase):
    def test_extension_is_lowercased(self) -> None:
        self.assertEqual(fo.folder_for(Path("PHOTO.JPG")), "jpg")

    def test_file_without_extension_uses_reserved_folder(self) -> None:
        self.assertEqual(fo.folder_for(Path("Makefile")), fo.NO_EXTENSION_DIR)

    def test_extension_cannot_impersonate_duplicates_folder(self) -> None:
        name = f"report.{fo.DUPLICATES_DIR}"
        self.assertNotEqual(fo.folder_for(Path(name)), fo.DUPLICATES_DIR)


class SortingTests(OrganizerTestCase):
    def test_sorts_files_into_created_extension_folders(self) -> None:
        self.write("a.txt", "text")
        self.write("b.PDF", "pdf")
        summary = self.run_organizer()
        self.assertEqual(self.tree(), {"Organized/txt/a.txt", "Organized/pdf/b.PDF"})
        self.assertEqual(summary.moved, 2)

    def test_dry_run_changes_nothing(self) -> None:
        self.write("a.txt")
        before = self.tree()
        summary = self.run_organizer(apply=False)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.output.exists())
        self.assertEqual(summary.moved, 1)

    def test_name_collision_never_overwrites(self) -> None:
        self.write("Organized/txt/a.txt", "existing")
        self.write("a.txt", "different")
        self.run_organizer()
        folder = self.output / "txt"
        self.assertEqual((folder / "a.txt").read_text(), "existing")
        self.assertEqual((folder / "a (1).txt").read_text(), "different")

    def test_subfolders_ignored_unless_recursive(self) -> None:
        nested = self.write("sub/a.txt")
        self.run_organizer()
        self.assertTrue(nested.exists())

    def test_recursive_includes_subfolders(self) -> None:
        self.write("sub/a.txt")
        self.run_organizer(recursive=True)
        self.assertIn("Organized/txt/a.txt", self.tree())

    def test_second_run_is_a_no_op(self) -> None:
        self.write("a.txt")
        self.run_organizer(recursive=True)
        before = self.tree()
        summary = self.run_organizer(recursive=True)
        self.assertEqual(self.tree(), before)
        self.assertEqual(summary.moved, 0)

    def test_hidden_files_skipped_by_default(self) -> None:
        hidden = self.write(".secret.txt")
        self.run_organizer()
        self.assertTrue(hidden.exists())

    def test_cross_device_fallback_preserves_content(self) -> None:
        source = self.write("a.txt", "payload")
        with mock.patch("file_organizer.os.link", side_effect=OSError(18, "EXDEV")):
            self.run_organizer()
        self.assertFalse(source.exists())
        self.assertEqual((self.output / "txt" / "a.txt").read_text(), "payload")


class DuplicateTests(OrganizerTestCase):
    def test_duplicate_is_quarantined_not_deleted(self) -> None:
        self.write("a.txt", "same")
        self.write("b.txt", "same")
        summary = self.run_organizer()
        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(len(list((self.output / "txt").iterdir())), 1)
        quarantined = list((self.output / fo.DUPLICATES_DIR / "txt").iterdir())
        self.assertEqual([p.read_text() for p in quarantined], ["same"])

    def test_same_size_different_content_is_not_duplicate(self) -> None:
        self.write("a.txt", "aaaa")
        self.write("b.txt", "bbbb")
        self.assertEqual(self.run_organizer().duplicates, 0)

    def test_detects_duplicate_of_previously_organized_file(self) -> None:
        self.write("Organized/txt/original.txt", "same")
        self.write("new.txt", "same")
        summary = self.run_organizer()
        self.assertEqual(summary.duplicates, 1)
        self.assertTrue((self.output / "txt" / "original.txt").exists())


class SafetyTests(OrganizerTestCase):
    def test_symlink_is_left_alone(self) -> None:
        target = self.write("sub/target.txt")
        link = self.source / "link.txt"
        link.symlink_to(target)
        self.run_organizer()
        self.assertTrue(link.is_symlink())

    def test_fifo_is_skipped_without_hanging(self) -> None:
        fifo = self.source / "pipe.txt"
        os.mkfifo(fifo)
        self.run_organizer()
        self.assertTrue(fifo.exists())

    def test_file_changed_after_scan_is_not_moved(self) -> None:
        path = self.write("a.txt", "before")
        original = fo.move_without_overwrite

        def change_then_move(*args: object, **kwargs: object) -> None:
            path.write_text("after-change", encoding="utf-8")
            original(*args, **kwargs)

        with mock.patch("file_organizer.move_without_overwrite", change_then_move):
            summary = self.run_organizer()
        self.assertTrue(path.exists())
        self.assertEqual(summary.failed, 1)

    def test_keeps_only_copy_if_source_vanishes_during_move(self) -> None:
        source = self.write("a.txt", "only copy")
        real_unlink = Path.unlink

        def vanish_then_fail(path: Path, missing_ok: bool = False) -> None:
            if path == source:
                real_unlink(path)
                raise FileNotFoundError(path)
            real_unlink(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", vanish_then_fail):
            self.run_organizer()
        self.assertEqual((self.output / "txt" / "a.txt").read_text(), "only copy")

    def test_excluded_path_is_not_moved(self) -> None:
        script = self.write("file_organizer.py")
        self.run_organizer(excluded=frozenset({script}))
        self.assertTrue(script.exists())

    def test_rejects_filesystem_root(self) -> None:
        with self.assertRaises(fo.UnsafePathError):
            fo.validate_paths(Path("/"), self.output)

    def test_rejects_home_directory(self) -> None:
        with self.assertRaises(fo.UnsafePathError):
            fo.validate_paths(Path.home(), self.output)

    def test_rejects_system_subdirectory(self) -> None:
        with self.assertRaises(fo.UnsafePathError):
            fo.validate_paths(Path("/etc/ssl"), self.output)

    def test_rejects_output_equal_to_source(self) -> None:
        with self.assertRaises(fo.UnsafePathError):
            fo.validate_paths(self.source, self.source)

    def test_rejects_source_inside_output(self) -> None:
        with self.assertRaises(fo.UnsafePathError):
            fo.validate_paths(self.source, self.source.parent)


class HardeningTests(OrganizerTestCase):
    """Regression tests for issues found during the security audit."""

    def run_script(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess:
        command = [sys.executable, "-B", str(Path(fo.__file__)), *args]
        return subprocess.run(command, check=False, text=True, **kwargs)

    def test_symlinked_output_folder_cannot_redirect_moves(self) -> None:
        outside = self.source.parent / "outside"
        outside.mkdir()
        self.output.mkdir()
        (self.output / "txt").symlink_to(outside)
        path = self.write("a.txt")
        summary = self.run_organizer()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(path.exists())
        self.assertEqual(summary.failed, 1)

    def test_rejects_source_inside_log_folder(self) -> None:
        self.write("a.txt")
        logging.disable(logging.NOTSET)
        with contextlib.redirect_stdout(io.StringIO()):
            code = fo.main([str(self.source), "--apply", "--log-dir", str(self.source)])
        self.assertEqual(code, 2)
        self.assertTrue((self.source / "a.txt").exists())

    def test_rejects_source_that_is_a_repository(self) -> None:
        (self.source / ".git").mkdir()
        with self.assertRaises(fo.UnsafePathError):
            fo.validate_paths(self.source, self.output)

    def test_nested_repository_is_left_intact(self) -> None:
        tracked = self.write("project/main.py")
        config = self.write("project/.git/config")
        self.run_organizer(recursive=True, include_hidden=True)
        self.assertTrue(tracked.exists())
        self.assertTrue(config.exists())

    def test_rejects_hidden_folder_inside_home(self) -> None:
        hidden = Path.home() / ".ssh"
        with self.assertRaises(fo.UnsafePathError):
            fo.validate_paths(hidden, hidden / "Organized")

    def test_copy_is_private_until_permissions_are_restored(self) -> None:
        path = self.write("secret.txt", "secret")
        path.chmod(0o600)
        modes: list[int] = []
        real_copy = fo.shutil.copyfileobj

        def spy(reader: object, writer: io.BufferedWriter) -> None:
            modes.append(stat.S_IMODE(os.fstat(writer.fileno()).st_mode))
            real_copy(reader, writer)

        with (
            mock.patch("file_organizer.os.link", side_effect=OSError(18, "EXDEV")),
            mock.patch("file_organizer.shutil.copyfileobj", spy),
        ):
            self.run_organizer()
        self.assertEqual(modes, [0o600])
        copied = self.output / "txt" / "secret.txt"
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)

    def test_copy_drops_setuid_bit(self) -> None:
        path = self.write("tool.bin")
        path.chmod(0o4755)
        with mock.patch("file_organizer.os.link", side_effect=OSError(18, "EXDEV")):
            self.run_organizer()
        mode = stat.S_IMODE((self.output / "bin" / "tool.bin").stat().st_mode)
        self.assertEqual(mode, 0o755)

    def test_error_message_cannot_forge_log_lines(self) -> None:
        path = self.write("a\nFORGED.txt", "before")
        info = path.stat()
        entry = fo.FileEntry(path, info.st_size + 1, info.st_mtime_ns)
        with self.assertRaises(fo.FileChangedError) as caught:
            fo.ensure_unchanged(entry)
        self.assertNotIn("\n", str(caught.exception))

    def test_long_name_collision_stays_within_filesystem_limit(self) -> None:
        name = "n" * 251 + ".txt"
        self.write(f"Organized/txt/{name}", "old")
        path = self.write(name, "new!")
        summary = self.run_organizer()
        self.assertEqual(summary.failed, 0)
        self.assertFalse(path.exists())
        self.assertEqual(len(list((self.output / "txt").iterdir())), 2)

    def test_long_multibyte_name_is_not_split_mid_character(self) -> None:
        name = "é" * 125 + ".txt"
        candidate = fo.unique_destination(self.source, name, {self.source / name})
        self.assertLessEqual(len(candidate.name.encode("utf-8")), 255)
        candidate.name.encode("utf-8")

    def test_ascii_console_does_not_break_logging(self) -> None:
        self.write("résumé.txt")
        logs = self.source.parent / "logs"
        result = self.run_script(
            str(self.source), "--log-dir", str(logs),
            env={**os.environ, "PYTHONIOENCODING": "ascii"},
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Logging error", result.stderr)

    def test_closed_output_pipe_does_not_spam_errors(self) -> None:
        self.write("a.txt")
        logs = self.source.parent / "logs"
        read_end, write_end = os.pipe()
        os.close(read_end)
        self.addCleanup(os.close, write_end)
        result = self.run_script(
            str(self.source), "--log-dir", str(logs),
            stdout=write_end, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_log_file_is_private(self) -> None:
        self.write("a.txt")
        logs = self.source.parent / "logs"
        logs.mkdir(mode=0o755)
        logging.disable(logging.NOTSET)
        with contextlib.redirect_stdout(io.StringIO()):
            fo.main([str(self.source), "--log-dir", str(logs)])
        log_file = next(logs.glob("*.log"))
        self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)

    def test_all_content_survives_randomized_run(self) -> None:
        rng = random.Random(7)
        for _ in range(300):
            extension = rng.choice([".txt", ".PDF", ".jpg", "", ".tar.gz"])
            name = f"d{rng.randrange(6)}/file{rng.randrange(40)}{extension}"
            self.write(name, str(rng.randrange(80)) * rng.randrange(1, 20))

        def contents() -> list[bytes]:
            files = (p for p in self.source.rglob("*") if p.is_file())
            return sorted(p.read_bytes() for p in files)

        before = contents()
        summary = self.run_organizer(recursive=True)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(contents(), before)


class CliTests(OrganizerTestCase):
    def test_cli_defaults_to_preview_and_writes_log(self) -> None:
        path = self.write("a.txt")
        log_dir = self.source.parent / "logs"
        logging.disable(logging.NOTSET)
        with contextlib.redirect_stdout(io.StringIO()):
            code = fo.main([str(self.source), "--log-dir", str(log_dir)])
        self.assertEqual(code, 0)
        self.assertTrue(path.exists())
        logs = list(log_dir.glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("a.txt", logs[0].read_text(encoding="utf-8"))

    def test_cli_returns_2_for_unsafe_source(self) -> None:
        log_dir = self.source.parent / "logs"
        self.assertEqual(fo.main(["/", "--log-dir", str(log_dir)]), 2)


if __name__ == "__main__":
    unittest.main()
