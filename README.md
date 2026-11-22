# Automated File Organizer

A safe command-line tool that sorts a messy folder by file extension, finds exact duplicates, and logs every action. It uses only the Python standard library.

It previews by default, and it never deletes or overwrites your files.

## What it does

- **Sorts files by extension**, for example `report.PDF` to `Organized/pdf/report.PDF`.
- **Creates folders automatically**, only when a file needs them.
- **Detects duplicates** by comparing SHA-256 hashes, including against files organized in earlier runs. The oldest copy is kept in place and the others move to `Organized/_duplicates/` for review.
- **Logs everything** to the console and to a private, timestamped log file.
- **Undoes runs** by restoring files to their original paths and removing the empty folders the run created.

## Demo

These screenshots are real program output from `docs/generate_demo.py`, which runs on sample files in a temporary folder.

**1. Preview what would happen. Nothing changes.**

![Preview run](docs/demo-preview.png)

**2. Apply the changes.**

![Apply run and resulting folder tree](docs/demo-apply.png)

**3. Dangerous targets are refused.**

![Refused targets](docs/demo-safety.png)

## Quick start

You need Python 3.11 or newer on Linux. There is nothing to install.

Preview first:

```bash
python3 file_organizer.py ~/Downloads
```

Move the files once the preview looks right:

```bash
python3 file_organizer.py ~/Downloads --apply
```

| Option | Purpose |
| --- | --- |
| `--apply` | Move files. Without it, the run is only a preview. |
| `-r`, `--recursive` | Include subfolders. |
| `-o`, `--output DIR` | Choose the destination. The default is `SOURCE/Organized`. |
| `--include-hidden` | Include dotfiles and dot folders. |
| `--log-dir DIR` | Choose where logs go. The default is `logs/` beside the script. |

Exit codes are `0` for success, `1` if some files failed, `2` for invalid or unsafe paths, and `130` if interrupted.

## Undo a run

Every `--apply` run saves a private journal in `logs/`. Preview undoing the latest run:

```bash
python3 undo_organizer.py
```

Restore the files:

```bash
python3 undo_organizer.py --apply
```

Run it again to step back through earlier runs. To see every run, or undo a specific one:

```bash
python3 undo_organizer.py --list
```

```bash
python3 undo_organizer.py logs/organizer_YYYYMMDD_HHMMSS_ffffff.journal.jsonl --apply
```

Undo never overwrites a file. If a new file now occupies the original path, or a file was edited after organizing, it stays where it is and the run reports it with exit code `1`. Keep the `logs/` folder if you want to be able to undo.

## Safety features

- Moves cannot overwrite: the destination is claimed atomically, and name clashes become `name (1).ext`.
- Moves between drives are copied, hash-verified, and only then removed from the source.
- Files that change after scanning are skipped.
- Symlinks, pipes, and devices are never followed or moved.
- System folders, home directories, hidden config folders such as `~/.ssh`, and Git, Mercurial, or Subversion repositories are protected.
- File names are escaped in logs, so a crafted name cannot forge log entries or inject terminal codes.
- Log files and undo journals are readable only by your user.
- Each move is journaled before it happens, so runs can be undone even after a crash.
- Undo rejects journals that are corrupt, writable by others, symlinked, or point outside the original folders.

## Technologies used

| Technology | Used for |
| --- | --- |
| Python 3.13 (3.11+ supported) | Application language |
| Standard library: `pathlib`, `os`, `shutil`, `hashlib`, `logging`, `argparse`, `dataclasses` | File handling, hashing, logging, and the CLI |
| `unittest` | Automated test suite |
| Coverage.py | Test coverage |
| Ruff | Linting |
| mypy (strict mode) | Static type checking |
| Bandit | Security scanning |
| Chromium (headless) | Rendering demo screenshots |
| Git | Version control |

## Testing

```bash
python3 -m unittest -v
```

The suite has 70 tests and 88% branch coverage. It covers sorting, duplicates, undo, the command line, hostile file names, tampered journals, simulated race conditions, and a randomized check that all file contents survive a run. Undo was also verified on 1,200 generated files across real filesystems and after `kill -9` crashes at several points in a run.

## Problems encountered and solved

| Problem | Risk | Solution |
| --- | --- | --- |
| Rollback could delete the destination after the source vanished mid-move | Loss of the only copy | Roll back only while the source still exists |
| A symlink inside the output folder could redirect moves | Files written outside the target | Refuse symlinks in the destination path |
| Temporary copies used default permissions | Private files briefly readable by other users | Create copies as `0600`, then restore the original mode |
| Copies could keep setuid or setgid bits | Privilege escalation when run as root | Strip those bits from copies |
| Newlines in file names reached log messages | Forged log entries | Escape paths in every error message |
| Log files were readable by other users | Exposed file names and folder structure | Create logs as `0600` |
| Recursive runs could reorganize Git repositories | Broken projects | Refuse repositories and skip nested ones |
| Folders such as `~/.ssh` were accepted | Broken SSH keys and app settings | Refuse hidden folders in home |
| Using the source as the log folder could move the active log | Lost audit trail | Reject that configuration |
| Name clashes on 255-byte file names failed | Files left unsorted | Trim whole characters to fit the limit |
| Non-UTF-8 consoles caused logging errors | Noisy, unreliable output | Escape unsupported characters |
| Piping into `head` produced thousands of error lines | Unusable output | Stop console output quietly and keep the file log |
| pytest was not installed | Tests could not run | Use the built-in `unittest` and stay dependency-free |
| A crash could happen after a move but before it was recorded | Moved file unknown to undo | Write and sync each journal entry before moving |
| A crash between linking and unlinking left one file at two paths | False conflict during undo | Detect the shared inode and remove only the extra link |
| A journal is a list of file moves, so it could be tampered with | Undo used to move arbitrary files | Validate ownership, permissions, and every path before moving anything |

## Known limitations

- Tested on Linux only. macOS may work, and Windows is not supported.
- Emptied subfolders are left in place, because the tool never deletes anything.
- Files are grouped by their last extension, so `backup.tar.gz` goes to `gz/`.
- A copy moved between drives by a different user may not keep the original owner.
- Syncing the journal for crash safety slows very large runs, to roughly 70 files per second on the test machine.
- If a crash interrupts a copy between drives, a partial file can remain in the output folder. The original stays safe, and undo reports the partial file instead of removing it.
