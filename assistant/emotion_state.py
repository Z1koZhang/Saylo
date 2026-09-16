# -*- coding: utf-8 -*-
"""跨轮保留 Saylo 的情绪底色，把任务模式和情绪状态分开。"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import config as C
import secure_records as S

STATE_FILE = S.SECURE_DIR / "emotion_state.state"
INHERIT_SEC = 30 * 60

# 这些模式本身表达了清楚的新情绪；其余模式只是对话功能，默认继承原有底色。
EMOTIONAL_MODES = {1, 2, 3, 6, 9}
EMOTION_NAMES = {
    1: "温柔关切",
    2: "轻松愉快",
    3: "好奇投入",
    5: "温暖平静",
    6: "欣赏和被触动",
    9: "开心惊喜",
}


class EmotionState:
    def __init__(self, path: Path | None = None) -> None:
        self._secure = path is None
        self.path = path or STATE_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            data = (S.read_json(self.path, "emotion_state", default={}) if self._secure
                    else json.loads(self.path.read_text(encoding="utf-8")))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self, data: dict) -> None:
        if self._secure:
            S.write_json(self.path, "emotion_state", data)
            return
        tmp = self.path.with_suffix(
            self.path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def preview(self, reply_mode: int, now: float | None = None) -> tuple[int, str]:
        """返回本轮应显示的情绪色与给回复模型的连续性提示，不立即提交。"""
        now = now or time.time()
        data = self._load()
        previous = int(data.get("mode", 5)) if str(data.get("mode", 5)).isdigit() else 5
        age = max(0.0, now - float(data.get("updated_at", 0.0) or 0.0))
        if age > INHERIT_SEC:
            previous = 5

        target = reply_mode if reply_mode in EMOTIONAL_MODES else previous
        previous_name = EMOTION_NAMES.get(previous, EMOTION_NAMES[5])
        target_name = EMOTION_NAMES.get(target, previous_name)
        if target == previous:
            prompt = (
                f"上一轮延续下来的情绪底色是“{previous_name}”。本轮继续保留这份底色，"
                "不要因为任务模式变化突然换一种性格或情绪。"
            )
        else:
            prompt = (
                f"上一轮情绪底色是“{previous_name}”，本轮出现了“{target_name}”的新情绪。"
                "自然转过去，但仍保留一点上一轮的余温，不要瞬间换脸。"
            )
        return target, prompt

    def commit(self, mode: int, now: float | None = None) -> None:
        self._save({
            "mode": mode if mode in EMOTION_NAMES else 5,
            "name": EMOTION_NAMES.get(mode, EMOTION_NAMES[5]),
            "updated_at": now or time.time(),
        })
