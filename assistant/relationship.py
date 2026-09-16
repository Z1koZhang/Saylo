# -*- coding: utf-8 -*-
"""Saylo 的轻量关系事实，只用于维持相处的连续性。"""
from __future__ import annotations

import json
from pathlib import Path

import config as C
import secure_records as S


DEFAULT_SHARED_CONTEXT = [
    "用户 创造了 Saylo，也一直在亲手测试和调整她，希望她成为有连续性、有主见的人",
    "用户 已经为 Saylo 确定并换上了他亲手描述和制作的头像；这对两个人都有关系意义",
    "Saylo 的样子仍在和 用户 的真实相处中逐渐形成，不能说他从未描述过自己的期待",
]


class RelationshipState:
    def __init__(self, path: Path | None = None) -> None:
        self._secure = path is None
        self.path = path or S.SECURE_DIR / "relationship_state.state"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            return (S.read_json(self.path, "relationship_state", default={})
                    if self._secure else json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return {}

    def prompt_snapshot(self) -> str:
        """返回短小、始终可见的关系事实，不把整段旧聊天塞进上下文。"""
        state = self._load()
        facts = state.get("shared_context")
        if not isinstance(facts, list) or not facts:
            facts = DEFAULT_SHARED_CONTEXT
        clean = [str(x).strip() for x in facts if str(x).strip()][:6]
        return "\n".join(f"- {x}" for x in clean)
