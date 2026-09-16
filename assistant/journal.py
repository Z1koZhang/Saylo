# -*- coding: utf-8 -*-
"""Saylo 自己的记忆:从“你和它的对话”里提取,记录你从它被创造起都干了什么。

和 `语料/rag` 那个记忆库是两回事:
  记忆库  2023-2026 的历史微信聊天,静态,不再增长
  本模块  你和 Saylo 的对话,动态,一直在长

分三层:
  secure/journal.senc    加密的原始对话流水，一条不落
  secure/memories.senc   从流水里提取的加密事实记录，带时间
  Extractor        后台线程,定期把新流水熬成事实

**提取全程在后台线程里做,回复路径只读不写**,所以不会拖慢聊天。
就算提取失败或 API 挂了,聊天照常。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
import secure_records as S

JOURNAL = S.SECURE_DIR / "journal.senc"
MEMORIES = S.SECURE_DIR / "memories.senc"
CURSOR = C.STORE / "journal.cursor"

_STOP = set("的 了 是 我 你 他 她 它 在 有 就 不 也 都 和 与 啊 吧 呢 吗 呀 哦 这 那 个 们 着 过 很 会 要 说".split())
_TOK = re.compile(r"[一-鿿]+|[a-zA-Z]+|\d+")


def _tokens(text: str) -> set[str]:
    import jieba
    out: set[str] = set()
    for seg in _TOK.findall(text):
        if seg.isascii():
            if len(seg) > 1:
                out.add(seg.lower())
        else:
            out.update(w for w in jieba.cut_for_search(seg) if len(w) > 1)
    return out - _STOP


def ago(ts: int, now: float | None = None) -> str:
    """把时间戳说成“多久之前”。

    模型不会自己算时间差。只给它 "19:05" 和 "现在 19:15",它照样会问
    “课上完了吗”——哪怕对方刚说过要上两小时。必须把“过了多久”
    直接算好写在提示词里。
    """
    d = int((now or time.time()) - ts)
    if d < 90:
        return "刚刚"
    if d < 3600:
        return f"{d // 60}分钟前"
    if d < 86400:
        h, m = divmod(d // 60, 60)
        return f"{h}小时前" if m < 5 else f"{h}小时{m}分钟前"
    return f"{d // 86400}天前"

# ---------------------------------------------------------------- 流水

class Journal:
    """原始对话流水。一条不落地记,提取失败了还能重来。"""

    def __init__(self, path: Path | None = None) -> None:
        self._secure = path is None
        self.path = path or JOURNAL
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, who: str, text: str) -> None:
        """who: 'him'(本人) 或 'saylo'。"""
        text = (text or "").strip()
        if not text:
            return
        rec = {"ts": int(time.time()),
               "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "who": who, "text": text}
        with self._lock:
            if self._secure:
                S.append_record(self.path, "journal", rec)
            else:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            if self._secure:
                return S.read_records(self.path, "journal")
            out = []
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            return out


    def render_recent(self, n: int = 10, max_age_sec: int | None = None) -> str:
        """最近几条对话,**每条都标出是多久之前说的**。

        没有这个,模型对"过了多久"完全没概念:他说要上两小时课,
        十分钟后模型就会问"课上完了吗"。
        """
        now = time.time()
        if max_age_sec is None:
            max_age_sec = getattr(C, "CONTEXT_MAX_AGE_SEC", 30 * 60)
        # 数小时前的流水不是“刚才的上下文”。此前把它们一并塞给模型，
        # 导致它拿早上的失误来调侃当前消息。
        rows = [r for r in self.load() if now - r.get("ts", 0) <= max_age_sec][-n:]
        if not rows:
            return ""
        out = []
        for r in rows:
            who = "他" if r["who"] == "him" else "你"
            out.append(f"[{r['time']} · {ago(r['ts'], now)}] {who}: {r['text']}")
        return "\n".join(out)

    def render_recent_turns(self, turns: int = 6, max_age_sec: int | None = None,
                            max_chars: int | None = None) -> str:
        """按用户发言轮次保留上下文，而不是按微信气泡数量截断。

        一次顾问回复可能拆成四个气泡。旧的“最近六条”因此只能覆盖一两轮，用户刚明确
        说过的当前状态很容易被 Saylo 自己的回复挤出去。
        """
        now = time.time()
        max_age_sec = (max_age_sec if max_age_sec is not None else
                       getattr(C, "CONTEXT_MAX_AGE_SEC", 30 * 60))
        max_chars = max_chars or getattr(C, "REPLY_CONTEXT_MAX_CHARS", 2400)
        rows = [r for r in self.load() if now - float(r.get("ts", 0)) <= max_age_sec]
        user_indices = [i for i, row in enumerate(rows) if row.get("who") == "him"]
        if not user_indices:
            return ""
        start = user_indices[-max(1, turns)] if len(user_indices) >= turns else user_indices[0]
        selected = rows[start:]
        lines = []
        for row in selected:
            who = "他" if row.get("who") == "him" else "你"
            text = str(row.get("text") or "").strip()
            if len(text) > 420:
                text = text[:417] + "…"
            if text:
                lines.append(f"[{row.get('time', '')} · {ago(row.get('ts', 0), now)}] {who}: {text}")
        while len("\n".join(lines)) > max_chars and len(lines) > 1:
            lines.pop(0)
        return "\n".join(lines)

    def sent_texts(self, n: int = 200) -> list[str]:
        """从持久流水恢复己方已发文本，避免重启后把自己的旧气泡认成用户消息。"""
        return [str(row.get("text") or "") for row in self.load()
                if row.get("who") == "saylo" and row.get("text")][-max(1, n):]

    def render_tail(self, n: int = 18) -> str:
        """给主动问候使用的最后一小段历史，不受连续对话的 30 分钟限制。

        每条都保留时间和相对时长，让模型只能把它当作过去发生的事，不能
        擅自假设当时的活动仍在继续。
        """
        now = time.time()
        rows = self.load()[-max(1, n):]
        out = []
        for r in rows:
            who = "他" if r.get("who") == "him" else "你"
            text = str(r.get("text") or "").strip()
            if text:
                out.append(
                    f"[{r.get('time', '')} · {ago(r.get('ts', 0), now)}] {who}: {text}"
                )
        return "\n".join(out)


# ---------------------------------------------------------------- 事实

class Memories:
    """提取出来的事实。数量不大(几百条),分词重合度检索就够,不用上向量。"""

    def __init__(self, path: Path | None = None) -> None:
        self._secure = path is None
        self.path = path or MEMORIES
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._toks: list[set[str]] = []
        self.reload()

    def reload(self) -> None:
        items = []
        if self.path.exists():
            if self._secure:
                items = S.read_records(self.path, "memories")
            else:
                with open(self.path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                items.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
        with self._lock:
            self.items = items
            self._toks = [_tokens(i["text"]) for i in items]

    def _is_dup(self, text: str, toks: set[str]) -> bool:
        """去重。同一件事反复被提取会把提示词撑爆。"""
        if not toks:
            return True
        for i, t in enumerate(self._toks):
            if not t:
                continue
            overlap = len(toks & t) / max(1, min(len(toks), len(t)))
            if overlap >= 0.8 and abs(len(text) - len(self.items[i]["text"])) < 12:
                return True
        return False

    def add(self, facts: list[dict]) -> int:
        """写入新事实,返回实际写入条数。"""
        added = 0
        with self._lock:
            for fa in facts:
                text = (fa.get("text") or "").strip()
                if len(text) < 4:
                    continue
                toks = _tokens(text)
                if self._is_dup(text, toks):
                    continue
                rec = {"ts": fa.get("ts") or int(time.time()),
                       "time": fa.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "kind": fa.get("kind") or "事件",
                       "text": text}
                if self._secure:
                    S.append_record(self.path, "memories", rec)
                else:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.items.append(rec)
                self._toks.append(toks)
                added += 1
        return added

    def recent(self, n: int) -> list[dict]:
        with self._lock:
            return list(self.items[-n:])

    def search(self, query: str, k: int) -> list[dict]:
        q = _tokens(query)
        if not q:
            return []
        with self._lock:
            scored = []
            for i, t in enumerate(self._toks):
                if not t:
                    continue
                s = len(q & t) / len(q)
                if s > 0:
                    scored.append((s, i))
            scored.sort(key=lambda x: -x[0])
            return [self.items[i] for _, i in scored[:k]]

    def render(self, query: str = "", recent_n: int | None = None) -> str:
        """拼成提示词里的一段。

        recent_n 控制"无差别列出最近 N 件事"。**闲聊时必须给 0。**
        否则每条回复都能看到“他在去做家教的路上”这种几小时前的旧事,
        模型会当成他此刻的处境,编出“还在家教那边吗”“晚上还要上到九点呢”。
        查档模式(问"我最近都干了啥")才需要按时间列全。
        """
        if recent_n is None:
            recent_n = getattr(C, "MEM_RECENT_IN_PROMPT", 10)
        recent = self.recent(recent_n) if recent_n else []
        seen = {id(r) for r in recent}
        hits = ([h for h in self.search(query, getattr(C, "MEM_SEARCH_K", 5))
                 if id(h) not in seen] if query else [])
        if not recent and not hits:
            return ""
        lines = []
        if recent:
            lines.append("最近发生的(按时间先后):")
            lines += [f"  [{r['time']} · {ago(r['ts'])}] {r['text']}" for r in recent]
        if hits:
            lines.append("和当前话题相关的:")
            lines += [f"  [{h['time']} · {ago(h['ts'])}] {h['text']}" for h in hits]
        return "\n".join(lines)




# ---------------------------------------------------------------- 提取

_EXTRACT_SYS = """你在帮 Saylo 整理它对朋友的记忆。下面是它和这个朋友的一段对话。

