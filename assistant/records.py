# -*- coding: utf-8 -*-
"""初始化、迁移和查看 Saylo 的加密私密记录。

常用命令：
  python assistant/records.py setup
  python assistant/records.py view
  python assistant/records.py view --stream journal --tail 50
  python assistant/records.py verify

口令通过 getpass 无回显读取。view 只向当前终端输出，不创建明文临时文件。
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secure_records as S

HERE = Path(__file__).resolve().parent
STORE = HERE / "store"
SECURE = S.SECURE_DIR

RECORD_FILES = {
    "journal": SECURE / "journal.senc",
    "memories": SECURE / "memories.senc",
    "reminders": SECURE / "reminders.senc",
    "memory_meta": SECURE / "memory_meta.senc",
    "style_meta": SECURE / "style_meta.senc",
    "agent_log": SECURE / "agent_log.slog",
    "supervisor_log": SECURE / "supervisor_log.slog",
    "widget_log": SECURE / "widget_log.slog",
    "crash_log": SECURE / "crash_log.slog",
    "reasoning": SECURE / "reasoning.senc",
}

STATE_FILES = {
    "checkins": SECURE / "checkins.state",
    "emotion_state": SECURE / "emotion_state.state",
    "active_context": SECURE / "active_context.state",
    "relationship_state": SECURE / "relationship_state.state",
    "memory_terms": SECURE / "memory_terms.state",
    "style_terms": SECURE / "style_terms.state",
}

BINARY_FILES = {
    "memory_bm25": SECURE / "memory_bm25.bin",
    "style_bm25": SECURE / "style_bm25.bin",
    "memory_vecs": SECURE / "memory_vecs.bin",
    "style_vecs": SECURE / "style_vecs.bin",
}

PLAIN_RECORDS = {
    "journal": (STORE / "journal.jsonl", "jsonl"),
    "memories": (STORE / "memories.jsonl", "jsonl"),
    "reminders": (STORE / "reminders.jsonl", "jsonl"),
    "memory_meta": (STORE / "memory.meta.jsonl", "jsonl"),
    "style_meta": (STORE / "style.meta.jsonl", "jsonl"),
    "agent_log": (HERE / "agent.log", "text"),
    "supervisor_log": (HERE / "supervisor.log", "text"),
    "widget_log": (HERE / "widget_v3.log", "text"),
    "crash_log": (HERE / "agent_crash.log", "text"),
}

PLAIN_STATES = {
    "checkins": STORE / "checkins.json",
    "emotion_state": STORE / "emotion_state.json",
    "active_context": STORE / "active_context.json",
    "relationship_state": STORE / "relationship_state.json",
    "memory_terms": STORE / "memory.terms.json",
    "style_terms": STORE / "style.terms.json",
}

PLAIN_BINARIES = {
    "memory_bm25": STORE / "memory.bm25.npz",
    "style_bm25": STORE / "style.bm25.npz",
    "memory_vecs": STORE / "memory.vecs.npy",
    "style_vecs": STORE / "style.vecs.npy",
}


def _passphrase(confirm: bool = False) -> str:
    first = getpass.getpass("请输入记录解锁口令（输入不回显）: ")
    if confirm:
        second = getpass.getpass("请再次输入同一口令: ")
        if first != second:
            raise S.SecureRecordsError("两次输入的口令不一致")
    return first


def _read_plain_records(path: Path, kind: str) -> list[Any]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        if kind == "jsonl":
            rows.append(json.loads(line))
        else:
            rows.append({"message": line})
    return rows


def migrate(key: bytes) -> list[str]:
    """逐文件加密并往返比对；只有该文件验证成功后才删除它的明文版本。"""
    migrated = []
    SECURE.mkdir(parents=True, exist_ok=True)
    for stream, (source, kind) in PLAIN_RECORDS.items():
        if not source.exists():
            continue
        target = RECORD_FILES[stream]
        if target.exists():
            raise S.SecureRecordsError(f"目标已存在，拒绝覆盖：{target}")
        rows = _read_plain_records(source, kind)
        S.write_records(target, stream, rows, key)
        if S.read_records(target, stream, key) != rows:
            target.unlink(missing_ok=True)
            raise S.SecureRecordsError(f"往返校验失败：{source}")
        source.unlink()
        migrated.append(f"{source.name} -> {target.name} ({len(rows)} 条)")

    for stream, source in PLAIN_STATES.items():
        if not source.exists():
            continue
        target = STATE_FILES[stream]
        if target.exists():
            raise S.SecureRecordsError(f"目标已存在，拒绝覆盖：{target}")
        value = json.loads(source.read_text(encoding="utf-8"))
        S.write_json(target, stream, value, key)
        if S.read_json(target, stream, key=key) != value:
            target.unlink(missing_ok=True)
            raise S.SecureRecordsError(f"往返校验失败：{source}")
        source.unlink()
        migrated.append(f"{source.name} -> {target.name}")

    for stream, source in PLAIN_BINARIES.items():
        if not source.exists():
            continue
        target = BINARY_FILES[stream]
        if target.exists():
            raise S.SecureRecordsError(f"目标已存在，拒绝覆盖：{target}")
        raw = source.read_bytes()
        S.write_secure_bytes(target, stream, raw, key)
        if S.read_secure_bytes(target, stream, key) != raw:
            target.unlink(missing_ok=True)
            raise S.SecureRecordsError(f"往返校验失败：{source}")
        source.unlink()
        migrated.append(f"{source.name} -> {target.name} ({len(raw)} 字节)")
    return migrated


def verify(key: bytes) -> list[str]:
    checked = []
    for stream, path in RECORD_FILES.items():
        if path.exists():
            count = len(S.read_records(path, stream, key))
            checked.append(f"{stream}: {count} 条，完整性正常")
    for stream, path in STATE_FILES.items():
        if path.exists():
            S.read_json(path, stream, key=key)
            checked.append(f"{stream}: 状态完整性正常")
    for stream, path in BINARY_FILES.items():
        if path.exists():
            size = len(S.read_secure_bytes(path, stream, key))
            checked.append(f"{stream}: {size} 字节，完整性正常")
    return checked


def _display(item: Any) -> str:
    if isinstance(item, dict):
        if "who" in item and "text" in item:
            return f"[{item.get('time', '')}] {item.get('who')}: {item.get('text')}"
        if "message" in item:
            return str(item["message"])
        return json.dumps(item, ensure_ascii=False, indent=2)
    return str(item)


def view(key: bytes, stream: str | None, tail: int) -> None:
    available = {name: path for name, path in RECORD_FILES.items() if path.exists()}
    if not available:
        print("目前没有可查看的加密记录")
        return
    if not stream:
        print("可用记录：" + "、".join(sorted(available)))
        stream = input("请输入要查看的记录名称: ").strip()
    if stream not in available:
        raise S.SecureRecordsError(f"没有这类记录：{stream}")
    rows = S.read_records(available[stream], stream, key)
    for item in rows[-max(1, tail):]:
        print(_display(item))


def main() -> None:
    parser = argparse.ArgumentParser(description="Saylo 加密记录工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="首次设置口令并迁移现有明文")
    sub.add_parser("migrate", help="继续迁移尚存的已知明文文件")
    sub.add_parser("verify", help="校验全部密文")
    sub.add_parser("verify-runtime", help=argparse.SUPPRESS)
    sub.add_parser("relocate-runtime-key", help=argparse.SUPPRESS)
    viewer = sub.add_parser("view", help="在终端实时解密查看")
    viewer.add_argument("--stream", choices=sorted(RECORD_FILES))
    viewer.add_argument("--tail", type=int, default=100)
    args = parser.parse_args()

    if args.command == "setup":
        print("不会保存你的口令。请使用至少 16 个字符、难以猜测且能离线保管的口令。")
        key = S.initialize(_passphrase(confirm=True))
        rows = migrate(key)
        print("\n初始化及迁移完成：")
        print("\n".join("  " + row for row in rows) or "  没有发现待迁移明文")
        print("\n".join("  " + row for row in verify(key)))
        return

    if args.command == "verify-runtime":
        print("\n".join(verify(S.runtime_key())) or "没有发现加密记录")
        return
    if args.command == "relocate-runtime-key":
        target = S.relocate_dpapi_key()
        print(f"DPAPI 运行密钥已迁移并验证：{target}")
        return

    key = S.unlock_with_passphrase(_passphrase())
    if args.command == "migrate":
        print("\n".join(migrate(key)) or "没有发现待迁移明文")
    elif args.command == "verify":
        print("\n".join(verify(key)) or "没有发现加密记录")
    else:
        view(key, args.stream, args.tail)


if __name__ == "__main__":
    try:
        main()
    except (S.SecureRecordsError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"失败：{exc}")
