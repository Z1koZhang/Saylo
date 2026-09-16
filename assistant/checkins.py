# -*- coding: utf-8 -*-
"""Saylo 的随机主动问候调度器。

每天持久化生成 5~6 个随机时点；正在聊天时不插话，错过后也不集中补发。
这里只决定何时可以主动开口；社交问候的具体内容由聊天模型结合近期对话生成。
午夜睡眠提醒也只在这里决定时机，具体内容交给人格模型生成。
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime
from pathlib import Path

import config as C
import secure_records as S


class CheckIns:
    def __init__(self, path: Path | None = None, rng=None) -> None:
        self._secure = path is None
        self.path = path or S.SECURE_DIR / "checkins.state"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rng = rng or random.SystemRandom()

    def _load(self) -> dict:
        try:
            return (S.read_json(self.path, "checkins", default={"sent": []})
                    if self._secure else json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return {"sent": []}

    def _save(self, data: dict) -> None:
        if self._secure:
            S.write_json(self.path, "checkins", data)
        else:
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _day(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    def _ensure_schedule(self, data: dict, now: float) -> tuple[dict, bool]:
        today = self._day(now)
        current = data.get("schedule")
        if isinstance(current, dict) and current.get("day") == today:
            return current, False

        windows = list(getattr(C, "PROACTIVE_TIME_WINDOWS", ()))
        low = max(1, int(getattr(C, "PROACTIVE_MIN_PER_DAY", 5)))
        high = max(low, int(getattr(C, "PROACTIVE_MAX_PER_DAY", 6)))
        count = min(len(windows), self.rng.randint(low, high))
        chosen = sorted(self.rng.sample(range(len(windows)), count))
        day_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0,
                                                        microsecond=0).timestamp()
        slots = []
        for seq, index in enumerate(chosen, 1):
            sh, sm, eh, em = windows[index]
            start_min, end_min = sh * 60 + sm, eh * 60 + em
            minute = self.rng.randrange(start_min, max(start_min + 1, end_min))
            slots.append({"kind": f"social-{seq}",
                          "ts": int(day_start + minute * 60)})
        schedule = {"day": today, "target": count, "slots": slots}
        data["schedule"] = schedule
        return schedule, True

    def due(self, last_activity_ts: float, now: float | None = None) -> tuple[str, str] | None:
        """返回 (种类, 文案)。不满足条件则不主动开口。"""
        if not getattr(C, "PROACTIVE_ENABLED", True):
            return None
        now = time.time() if now is None else now
        dt, data = datetime.fromtimestamp(now), self._load()
        today = self._day(now)
        sent = [r for r in data.get("sent", []) if r.get("day") == today]
        kinds = {r.get("kind") for r in sent}

        # 午夜睡眠提醒只固定时机；00:00~00:04 内只会触发一次，文案由模型生成。
        if dt.hour == getattr(C, "PROACTIVE_SLEEP_HOUR", 0) and dt.minute < 5 and "sleep" not in kinds:
            return "sleep", ""

        schedule, changed = self._ensure_schedule(data, now)
        if changed:
            self._save(data)

        # 旧版的 day/morning 记录也计入当天总数，升级当天不会额外刷屏。
        social = [r for r in sent if r.get("kind") != "sleep"]
        if len(social) >= int(schedule.get("target", 0)):
            return None
        quiet = float(getattr(C, "PROACTIVE_CONVERSATION_QUIET_SEC", 45 * 60))
        if last_activity_ts and now - last_activity_ts < quiet:
            return None
        last_social = max((float(r.get("ts", 0)) for r in social), default=0.0)
        if last_social and now - last_social < float(getattr(C, "PROACTIVE_MIN_GAP_SEC", 75 * 60)):
            return None

        grace = float(getattr(C, "PROACTIVE_SLOT_GRACE_SEC", 90 * 60))
        eligible = [s for s in schedule.get("slots", [])
                    if s.get("kind") not in kinds and
                    float(s.get("ts", 0)) <= now <= float(s.get("ts", 0)) + grace]
        if eligible:
            return max(eligible, key=lambda s: float(s.get("ts", 0)))["kind"], ""
        return None

    def mark_sent(self, kind: str, now: float | None = None,
                  status: str = "sent") -> None:
        now = time.time() if now is None else now
        data = self._load()
        data["sent"] = [r for r in data.get("sent", [])
                        if r.get("day") == self._day(now)]
        data["sent"].append({"day": self._day(now), "kind": kind,
                             "ts": int(now), "status": status})
        self._save(data)
