# -*- coding: utf-8 -*-
"""对话中的短期当前状态。

这里只保存用户明确说出的、数小时内仍可能有效的状态，不把搜索地点、模型猜测或
Saylo 自己说的话写进来。长期偏好仍由 journal.Memories 管理。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import config as C
import secure_records as S

ACTIVE_CONTEXT_FILE = S.SECURE_DIR / "active_context.state"

_WORKING = re.compile(
    r"(?:我)?(?:还在|正在|现在在|目前在)(?:公司|单位|店里|学校)?(?:上班|工作)"
    r"|我在(?:公司|单位|店里|学校)?上班"
    r"|我今天(?:还)?(?:要|得)上班"
)
_WORKING_CASUAL = re.compile(r"当然是上班|还要继续忙|我要继续忙|我得继续忙")
_WORK_ENDED = re.compile(r"(?:已经|终于|刚刚|我)?下班(?:了|啦|咯|喽)|工作结束了|忙完了")
_PLACE = re.compile(r"我(?:现在|目前|还)(?:在|待在)(公司|单位|学校|宿舍|家里|家中|路上|车上|店里)")
_PLACE_ENDED = re.compile(r"(?:到家了|回家了|离开(?:公司|单位|学校|店里)了)")


class ActiveContext:
    """持久化少量带过期时间的当下事实。"""

    def __init__(self, path: Path | None = None, ttl_sec: int | None = None):
        self._secure = path is None
        self.path = path or ACTIVE_CONTEXT_FILE
        self.ttl_sec = ttl_sec or getattr(C, "ACTIVE_CONTEXT_TTL_SEC", 6 * 60 * 60)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = (S.read_json(self.path, "active_context", default={}) if self._secure
                    else json.loads(self.path.read_text(encoding="utf-8")))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, data: dict) -> None:
        if self._secure:
            S.write_json(self.path, "active_context", data)
            return
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _live(data: dict, now: float) -> dict:
        return {
            key: item for key, item in data.items()
            if isinstance(item, dict) and float(item.get("expires_at", 0)) > now
        }

    def observe(self, text: str, observed_at: float | None = None) -> None:
        """只观察用户原话；调用者不得传入 Saylo 回复或网页搜索摘要。"""
        text = (text or "").strip()
        if not text:
            return
        now = observed_at or time.time()
        with self._lock:
            data = self._live(self._read(), now)
            changed = False
            if _WORK_ENDED.search(text):
                changed = bool(data.pop("working", None)) or changed
            elif _WORKING.search(text) or _WORKING_CASUAL.search(text):
                data["working"] = {
                    "text": "用户 明确说自己目前仍在上班或工作；不能默认他现在能离开、吃饭或休息",
                    "source": text[:120],
                    "observed_at": now,
                    "expires_at": now + self.ttl_sec,
                }
                changed = True

            if _PLACE_ENDED.search(text):
                changed = bool(data.pop("place", None)) or changed
            else:
                place = _PLACE.search(text)
                if place:
                    data["place"] = {
                        "text": f"用户 明确说自己目前在{place.group(1)}",
                        "source": text[:120],
                        "observed_at": now,
                        "expires_at": now + self.ttl_sec,
                    }
                    changed = True
            if changed:
                self._write(data)

    def bootstrap(self, rows: list[dict], now: float | None = None) -> None:
        """首次升级或状态文件丢失时，从近几小时用户流水按顺序恢复。"""
        now = now or time.time()
        if self.path.exists():
            return
        for row in rows:
            ts = float(row.get("ts", 0))
            if row.get("who") == "him" and 0 <= now - ts <= self.ttl_sec:
                self.observe(str(row.get("text") or ""), observed_at=ts)

    def render(self, now: float | None = None) -> str:
        now = now or time.time()
        with self._lock:
            raw = self._read()
            data = self._live(raw, now)
            if data != raw:
                self._write(data)
        if not data:
            return ""
        return "\n".join(f"- {item['text']}（用户原话：{item.get('source', '')}）"
                         for item in data.values())
