#!/usr/bin/env python3
"""Run the organizer on sample files and screenshot its real output.

Everything happens in a temporary folder. Requires Chromium for the PNGs.
Usage: python3 docs/generate_demo.py
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DOCS = Path(__file__).resolve().parent
SCRIPT = DOCS.parent / "file_organizer.py"
WIDTH = 1640
LINE_HEIGHT = 22
SAMPLES = {
    "invoice.pdf": "invoice-2026",
    "invoice (copy).pdf": "invoice-2026",
    "notes.TXT": "meeting notes",
    "vacation.jpg": "image-bytes",
    "archive/vacation-2.jpg": "image-bytes",
    "backup.tar.gz": "archive-bytes",
    "Makefile": "all:",
    ".env": "PLACEHOLDER=1",
}
COLORS = (
    ("$ ", "#7ee787"),
    ("WARNING", "#e3b341"),
    ("ERROR", "#ff7b72"),
    ("INFO: Summary", "#79c0ff"),
)


def build_sandbox(root: Path) -> None:
    downloads = root / "Downloads"
    for offset, (name, content) in enumerate(SAMPLES.items()):
        path = downloads / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        timestamp = 1_700_000_000 + offset
        os.utime(path, (timestamp, timestamp))
    (downloads / "shortcut.txt").symlink_to("notes.TXT")
    (root / "my-project" / ".git").mkdir(parents=True)


def run(root: Path, *args: str) -> list[str]:
    shown = " ".join(args)
    command = [sys.executable, "-B", str(SCRIPT), *args, "--log-dir", "logs"]
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, check=False
    )
    lines = (result.stdout + result.stderr).splitlines()
    return [f"$ python3 file_organizer.py {shown} --log-dir logs", *lines, ""]


def tree(root: Path) -> list[str]:
    lines = ["$ tree -a Downloads", "Downloads"]

    def walk(folder: Path, prefix: str) -> None:
        children = sorted(folder.iterdir(), key=lambda item: item.name.lower())
        for index, child in enumerate(children):
            last = index == len(children) - 1
            label = f"{child.name} -> {os.readlink(child)}" if child.is_symlink() else child.name
            lines.append(f"{prefix}{'└── ' if last else '├── '}{label}")
            if child.is_dir() and not child.is_symlink():
                walk(child, prefix + ("    " if last else "│   "))

    walk(root / "Downloads", "")
    return lines


def to_html(title: str, lines: list[str]) -> str:
    rows = []
    for line in lines:
        color = next((c for key, c in COLORS if line.startswith(key)), "#c9d1d9")
        rows.append(f'<div style="color:{color}">{html.escape(line) or "&nbsp;"}</div>')
    return f"""<!doctype html><meta charset="utf-8"><style>
    html,body{{margin:0;background:#0d1117}}
    .bar{{background:#161b22;color:#8b949e;padding:10px 18px;font:13px sans-serif}}
    .body{{padding:16px 18px;font:13px/{LINE_HEIGHT}px "DejaVu Sans Mono",monospace;white-space:pre}}
    </style><div class="bar">{html.escape(title)}</div><div class="body">{''.join(rows)}</div>"""


def screenshot(browser: str, work: Path, name: str, title: str, lines: list[str]) -> None:
    page = work / f"{name}.html"
    page.write_text(to_html(title, lines), encoding="utf-8")
    height = len(lines) * LINE_HEIGHT + 76
    subprocess.run(
        [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--user-data-dir={work / 'profile'}", "--force-device-scale-factor=2",
            f"--window-size={WIDTH},{height}",
            f"--screenshot={DOCS / (name + '.png')}", page.as_uri(),
        ],
        check=True, capture_output=True,
    )


def main() -> int:
    browser = shutil.which("chromium") or shutil.which("google-chrome")
    if browser is None:
        print("Chromium is required to generate screenshots.", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="demo-", dir="/tmp") as temp:
        root = Path(temp)
        build_sandbox(root)
        scenes = {
            "demo-preview": ("1. Preview: nothing is changed", run(root, "Downloads", "-r")),
            "demo-apply": (
                "2. Apply: files sorted, duplicates quarantined",
                run(root, "Downloads", "-r", "--apply") + tree(root),
            ),
            "demo-safety": (
                "3. Safety: dangerous targets are refused",
                run(root, "/") + run(root, "/etc") + run(root, "my-project"),
            ),
        }
        with tempfile.TemporaryDirectory(prefix="render-") as render:
            for name, (title, lines) in scenes.items():
                screenshot(browser, Path(render), name, title, lines)
                print(f"Wrote docs/{name}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