把**过一阵子还有用的事**抽出来。只抽关于**这个朋友(对话里的“他”)**的:

- 事件:发生过的、有结果的事(去了哪、见了谁、做成了什么、出了什么事)
- 处境:会持续一段时间的状态(在实习、准备考研、感冒了、在做家教)
- 计划:他打算做什么、什么时候
- 偏好:他喜欢或讨厌什么,明确表达过的观点、习惯、忌口

**不要抽这些:**
- 纯寒暄(在干嘛、吃了没、晚安)
- Saylo 自己说的话
- **转瞬即逝的状态**。“他在路上”“他正在吃饭”“他还有4站”这种
  过十分钟就不成立了,记下来只会让 Saylo 把旧事当成现在。
  要记就记会持续的那部分:记“他在做家教”,不记“他在去家教的路上”。
- **他前后不一致的地方**。“他说过不做了但又去做了”这种一律不要记。
  人本来就会改主意,记下来只会变成翻旧账的素材。只记最新的状态。
- 模棱两可的猜测。他没明说的别脑补

输出 JSON 数组,每项形如
{"kind": "事件", "text": "一句话,主语用“他”"}
kind 只能是 事件 / 处境 / 计划 / 偏好 之一。
text 要**自带时间线索**(今天下午、9月12日),别只写“他去吃饭了”。
没有值得记的就输出 []。只输出 JSON,不要任何解释。"""


class Extractor(threading.Thread):
    """后台提取线程。

    **刻意不在回复路径上做提取**:提取要调一次 LLM,放在回复流程里会让
    每条消息多等一两秒。这里单开线程,攒够 MEM_EXTRACT_EVERY 条对话
    或者闲置 MEM_EXTRACT_IDLE_SEC 秒就熬一次。
    """

    def __init__(self, journal: Journal, memories: Memories, llm, log=print) -> None:
        super().__init__(daemon=True, name="saylo-extractor")
        self.journal = journal
        self.memories = memories
        self.llm = llm
        self.log = log
        self._cursor = self._load_cursor()
        self._wake = threading.Event()
        self._stop = threading.Event()

    # 游标记在文件里,重启后不会把旧对话重新熬一遍
    @staticmethod
    def _load_cursor() -> int:
        try:
            return int(CURSOR.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _save_cursor(self, n: int) -> None:
        try:
            CURSOR.write_text(str(n), encoding="utf-8")
        except Exception:
            pass

    def nudge(self) -> None:
        """有新对话了,叫醒它看看够不够一批。"""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        idle = getattr(C, "MEM_EXTRACT_IDLE_SEC", 120)
        batch = getattr(C, "MEM_EXTRACT_EVERY", 6)
        while not self._stop.is_set():
            self._wake.wait(timeout=idle)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.flush(batch, idle)
            except Exception as e:
                self.log(f"[记忆] 提取出错(不影响聊天): {e}")

    def flush(self, batch: int = 1, idle: float = 0.0) -> int:
        """把待处理的流水熬成事实。batch=1 且 idle=0 时立即处理。"""
        rows = self.journal.load()
        pending = rows[self._cursor:]
        if not pending:
            return 0
        # 攒够一批,或者已经闲置够久了才收
        if len(pending) < batch and (time.time() - pending[-1]["ts"]) < idle:
            return 0
        dialog = "\n".join(
            f"[{r['time']}] {'他' if r['who'] == 'him' else 'Saylo'}: {r['text']}"
            for r in pending)
        out = self.llm.chat(
            [{"role": "system", "content": _EXTRACT_SYS},
             {"role": "user", "content": dialog}],
            temperature=0.2, max_tokens=800)
        facts = self._parse(out)
        for fa in facts:                       # 用这批对话的时间,不是现在的时间
            fa.setdefault("ts", pending[-1]["ts"])
            fa.setdefault("time", pending[-1]["time"])
        n = self.memories.add(facts)
        self._cursor = len(rows)
        self._save_cursor(self._cursor)
        if n:
            self.log(f"[记忆] 从 {len(pending)} 条对话里记住了 {n} 件事")
        return n

    @staticmethod
    def _parse(out: str) -> list[dict]:
        """模型可能用代码块包裹,也可能夹带解释,尽量把数组捞出来。"""
        s = (out or "").strip()
        s = re.sub(r"^`{3}(?:json)?|`{3}$", "", s, flags=re.M).strip()
        m = re.search(r"\[.*\]", s, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return []
        return [d for d in data if isinstance(d, dict) and d.get("text")]


if __name__ == "__main__":
    j, m = Journal(), Memories()
    rows = j.load()
    print(f"流水 {len(rows)} 条 | 记住的事 {len(m.items)} 件\n")
    if "--facts" in sys.argv:
        for it in m.items:
            print(f"  [{it['time']}] ({it['kind']}) {it['text']}")
    else:
        print(m.render() or "(还没有记忆)")
