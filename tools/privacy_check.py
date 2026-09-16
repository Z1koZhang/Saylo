"""Fail when a release tree contains common credentials or private runtime artifacts."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", ".venv", "__pycache__"}
FORBIDDEN_NAMES = {
    "secrets.json", "secrets.enc", "journal.jsonl", "memories.jsonl",
    "reminders.jsonl", "runtime_status.json", "runtime_control.json",
    "saylo.pid", "supervisor.pid", "widget_v3.pid",
    "records.key.json", "records.key.dpapi",
}
FORBIDDEN_SUFFIXES = {".log", ".key", ".npy", ".npz", ".dmp", ".senc", ".slog", ".state", ".bin"}
TEXT_SUFFIXES = {".py", ".pyw", ".ps1", ".vbs", ".md", ".txt", ".json", ".toml", ".yml", ".yaml"}
PATTERNS = {
    "API key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "WeChat ID": re.compile(r"\bwxid_[A-Za-z0-9_-]+\b", re.I),
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I),
    "non-empty whitelist": re.compile(r"REPLY_WHITELIST\s*=\s*\[\s*['\"][^'\"]+", re.I),
    "non-empty self ID": re.compile(r"SELF_WXID\s*=\s*['\"][^'\"]+", re.I),
}


def main() -> int:
    problems: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        rel = path.relative_to(ROOT)
        if path.name.lower() in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"forbidden file: {rel}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"non-UTF-8 text file: {rel}")
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                problems.append(f"{label}: {rel}")
    if problems:
        print("Privacy check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Privacy check passed: no known credentials or private runtime artifacts found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
