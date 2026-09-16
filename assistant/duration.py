# -*- coding: utf-8 -*-
"""从他说的话里抠出时长,替模型把时间算好。

为什么要专门做这个:模型算不明白时间。上下文里明明写着“10分钟前”,
它照样能说成“这才一个小时呢”;他说要上两小时课,过了一小时它就问
“课上完了吧”。给它原始时间让它自己推,基本是碰运气。

所以这里用正则把时长抠出来,在代码里算清楚“过了多久、还剩多久”,
直接把结论写进提示词。模型只要照着念就行,不用做算术。
"""
from __future__ import annotations

import re
import time

_CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# “两个小时”“1个半小时”“半小时”“40分钟”“一小时二十分”
_HOUR = re.compile(r"([0-9]+|[一二两三四五六七八九十百]+)\s*个?\s*半?\s*(?:小时|钟头)")
_HALF_HOUR = re.compile(r"半\s*(?:个\s*)?(?:小时|钟头)")
_AND_HALF = re.compile(r"([0-9]+|[一二两三四五六七八九十百]+)\s*个?\s*半\s*(?:小时|钟头)")
_MIN = re.compile(r"([0-9]+|[一二两三四五六七八九十百]+)\s*分钟?")

# 说这事**正要开始或正在进行**的线索。没有这些就可能是在讲过去的事
# (“我昨天睡了两个小时”),那种不该算成进行中。
_ONGOING = re.compile(r"要去|要上|去上|准备去|马上|等下|待会|接下来|正在|在上|"
                      r"得上|要弄|要做|还要|上课|上班|开会|做家教|加班|考试")
_PAST = re.compile(r"昨天|前天|上周|上个月|刚睡|睡了|已经结束|上完了|做完了|下课了")


def _num(s: str) -> int:
    if s.isdigit():
        return int(s)
    # 覆盖日常会说的“一小时”“十分钟”“二十五分钟”“一百二十分钟”。
    if s in _CN:
        return _CN[s]
    if "百" in s:
        left, _, right = s.partition("百")
        hundreds = _CN.get(left, 1) if left else 1
        return hundreds * 100 + (_num(right) if right else 0)
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN.get(left, 1) if left else 1
        ones = _CN.get(right, 0) if right else 0
        return tens * 10 + ones
    return 0


def find_minutes(text: str) -> int | None:
    """从一句话里抠出时长,返回分钟数。抠不出来返回 None。"""
    if not text:
        return None
    m = _AND_HALF.search(text)              # “一个半小时”要先于“一个小时”判
    if m:
        return _num(m.group(1)) * 60 + 30
    if _HALF_HOUR.search(text):
        return 30
    total = 0
    m = _HOUR.search(text)
    if m:
        total += _num(m.group(1)) * 60
    m2 = _MIN.search(text)
    if m2:
        total += _num(m2.group(1))
    return total or None


def scan(rows: list[dict], now: float | None = None) -> str:
    """扫最近的对话,找出“他说要花多久做某事”,算出进度。

    rows 是流水记录(带 ts / who / text)。返回一段可以直接塞进提示词的话,
    没找到就返回空串。
    """
    now = now or time.time()
    for r in reversed(rows[-12:]):
        if r.get("who") != "him":
            continue
        text = r.get("text") or ""
        if _PAST.search(text) or not _ONGOING.search(text):
            continue
        mins = find_minutes(text)
        if not mins:
            continue
        elapsed = int((now - r["ts"]) / 60)
        if elapsed > mins + 240:            # 早过完了,没必要再提
            return ""
        left = mins - elapsed
        head = (f"他在 {r['time']} 说过:“{text[:40]}”,提到要花 {mins} 分钟。"
                f"到现在过了 {elapsed} 分钟。")
        if left > 0:
            return (f"{head}**按他说的时长,还差 {left} 分钟才结束,"
                    f"所以现在别问他做完没做完。**")
        return (f"{head}按他说的时长早该结束了,可以问问怎么样了。")
    return ""


if __name__ == "__main__":
    for t in ["我要去给学生上课了，两个小时", "半小时就回来", "还要一个半小时",
              "等下要考试，40分钟", "我昨天睡了两个小时", "上完了，两个小时累死"]:
        print(f"  {t!r:34s} -> {find_minutes(t)} 分钟"
              f"{'  (过去式,不算进行中)' if _PAST.search(t) else ''}")
