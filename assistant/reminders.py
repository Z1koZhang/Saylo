# -*- coding: utf-8 -*-
"""定时提醒。

起因:他说“我半个小时后到学校，半个小时后你问我到学校没”,Saylo 答应了
“行，那半小时后我准时问你”——但它**根本做不到**。整个 agent 是纯被动的:
轮询到新消息才回,没人说话它就什么都不干。它答应了一件自己没有能力做的事。

这里补上这个能力:把“X 分钟后问我 Y”存下来,到点了主动发一条。

注意这是**唯一一处 Saylo 可以主动开口**的地方,所以:
- 只在他明确要求时才建(得同时出现时间和“提醒/问/叫”这类词)
- 到点只发一次,发完就标记完成
- 仍然受发送冷却约束,只是不受“连续几轮没等到用户消息”那道闸限制——
  定时提醒本来就发生在没人说话的时候
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
import secure_records as S
from duration import find_minutes

STORE_FILE = S.SECURE_DIR / "reminders.senc"

# 动作必须紧跟在“X 分钟后”之后。不能在整句话任意位置搜“提醒我”，否则
# “一分钟后告诉我，你已经能主动提醒我了”里的“主动提醒我”会被误判为指令。
_ACTION_WORDS = (
    r"提醒(?:一下)?我|问(?:一下)?我|记得问|到时候问|到点问|"
    r"催我|喊我|叫醒我|叫我|告诉我|跟我说|发消息告诉我"
)
_ACTION = re.compile(
    r"^[\s，,。]*?(?:你[\s，,。]*)?"
    rf"(?P<action>{_ACTION_WORDS})")
# “半个小时后”“30分钟后”“两小时之后”。没有“后/之后”就不是定时。
_AFTER = re.compile(r"(后|之后|以后)")
_CLOCK_ACTION = re.compile(
    r"(?P<day>今天|明天|今晚|今早|明早)?[\s，,]*"
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?[\s，,]*"
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})[点时]"
    r"(?:(?P<minute>半|\d{1,2}|[零〇一二两三四五六七八九十]{1,3})分?)?"
    r"(?:钟)?(?:的时候|左右)?[\s，,。]*?(?:你[\s，,。]*)?"
    rf"(?P<action>{_ACTION_WORDS})"
)
_ACTION_CLOCK = re.compile(
    r"(?:^|[，,。])\s*(?:请(?:你)?|麻烦(?:你)?|帮我|你|能不能)?\s*"
    rf"(?P<action>{_ACTION_WORDS})\s*(?:在)?\s*"
    r"(?P<day>今天|明天|今晚|今早|明早)?[\s，,]*"
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?[\s，,]*"
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})[点时]"
    r"(?:(?P<minute>半|\d{1,2}|[零〇一二两三四五六七八九十]{1,3})分?)?"
    r"(?:钟)?(?:的时候|左右)?"
)
_ACTION_DELAY = re.compile(
    r"(?:^|[，,。])\s*(?:请(?:你)?|麻烦(?:你)?|帮我|你|能不能)?\s*"
    rf"(?P<action>{_ACTION_WORDS})\s*"
    r"(?P<delay>[^，,。；;！!？?]{1,16}?)(?:后|之后|以后)"
)
_CLOCK_MENTION = re.compile(
    r"(?P<day>今天|明天|今晚|今早|明早)?[\s，,]*"
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?[\s，,]*"
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})[点时]"
    r"(?:(?P<minute>半|\d{1,2}|[零〇一二两三四五六七八九十]{1,3})分?)?"
)
_REMINDER_ACTION_ANYWHERE = re.compile(rf"(?:{_ACTION_WORDS})")
_TIME_CUE = re.compile(
    r"(?:\d+|[零〇一二两三四五六七八九十半两]+)(?:分钟?|小时|点|时)|"
    r"今天|明天|今晚|明早|早上|上午|中午|下午|晚上|夜里|之后|以后"
)
_NON_REQUEST_DISCUSSION = re.compile(
    r"记不记得|还记得|但是你却|但你却|(?:没|没有|未)提醒|我不是让你|"
    r"为什么.{0,12}提醒|提醒.{0,12}(?:取消|删掉)|(?:取消|删掉).{0,12}提醒"
)
_CANCEL_REQUEST = re.compile(r"(?:取消|删掉|删除|不要)(?:.{0,20})提醒|提醒(?:.{0,20})(?:取消|删掉|删除)")


def _cn_int(raw: str) -> int | None:
    """解析提醒钟点会用到的 0~59 中文数字。"""
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if raw == "十":
        return 10
    if "十" in raw:
        left, right = raw.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    try:
        return int("".join(str(digits[ch]) for ch in raw))
    except (KeyError, ValueError):
        return None


def looks_like_request(text: str) -> bool:
    """检测像提醒指令但尚未成功解析的消息，供发送层阻止假确认。"""
    return bool(text and not _NON_REQUEST_DISCUSSION.search(text) and
                _REMINDER_ACTION_ANYWHERE.search(text) and _TIME_CUE.search(text))


def is_cancel_request(text: str) -> bool:
    return bool(text and _CANCEL_REQUEST.search(text))


def cancellation_targets(text: str, pending: list[dict],
                         now: float | None = None) -> list[dict]:
    """从取消指令中挑出目标；不确定时返回空列表，不做猜测性删除。"""
    if not is_cancel_request(text) or not pending:
        return []
    if re.search(r"全部|所有|都(?:取消|删)", text):
        return list(pending)

    clock = _CLOCK_MENTION.search(text)
    if clock:
        target_ts = _clock_timestamp(clock, now or time.time())
        if target_ts is not None:
            # 用户说的分钟若省略，匹配同一小时；说了分钟则精确到分钟。
            has_minute = bool(clock.group("minute"))
            target = datetime.fromtimestamp(target_ts)
            matches = []
            for row in pending:
                due = datetime.fromtimestamp(float(row.get("due_ts", 0)))
                same = due.date() == target.date() and due.hour == target.hour
                if has_minute:
                    same = same and due.minute == target.minute
                if same:
                    matches.append(row)
            if matches:
                return matches

    if re.search(r"刚刚|刚才|最后(?:一个)?|最新(?:的)?", text):
        return [pending[-1]]
    return list(pending) if len(pending) == 1 else []


def _result(due: float | None, what: str, action: str) -> tuple[float, str, str] | None:
    what = (what or "").strip(" ，,。.、？?！!呀啊啦呢哦~～")
    if due is None or len(what) < 2:
        return None
    # 保存提醒的动作语义，而不是把用户句中的“该/要……了”原封不动交给到点文案。
    # 否则生成器自然补全语气时可能得到重复助动词。这不是固定回复替换，
    # 只是把提醒 payload 规范成可重新措辞的动作。
    modal = re.fullmatch(r"(?:我)?(?:应该|该|要|得)\s*(.+?)(?:了)?", what)
    if modal and len(modal.group(1).strip()) >= 2:
        what = modal.group(1).strip()
    if any(x in action for x in ("告诉", "跟我说", "发消息")):
        tmp = "\0"
        what = what.replace("我", tmp).replace("你", "我").replace(tmp, "你")
        what = re.sub(r"了$", "啦", what)
        kind = "announce"
    else:
        kind = "question" if ("问" in action or re.search(r"(?:没|吗)$", what)) else "remind"
    return due, what[:60], kind


def _clock_timestamp(match: re.Match[str], now: float) -> float | None:
    current = datetime.fromtimestamp(now)
    hour = _cn_int(match.group("hour"))
    minute_raw = match.group("minute") or "0"
    minute = 30 if minute_raw == "半" else _cn_int(minute_raw)
    if hour is None or minute is None or not 0 <= minute < 60 or not 0 <= hour <= 23:
        return None

    day = match.group("day") or ""
    period = match.group("period") or ""
    if day == "今晚" and not period:
        period = "晚上"
    elif day in {"今早", "明早"} and not period:
        period = "早上"
    if period in {"下午", "傍晚", "晚上", "夜里"} and 1 <= hour < 12:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    elif period == "中午" and 1 <= hour < 11:
        hour += 12

    explicit_tomorrow = day in {"明天", "明早"}
    explicit_today = day in {"今天", "今晚", "今早"}
    base = current + timedelta(days=1 if explicit_tomorrow else 0)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if explicit_today:
        return candidate.timestamp() if candidate > current else None
    if explicit_tomorrow:
        return candidate.timestamp()

    # 没说早晚时选最近的未来钟点。例如晚上 21:30 说“十一点”通常指 23:00，
    # 而上午 10:00 说“十一点”则指 11:00。
    candidates = [candidate]
    if not period and 1 <= hour <= 11:
        candidates.append(candidate.replace(hour=hour + 12))
    elif not period and hour == 12:
        candidates.append(candidate.replace(hour=0))
    future = [dt for dt in candidates if dt > current]
    if not future:
        tomorrow = candidate + timedelta(days=1)
        future = [tomorrow]
        if not period and 1 <= hour <= 11:
            future.append(tomorrow.replace(hour=hour + 12))
        elif not period and hour == 12:
            future.append(tomorrow.replace(hour=0))
    return min(future).timestamp()


def parse(text: str, now: float | None = None) -> tuple[float, str, str] | None:
    """从一句话里解析出定时提醒。解析不出来返回 None。

    返回 (到期时间戳, 内容, 类型)。类型是 ``question`` 或 ``remind``。
    """
    if not text or _NON_REQUEST_DISCUSSION.search(text):
        return None
    now = now or time.time()

    # 绝对钟点：“十一点的时候提醒我准备睡觉”“明早八点叫我起床”。
    clock = _CLOCK_ACTION.search(text)
    if clock:
        return _result(_clock_timestamp(clock, now), text[clock.end():],
                       clock.group("action"))

    # 动作在时间前：“提醒我明早八点起床”“提醒我半小时后关火”。
    action_clock = _ACTION_CLOCK.search(text)
    if action_clock:
        return _result(_clock_timestamp(action_clock, now), text[action_clock.end():],
                       action_clock.group("action"))
    action_delay = _ACTION_DELAY.search(text)
    if action_delay:
        mins = find_minutes(action_delay.group("delay"))
        due = now + mins * 60 if mins and 1 <= mins <= 60 * 24 else None
        return _result(due, text[action_delay.end():], action_delay.group("action"))

    if not _AFTER.search(text):
        return None
    # 一句话可能有两个“后”（例如“半小时后到学校，半小时后你问我”），
    # 只接受某个“后”之后明确开头的动作。
    action_match = None
    for after in _AFTER.finditer(text):
        candidate = _ACTION.match(text[after.end():])
        if candidate:
            action_match = (after, candidate)
            break
    if action_match is None:
        return None
    after, m = action_match
    # 时长只从这次“后”之前的最近一句里取，避免多段时间混用。
    prefix = text[:after.start()]
    # 取最近一个分句的时长：“10分钟后到家，1小时后问我”要取 1 小时。
    cut = max(prefix.rfind(ch) for ch in "，,。；;！!？?")
    prefix = prefix[cut + 1:]
    mins = find_minutes(prefix)
    if not mins or mins < 1 or mins > 60 * 24:
        return None
    # 提醒即使 API 暂时不可用也能准时送达，所以类型和人称在程序内确定。
    return _result(now + mins * 60, text[after.end() + m.end():], m.group("action"))


class Reminders:
    """待办提醒。条数极少,直接整个文件读写,不做索引。"""

    def __init__(self) -> None:
        self.path = STORE_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        return S.read_records(self.path, "reminders")

    def _save(self, rows: list[dict]) -> None:
        S.write_records(self.path, "reminders", rows)

    def add(self, due_ts: float, what: str, kind: str = "remind") -> dict:
        rec = {"id": f"r{int(time.time()*1000)}", "due_ts": int(due_ts),
               "due": datetime.fromtimestamp(due_ts).strftime("%Y-%m-%d %H:%M:%S"),
               "what": what, "kind": kind, "done": False}
        with self._lock:
            rows = self._load()
            rows.append(rec)
            self._save(rows)
        return rec

    def due(self, now: float | None = None) -> list[dict]:
        """到点且还没发过的。"""
        now = now or time.time()
        with self._lock:
            return [r for r in self._load()
                    if not r.get("done") and r["due_ts"] <= now]

    def mark_done(self, rid: str) -> None:
        with self._lock:
            rows = self._load()
            for r in rows:
                if r["id"] == rid:
                    r["done"] = True
            self._save(rows)

    def cancel(self, rid: str) -> bool:
        """保留记录但标成已取消，便于审计且不会在到点时发送。"""
        changed = False
        with self._lock:
            rows = self._load()
            for r in rows:
                if r.get("id") == rid and not r.get("done"):
                    r["done"] = True
                    r["cancelled"] = True
                    r["cancelled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    changed = True
            self._save(rows)
        return changed

    def reschedule(self, rid: str, due_ts: float) -> bool:
        """纠正一个尚未完成提醒的时间，同时留下原时间审计字段。"""
        changed = False
        with self._lock:
            rows = self._load()
            for r in rows:
                if r.get("id") == rid and not r.get("done"):
                    r.setdefault("original_due", r.get("due"))
                    r["due_ts"] = int(due_ts)
                    r["due"] = datetime.fromtimestamp(due_ts).strftime("%Y-%m-%d %H:%M:%S")
                    r["corrected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    changed = True
            self._save(rows)
        return changed

    def pending(self) -> list[dict]:
        return [r for r in self._load() if not r.get("done")]

    def next_due(self) -> float | None:
        """下一个未完成提醒的时间戳；没有则返回 None。"""
        pending = self.pending()
        return min((float(r["due_ts"]) for r in pending), default=None)


if __name__ == "__main__":
    now = time.time()
    tests = [
        "我半个小时后到学校，半个小时后你问我到学校没",
        "40分钟后提醒我关火",
        "两小时后叫我一下",
        "我半小时后到学校",                      # 只讲行程,不该建提醒
        "记得问我作业写完没",                    # 没有时间,不该建
        "我昨天半小时就到了",                    # 过去式
    ]
    for t in tests:
        r = parse(t, now)
        if r:
            print(f"  建提醒 {int((r[0]-now)/60):3d} 分钟后: {r[1]!r} ({r[2]})  <- {t}")
        else:
            print(f"  不建提醒                        <- {t}")
    print()
    rm = Reminders()
    print("当前待办:", rm.pending() or "(无)")
