# -*- coding: utf-8 -*-
"""微信自动回复循环。

    python assistant/wx_agent.py            # 按 config 的安全设置跑
    python assistant/wx_agent.py --once     # 只扫一轮就退出,适合调试
    python assistant/wx_agent.py --live     # 真发消息(必须显式加,且白名单非空)

三道闸,默认全部拦死:
    DRY_RUN=True          只打印不发
    REPLY_WHITELIST = []    空 = 谁都不回
    MAX_REPLIES_PER_HOUR  熔断
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
import secure_records as S
from active_context import ActiveContext
from checkins import CheckIns
import duration
from emotion_state import EmotionState
import reminders as _rem
from brain import (Assistant, MODE_NAMES, MODE_SEGMENT_LIMITS, SKIP_PROACTIVE,
                   wants_web_search)
from relationship import RelationshipState
from runtime_state import RuntimeBridge
from wx_bridge import WeChatUI

HOLD = "[需要本人处理]"
# 分条分隔符。标准是单独一行 ---,但模型经常只用换行,所以换行一律当分隔。
_SPLIT = re.compile(r"\n\s*-{3,}\s*\n|\n+")
# 切完要滤掉"只剩分隔符"的段落。换行一律分条之后,模型输出
# "嗯\n---\n我怎么问你啊" 会把中间那行 --- 切成独立一段,直接发出去。
_DASH_ONLY = re.compile(r"^[\s\-—–_=*.]+$")
# 这是模型和程序之间的控制标记，发送给微信前必须删掉。
_TRAILING_PERIOD = re.compile(r"(?:。|(?<!\.)\.(?!\.))(?P<close>[”’\"）】》]*)$")


def normalize_segment(text: str) -> str:
    """微信气泡末尾不留单个句号；保留句中句号、问号、叹号和省略号。"""
    text = (text or "").strip()
    for banned in getattr(C, "BANNED_EXPRESSIONS", ()):
        text = text.replace(str(banned), "")
    return _TRAILING_PERIOD.sub(r"\g<close>", text).strip()


def split_segments(text: str) -> list[str]:
    """把模型的回复切成一条条要发的消息。"""
    out = []
    for seg in _SPLIT.split(text or ""):
        seg = seg.strip()
        if seg and not _DASH_ONLY.match(seg):
            out.append(normalize_segment(seg))
    return out


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    S.secure_log("agent_log", line)


class Agent:
    def __init__(self, live: bool = False):
        # 能进入 Agent 初始化说明依赖与模块加载已经完成；完整就绪后由运行桥写状态。
        startup_diagnostic = C.STORE / "startup_diagnostic.json"
        self.wx = WeChatUI()
        self.ai = Assistant()
        self.live = live
        self.dry = C.DRY_RUN and not live
        self.seen: dict[str, str] = {}       # 会话名 -> 最后见到的预览指纹
        self.seen_msgs: dict[str, list[str]] = {}   # 会话名 -> 上次读到的消息列表
        self.based: dict[str, bool] = {}            # 会话名 -> 是否已记录基线
        self._warned_unreadable = False             # 微信离线/锁屏提示只打一次
        self._warned_nochat = False                 # 提示只打一次,别刷屏
        self._warned_chat: str | None = None
        self.sent_log: deque[str] = deque(maxlen=200)   # 自己发过的话,用来区分谁发的

        # 流水也承担跨重启发送方识别，所以即使关闭长期记忆提取也要加载 Journal。
        self.journal = None
        self.extractor = None
        try:
            from journal import Journal
            self.journal = Journal()
            self.sent_log.extend(self.journal.sent_texts(200))
        except Exception as e:
            log(f"[流水] 启动失败,聊天不受影响: {e}")
        if getattr(C, "MEM_ENABLED", True) and self.journal is not None:
            try:
                from journal import Extractor, Memories
                mem = self.ai.mem or Memories()
                self.extractor = Extractor(self.journal, mem, self.ai.llm, log=log)
                self.extractor.start()
            except Exception as e:
                log(f"[记忆] 启动失败,聊天不受影响: {e}")
        self.active_context = ActiveContext()
        if self.journal is not None:
            try:
                self.active_context.bootstrap(self.journal.load())
            except Exception as e:
                log(f"[当前状态] 恢复失败,聊天不受影响: {e}")
        self.sent_times: deque[float] = deque()
        self.last_sent = 0.0
        self.since_user = 0                  # 连续几轮没等到新的用户消息
        self._img_cache: dict[str, str] = {}  # 图片描述缓存,免得每轮重看
        self._last_reminder_status = 0.0
        self._warmed = False
        self._warmup_deferred_notice = False
        self.relationship = RelationshipState()
        self.emotion = EmotionState()
        self.checkins = CheckIns()
        self._last_proactive_attempt = 0.0
        self._reply_turn = 0
        self._last_expression_turn = -999
        self.pending_inputs: dict[str, list[str]] = {}
        self.pending_recent: dict[str, list[str]] = {}
        self.pending_updated: dict[str, float] = {}
        self.pending_failures: dict[str, int] = {}
        self.pending_retry_after: dict[str, float] = {}
        self._last_send_count = 0
        self.runtime = RuntimeBridge()
        startup_diagnostic.unlink(missing_ok=True)
        try:
            self.reminders = _rem.Reminders()
        except Exception:
            self.reminders = None

    # ------------------------------------------------------------ 限流

    def _allowed(self) -> tuple[bool, str]:
        """能不能回。

        熔断防的是**机器人自己刷屏**,不是防用户聊天。
        所以主判据是“连续几轮没等到新的用户消息”——正常对话里这个数永远是 0,
        只有陷入自问自答才会一路涨上去。用户一发消息就归零(见 handle)。

        早先按“每小时回了多少条”算,结果分条一激进,一轮就是五六条,
        聊几句就把自己拦下了——那是在拦用户,不是在拦故障。
        """
        now = time.time()
        limit = getattr(C, "MAX_REPLIES_WITHOUT_USER", 3)
        if self.since_user >= limit:
            return False, (f"连续 {self.since_user} 轮没等到你的新消息,先停一下"
                           f"(防自问自答;你再发一条就恢复)")
        while self.sent_times and now - self.sent_times[0] > 3600:
            self.sent_times.popleft()
        if len(self.sent_times) >= C.MAX_REPLIES_PER_HOUR:
            return False, (f"兜底熔断:一小时 {len(self.sent_times)} 轮,"
                           f"上限 {C.MAX_REPLIES_PER_HOUR}")
        if now - self.last_sent < C.REPLY_COOLDOWN_SEC:
            return False, "冷却中"
        return True, ""


    # ------------------------------------------------------------ 一轮

    def _warmup_if_safe(self) -> bool:
        """预热不能抢占即将到点的提醒；提醒优先级永远更高。"""
        if self._warmed:
            return True
        if not getattr(C, "WARMUP_EMBEDDING_ON_START", False):
            self._warmed = True
            log("embedding 设为按需加载：普通聊天启动不载入历史向量模型")
            return True
        if self.reminders is not None:
            due = self.reminders.next_due()
            if due is not None and due - time.time() < 45:
                if not self._warmup_deferred_notice:
                    log("[预热] 有不到 45 秒的倒计时，先不预热，保证提醒准时")
                    self._warmup_deferred_notice = True
                return False
        log("预热 embedding 模型(首次要几十秒,省得压在第一条消息上)...")
        log(f"预热完成,耗时 {self.ai.warmup():.1f}s")
        self._warmed = True
        return True

    def tick(self) -> None:
        """盯住已经打开的那个会话,读增量消息。

        **轮询全程不抢焦点。** 读控件树不需要窗口在前台(实测焦点移走 12 秒后
        照样读得到),只有发消息才需要键盘焦点。早先版本每轮开头就激活窗口,
        结果微信每 6 秒被拽到前台一次,表现为反复最小化又弹出。
        """
        # 倒计时和微信新消息共用同一个主循环。先报状态，避免提醒被普通消息
        # 的早退分支饿死；真正到点后还会优先处理。
        self._log_reminder_status()
        if not self.wx.readable():
            restored = self.wx.restore_if_minimized()
            if restored and self.wx.readable():
                log("[恢复] 微信之前被最小化了,已还原")
                self._warned_unreadable = False
            else:
                if not self._warned_unreadable:
                    log("[等待] 读不到微信控件树,跳过这轮")
                    self._warned_unreadable = True
                self.runtime.set("error", label="暂时读不到微信")
                return
        elif self._warned_unreadable:
            log("[恢复] 已重新读到微信控件树")
            self._warned_unreadable = False
        # 到点时不要求用户正好停留在白名单会话。先切回唯一允许主动发送的会话。
        if self._check_reminders():
            return

        chat = self.wx.current_chat()
        if not chat:
            if not self._warned_nochat:
                self._warned_nochat = True
                log(f"[等待] 微信里没有打开任何聊天。请点开“{'/'.join(C.REPLY_WHITELIST)}”")
            return
        self._warned_nochat = False
        if chat not in C.REPLY_WHITELIST:
            if self._warned_chat != chat:
                self._warned_chat = chat
                log(f"[跳过] 当前打开的是“{chat}”,不在白名单")
            return
        self._warned_chat = None
        self.handle(chat)
        self._check_proactive(chat)

    @staticmethod
    def _new_start(prev: list[str], cur: list[str]) -> int:
        """返回 cur 里第一条“上次之后新增”消息的下标。

        消息列表是虚拟化的,只能看到视口内一段,前后两次读到的窗口会滑动:
        上次 [m1..m10],这次可能是 [m3..m13]。所以找**最大重叠**:
        最大的 k 使 prev[-k:] == cur[:k],那么 cur[k:] 就是新增的。

        取最大的 k 而不是最小的,是为了在内容重复时(连着发三个“Hi”)
        尽量少判新增——宁可漏回一条,也不要把历史消息当新消息重复回复。

        返回下标而不是文本列表:发送方要按下标去取,用文本当 key 的话,
        两条内容相同、方向不同的消息会互相覆盖。
        """
        if not cur:
            return 0
        if not prev:
            return len(cur)                  # 首次只记基线,绝不回复历史
        for k in range(min(len(prev), len(cur)), 0, -1):
            if prev[-k:] == cur[:k]:
                return k
        return max(0, len(cur) - 1)          # 完全对不上,保守只认最后一条

    def _classify_sides(self, texts: list[dict]) -> list[str]:
        """判断每条消息是谁发的。

        **不靠像素。** 早先用气泡颜色/位置推断,结果用户换了微信主题,
        自己的气泡不再是绿色,判据全线失效(实测 6 条错 4 条),
        导致机器人开始回复自己发的消息。

        现在用确定性判据:机器人自己发过什么,它心里有数。
        用 Counter 而不是 set,是为了处理重复内容——
        它说过两次“怎么可能”,就只认掉两条。
        剩下的必然是对方发的。
        """
        pending = Counter(self.sent_log)
        sides = []
        for m in texts:
            t = m["text"]
            if pending.get(t, 0) > 0:
                pending[t] -= 1
                sides.append("self")
            else:
                sides.append("other")
        return sides

    def _has_unprocessed_user_message(self, name: str) -> bool:
        """发送前再看一眼：生成期间有没有收到新的对方消息。

        这里刻意只检查、不更新 ``seen_msgs``。这样下一轮 ``handle`` 仍会把
        新消息完整地收入待回复队列，而不会被这次探测吃掉。
        """
        try:
            texts = [m for m in self.wx.messages(20)
                     if m["type"] in ("text", "image")]
            cur = [m["text"] for m in texts]
            start = self._new_start(self.seen_msgs.get(name, []), cur)
            if start >= len(texts):
                return False
            return any(side == "other" for side in
                       self._classify_sides(texts)[start:])
        except Exception as e:
            # 看不到控件树时宁可继续发送；不能因为一次临时读取失败而吞掉回复。
            log(f"[合并消息] 发送前检查失败，继续本轮：{e}")
            return False

    def _pending_ready_in(self) -> float | None:
        """返回最早一组待合并消息还需等待的秒数。"""
        if not self.pending_updated:
            return None
        settle = getattr(C, "MESSAGE_BATCH_SETTLE_SEC", 1.5)
        ready_times = [
            max(ts + settle, self.pending_retry_after.get(name, 0.0))
            for name, ts in self.pending_updated.items()
        ]
        return max(0.0, min(ready_times) - time.time())

    @staticmethod
    def _typing_delay(text: str) -> float:
        """模拟真人打字:内容越长,发出来越慢。

        秒回一大段话很假。按每秒 TYPING_CPS 个字算,加一个基础思考时间,
        再按 config 的上下限夹住。
        """
        cps = getattr(C, "TYPING_CPS", 4.5)
        base = getattr(C, "TYPING_BASE_SEC", 0.8)
        lo = getattr(C, "TYPING_MIN_SEC", 1.0)
        hi = getattr(C, "TYPING_MAX_SEC", 9.0)
        return max(lo, min(hi, base + len(text) / max(cps, 0.5)))

    def handle(self, name: str) -> None:
        # 硬闸:无论调用方怎么传,只回本人的号
        if getattr(C, "STRICT_SELF_ONLY", True) and name not in C.REPLY_WHITELIST:
            log(f"[硬闸] “{name}”不在白名单,拒绝回复")
            return
        msgs = self.wx.messages(20)
        # 图片也要进跟踪列表,否则收到图片时"没有新消息",它就不理你
        texts = [m for m in msgs if m["type"] in ("text", "image")]
        if not texts:
            self._quiet("读不到任何消息(聊天窗口可能被关了或正在加载)")
            return

        cur = [m["text"] for m in texts]
        start = self._new_start(self.seen_msgs.get(name, []), cur)
        self.seen_msgs[name] = cur
        if not self.based.get(name):
            self.based[name] = True
            log(f"[基线] 记下 {len(cur)} 条历史消息,从现在起只回新消息")
            return

        # 按下标取,不用文本当 key:两条内容相同方向不同的消息会互相覆盖
        sides = self._classify_sides(texts)
        for m, sd in zip(texts, sides):
            m["side"] = sd                      # 覆盖掉像素猜的结果
        fresh = texts[start:]
        # 新消息里有图片就先看一眼,把内容变成文字再交给聊天模型
        fresh_other = [m for m in fresh if m["side"] == "other"]
        if fresh_other:
            self.runtime.set("listening", label="收到新消息")
            # 第一条进队列时先冻结上下文；随后连发的内容会和它合成同一轮，
            # 但不会把未来才出现的 Saylo 回复误塞回上下文。
            pending = self.pending_inputs.setdefault(name, [])
            if not pending:
                self.pending_recent[name] = (
                    [self.journal.render_recent_turns(
                        getattr(C, "REPLY_CONTEXT_TURNS", 6),
                        max_chars=getattr(C, "REPLY_CONTEXT_MAX_CHARS", 2400),
                    )] if self.journal else
                    [f"{'我' if m['side'] == 'self' else '他'}: {m['text']}"
                     for m in texts[-getattr(C, "REPLY_CONTEXT_MESSAGES", 6):]]
                )
            bits = []
            for m in fresh_other:
                if m["type"] == "image":
                    d = self._describe_image(m)
                    bits.append(f"[他发来一张图片:{d}]" if d else "[他发来一张图片,你没看清内容]")
                else:
                    bits.append(m["text"])
            pending.extend(bits)
            self.pending_updated[name] = time.time()
            # 有了新的用户补充，就立即用合并后的完整内容重新处理，不继承旧失败退避。
            self.pending_failures.pop(name, None)
            self.pending_retry_after.pop(name, None)
            self.since_user = 0          # 他发话了,说明不是自循环,熔断计数归零
            for bit in bits:
                self._remember("him", bit)
                try:
                    self.active_context.observe(bit)
                except Exception as e:
                    log(f"[当前状态] 更新失败,聊天不受影响: {e}")

            # 留出一个很短的安静窗口收集连发消息。图片 + 紧跟的一句提问、
            # 多句补充说明都会在这里合并，模型只回复一次。
            log(f"[合并消息] 收到 {len(bits)} 条，等待继续输入")
            return

        if fresh:
            self._quiet(f"有 {len(fresh)} 条新消息但都不是他发的")
        if name not in self.pending_inputs:
            return
        ready_in = self._pending_ready_in()
        if ready_in is not None and ready_in > 0:
            return
        incoming = "\n".join(self.pending_inputs.get(name, []))
        recent = self.pending_recent.get(name, [])
        if not incoming:
            return

        # 他要求定时提醒的话,先记下来。Saylo 以前会答应"半小时后问你",
        # 但它没有主动开口的能力,等于说空话。现在真的能做到了。
        if self.reminders is not None:
            if _rem.is_cancel_request(incoming):
                pending = self.reminders.pending()
                targets = _rem.cancellation_targets(incoming, pending)
                cancelled = [row for row in targets if self.reminders.cancel(row["id"])]
                if cancelled:
                    for row in cancelled:
                        log(f"[提醒已取消] {row.get('due')} | {row.get('what')}")
                    facts = (f"用户要求取消提醒；程序已经成功取消 {len(cancelled)} 个提醒。\n" +
                             "\n".join(f"- {row.get('due')}：{row.get('what')}" for row in cancelled))
                    segs = self._event_segments("提醒取消成功", facts, recent, max_segments=2)
                    if segs:
                        self._send_all(name, segs, count_round=True)
                    else:
                        log("[提醒] 已取消，但人格回复生成失败；不发送固定确认")
                    self._clear_pending(name)
                    return
                if not pending:
                    facts = "用户要求取消提醒；当前没有任何尚未完成的提醒。"
                    event = "没有可取消的提醒"
                else:
                    facts = (f"用户要求取消提醒；当前有 {len(pending)} 个待办，程序无法可靠判断目标。"
                             "需要请用户补充目标提醒的时间或内容，不能擅自取消。")
                    event = "取消目标不明确"
                segs = self._event_segments(event, facts, recent, max_segments=2)
                if segs and self._send_all(name, segs, count_round=True):
                    self._clear_pending(name)
                return
            got = _rem.parse(incoming)
            if got:
                rec = self.reminders.add(*got)
                mins = max(0, int((rec['due_ts'] - time.time() + 59) // 60))
                log(f"[倒计时已创建] 还剩约 {mins} 分钟 | {rec['due']} | {rec['what']}")
                facts = ("用户要求创建提醒；程序已经真实落盘。\n"
                         f"到期时间：{rec['due']}\n内容：{rec['what']}\n类型：{rec['kind']}")
                segs = self._event_segments("提醒创建成功", facts, recent, max_segments=2)
                if segs:
                    self._send_all(name, segs, count_round=True)
                else:
                    log("[提醒] 已创建，但人格回复生成失败；不发送固定确认")
                self._clear_pending(name)
                return
            if _rem.looks_like_request(incoming):
                log(f"[提醒未创建] 无法可靠解析时间或内容: {incoming[:80]}")
                facts = ("用户想创建提醒，但程序无法可靠解析明确的未来时间或提醒内容；"
                         "提醒尚未创建。需要请用户重新说清楚准确时间和内容，不要假装已经设置。")
                segs = self._event_segments("提醒请求需要澄清", facts, recent, max_segments=2)
                if segs and self._send_all(name, segs, count_round=True):
                    self._clear_pending(name)
                return

        ok, why = self._allowed()
        if not ok:
            log(f"[拦截] {why}")
            return

        if self._try_tell(name, incoming):
            self.pending_inputs.pop(name, None)
            self.pending_recent.pop(name, None)
            self.pending_updated.pop(name, None)
            return

        controls = self.runtime.controls()
        prefixes = getattr(C, "ASK_PREFIXES", ())
        hit = next((p for p in prefixes if incoming.startswith(p)), None)
        web_requested = wants_web_search(incoming)
        web_hit = web_requested and bool(controls.get("web_search", True))
        mode = None
        # 搜索、查档等是任务状态，不是新情绪；先取出原有底色，任务结束后恢复它。
        emotion_mode, emotional_context = self.emotion.preview(5)
        if web_hit:
            mode = 10
            log(f"[收到|联网检索] {incoming[:60]}")
            self.runtime.set("searching", mode=mode, label="正在联网寻找")
        elif web_requested:
            mode = 10
            log(f"[收到|联网检索已关闭] {incoming[:60]}")
            self.runtime.set("idle", mode=mode, label="联网检索已关闭")
        elif hit:
            log(f"[收到|查档] {incoming[:60]}")
            self.runtime.set("thinking", label="正在查找记忆")
        else:
            active_context = self.active_context.render()
            mode = self.ai.classify(incoming, recent=recent,
                                    active_context=active_context)
            emotion_mode, emotional_context = self.emotion.preview(mode)
            label = MODE_NAMES.get(mode, "未知")
            log(f"[收到|模式{mode}-{label}] {incoming[:60]}")
            self.runtime.set("thinking", mode=emotion_mode, label=f"正在想 · {label}")
        try:
            if web_hit:
                resolved_query = self.ai.resolve_web_query(incoming, recent=recent)
                if resolved_query:
                    log(f"[搜索问题已还原] {resolved_query[:120]}")
                    reply = self.ai.search_web(resolved_query, request_text=incoming)
                else:
                    reply = self._event_reply(
                        "联网搜索目标需要澄清",
                        "用户明确要求联网，但最新指令中的搜索对象依赖指代；程序结合近期"
                        "上下文后仍无法可靠还原完整问题。本次没有执行搜索，需要自然地请"
                        "用户补充搜索对象，不能假装已经查过。",
                        recent,
                    )
            elif web_requested:
                reply = self._event_reply(
                    "联网功能已关闭",
                    "用户提出了联网请求；当前桌面挂件的联网开关处于关闭状态。"
                    "可以通过挂件右键菜单重新开启，但程序没有执行本次检索。",
                    recent,
                )
            elif hit:
                q = incoming[len(hit):].strip() if hit in ("?", "？") else incoming
                reply = self.ai.ask(q)
            else:
                prog = (duration.scan(self.journal.load())
                        if self.journal else "")
                reply = self.ai.reply(
                    incoming, recent=recent, progress=prog, mode=mode,
                    recent_saylo=self._recent_saylo_texts(),
                    active_context=self.active_context.render(),
                    emotional_context=emotional_context,
                    thinking_enabled=bool(controls.get("thinking", True)),
                    reply_style=str(controls.get("reply_style", "cute")),
                )
        except Exception as e:
            log(f"[错误] 生成失败: {e}")
            if web_hit:
                reply = self._event_reply(
                    "联网检索失败",
                    "用户提出了联网请求，但本次网络或搜索服务调用失败，没有取得可靠结果。"
                    "不能编造查询结果。",
                    recent,
                )
                self.runtime.set("error", mode=mode, label="联网检索失败")
            else:
                self.runtime.set("error", mode=mode, label="回复生成失败")
                return

        quality_reason = getattr(self.ai, "last_quality_reason", "") if not web_hit else ""
        if quality_reason:
            log(f"[质量重写] 模式{mode}: {quality_reason}")

        if HOLD in reply:
            log("[转人工] 命中敏感内容,不自动回")
            self.pending_inputs.pop(name, None)
            self.pending_recent.pop(name, None)
            self.pending_updated.pop(name, None)
            return

        segs = split_segments(reply)
        if not segs:
            failures = self.pending_failures.get(name, 0) + 1
            self.pending_failures[name] = failures
            base = max(5, int(getattr(C, "REPLY_FAILURE_RETRY_BASE_SEC", 45)))
            cap = max(base, int(getattr(C, "REPLY_FAILURE_RETRY_MAX_SEC", 300)))
            delay = min(cap, base * (2 ** min(failures - 1, 8)))
            self.pending_retry_after[name] = time.time() + delay
            log(f"[生成失败] 模型没有返回可发送内容；保留消息，{delay} 秒后再试，"
                "不发送固定兜底")
            return
        mode_limit = MODE_SEGMENT_LIMITS.get(mode or 5, 3)
        if len(segs) > mode_limit:
            log(f"[拦截] 模式{mode}重写后仍有 {len(segs)} 条，超过上限 {mode_limit}，本轮不发送")
            return
        sent = self._send_all(
            name, segs, visual_mode=emotion_mode,
            abort_if=lambda: self._has_unprocessed_user_message(name),
        )
        # 如果一个字都还没发，新内容会在下轮和这一轮合并；若已发出前半段，
        # 旧回复无法撤回，只丢掉未发送的尾部，下一轮仅回答新内容。
        if sent or self._last_send_count:
            if mode not in {10, None}:
                self.emotion.commit(emotion_mode)
            self._clear_pending(name)
            self.runtime.set("happy" if mode == 9 else "idle",
                             mode=emotion_mode,
                             label=MODE_NAMES.get(mode or 5, "待机"))

    def _quiet(self, msg: str) -> None:
        """同样的原因只打一次日志,别把日志刷爆。

        加这个是因为出过一次“跑着跑着就不回了,日志里一行都没有”——
        早退分支全是静默 return,根本无从排查。
        """
        if getattr(self, "_last_quiet", None) != msg:
            self._last_quiet = msg
            log(f"[静默] {msg}")

    def _try_tell(self, name: str, incoming: str) -> bool:
        """他直接告诉 Saylo 一件事,比如“记住 我不吃香菜”。

        走这条路的内容**立刻写进记忆**,不等后台提取,也不过 LLM——
        他亲口说的话,不需要再让模型去"抽取"一遍,原样记住最准。
        """
        prefixes = getattr(C, "TELL_PREFIXES", ())
        hit = next((p for p in prefixes if incoming.startswith(p)), None)
        if not hit:
            return False
        fact = incoming[len(hit):].strip(" ：:，,。.")
        if len(fact) < 2:
            segs = self._event_segments(
                "记忆指令缺少内容", "用户要求记住一件事，但没有提供可记录的具体内容。",
                max_segments=1,
            )
            if segs:
                self._send_all(name, segs)
            return True
        mem = self.ai.mem
        if mem is None:
            segs = self._event_segments(
                "记忆功能不可用", "用户要求记住一件事，但当前记忆模块没有运行，事实未被保存。",
                max_segments=1,
            )
            if segs:
                self._send_all(name, segs)
            return True
        n = mem.add([{"kind": "偏好", "text": fact}])
        log(f"[记住] {fact[:60]}" + ("" if n else "  (已经记过了)"))
        state = "已经成功写入记忆" if n else "此前已经存在，没有重复写入"
        segs = self._event_segments(
            "记忆写入结果", f"用户要求记录的事实：{fact}\n程序结果：{state}。",
            max_segments=1,
        )
        if segs:
            self._send_all(name, segs)
        return True

    def _describe_image(self, msg: dict) -> str:
        """截图 + 看图,返回一句描述。看不了就返回空串,不能让聊天崩掉。

        描述结果按控件位置缓存:轮询每 6 秒一次,同一张图会被反复读到,
        不缓存的话每轮都要花钱重看一遍。
        """
        if not getattr(C, "VISION_ENABLED", True):
            return ""
        ctrl = msg.get("_ctrl")
        if ctrl is None:
            return ""
        try:
            r = ctrl.BoundingRectangle
            key = f"{msg['text']}|{r.width()}x{r.height()}"
        except Exception:
            key = msg.get("text", "")
        if key in self._img_cache:
            return self._img_cache[key]

        import vision
        path = C.STORE / "images" / f"{int(time.time())}.png"
        desc = ""
        try:
            if self.wx.capture(ctrl, path):
                desc = vision.describe(path)
                if desc:
                    log(f"[看图] {desc[:70]}")
                else:
                    log("[看图] 没看出内容")
            else:
                log("[看图] 截图失败(微信可能被挡住或消息滚出视口)")
        except Exception as e:
            log(f"[看图] 出错: {e}")
        finally:
            if not getattr(C, "VISION_KEEP_IMAGES", False):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                self.wx.restore_focus()       # 截图要抢前台,用完还回去
            except Exception:
                pass
        self._img_cache[key] = desc
        return desc

    def _log_reminder_status(self, force: bool = False) -> None:
        """在终端展示持久化倒计时；不另开终端，也不会阻塞正常回复。"""
        if self.reminders is None:
            return
        now = time.time()
        every = getattr(C, "REMINDER_STATUS_INTERVAL_SEC", 60)
        if not force and now - self._last_reminder_status < every:
            return
        pending = self.reminders.pending()
        self._last_reminder_status = now
        for r in pending:
            left = max(0, int(r["due_ts"] - now))
            mm, ss = divmod(left, 60)
            log(f"[倒计时] {mm:02d}:{ss:02d} 后提醒：{r['what']}")

    def _clear_pending(self, name: str) -> None:
        self.pending_inputs.pop(name, None)
        self.pending_recent.pop(name, None)
        self.pending_updated.pop(name, None)
        self.pending_failures.pop(name, None)
        self.pending_retry_after.pop(name, None)

    def _event_reply(self, event: str, facts: str,
                     recent: list[str] | None = None,
                     max_segments: int = 2) -> str:
        """所有程序事件也必须经 persona.py 生成，不提供库存回复。"""
        controls = self.runtime.controls()
        _, emotional_context = self.emotion.preview(5)
        try:
            return self.ai.event_reply(
                event, facts, recent=recent,
                recent_saylo=self._recent_saylo_texts(),
                emotional_context=emotional_context,
                reply_style=str(controls.get("reply_style", "cute")),
                max_segments=max_segments,
            )
        except Exception as exc:
            log(f"[事件回复生成失败] {event}: {exc}")
            return ""

    def _event_segments(self, event: str, facts: str,
                        recent: list[str] | None = None,
                        max_segments: int = 2) -> list[str]:
        return split_segments(self._event_reply(event, facts, recent, max_segments))

    def _last_activity_ts(self) -> float:
        if self.journal is None:
            return max(self.last_sent, max(self.pending_updated.values(), default=0.0))
        try:
            rows = self.journal.load()
            journal_ts = float(rows[-1].get("ts", 0)) if rows else 0.0
            return max(journal_ts, self.last_sent,
                       max(self.pending_updated.values(), default=0.0))
        except Exception:
            return max(self.last_sent, max(self.pending_updated.values(), default=0.0))

    def _recent_saylo_texts(self, now: float | None = None) -> list[str]:
        """最近半小时 Saylo 自己说过的话，只用于口癖检测，不注入提示词。"""
        if self.journal is None:
            return []
        now = now or time.time()
        try:
            rows = [r for r in self.journal.load()
                    if r.get("who") == "saylo" and
                    now - float(r.get("ts", 0)) <= 30 * 60]
            return [str(r.get("text") or "") for r in rows[-30:]]
        except Exception:
            return []

    def _check_proactive(self, name: str) -> bool:
        """低频主动问候；定时提醒和用户刚发消息时都不会触发。"""
        if self.pending_inputs:
            return False
        due = self.checkins.due(self._last_activity_ts())
        if not due:
            return False
        kind, text = due
        # 睡眠提醒和普通主动问候都只固定触发条件，具体措辞统一由 persona.py 生成。
        if not text:
            if kind != "sleep" and not getattr(C, "PROACTIVE_HISTORY_EXTERNAL_CONSENT", False):
                self._quiet("主动问候等待授权：尚未允许将近期 Saylo 对话发送给 DeepSeek")
                return False
            now = time.time()
            retry = getattr(C, "PROACTIVE_RETRY_SEC", 10 * 60)
            if now - self._last_proactive_attempt < retry:
                return False
            self._last_proactive_attempt = now
            recent = ""
            if kind != "sleep" and self.journal is not None:
                try:
                    recent = self.journal.render_tail(
                        getattr(C, "PROACTIVE_HISTORY_MESSAGES", 18)
                    )
                except Exception:
                    recent = ""
            try:
                text = self.ai.proactive(kind, recent=recent)
            except Exception as e:
                log(f"[主动问候] 动态生成失败，稍后重试: {e}")
                return False
            if text == SKIP_PROACTIVE:
                if not self.dry:
                    self.checkins.mark_sent(kind, status="skipped_forbidden_prompt")
                log(f"[主动问候跳过] {kind}: 连续两次生成泛问句")
                return False
        segs = split_segments(text)
        if not segs:
            log("[主动问候] 模型返回空内容，稍后重试")
            return False
        if self._send_all(name, segs, count_round=False):
            if self.dry:
                log(f"[模拟主动问候] {kind}: {text.replace(chr(10), ' / ')}")
            else:
                self.checkins.mark_sent(kind)
                log(f"[主动问候] {kind}: {text.replace(chr(10), ' / ')}")
            return True
        return False

    def _check_reminders(self) -> bool:
        """到点的提醒主动发出去。发了返回 True。

        这是**唯一一处 Saylo 可以主动开口**的地方。它不受"连续几轮没等到
        用户消息"那道闸限制——定时提醒本来就发生在没人说话的时候——
        但仍然只发一次,发完就标记完成。
        """
        if self.reminders is None:
            return False
        due = self.reminders.due()
        if not due:
            return False
        if not C.REPLY_WHITELIST:
            log("[提醒] 白名单为空，保留待办，暂不发送")
            return False
        name = C.REPLY_WHITELIST[0]
        # 主动提醒不依赖当前刚好打开的聊天。发送前主动切到受硬闸保护的白名单会话。
        if self.wx.current_chat() != name:
            if not self.wx.activate() or not self.wx.open_chat(name):
                log(f"[提醒] 到点了，但无法打开“{name}”；保留待办下轮重试")
                return False
        for r in due:
            log(f"[提醒到点] {r['what']}")
            facts = ("程序确认一个已创建的提醒现在到期。\n"
                     f"提醒内容：{r.get('what')}\n类型：{r.get('kind')}\n"
                     "必须传达这项提醒本身；不要添加新的提醒承诺或虚构用户正在做什么。")
            segs = self._event_segments("提醒到期", facts, max_segments=2)
            if not segs:
                log(f"[提醒] 人格回复生成失败，保留待办下轮重试：{r['what']}")
                continue
            if self._send_all(name, segs, count_round=False):
                if self.dry:
                    log(f"[模拟提醒] 未标记完成：{r['what']}")
                else:
                    self.reminders.mark_done(r["id"])
                    log(f"[提醒已送达] {r['what']}")
            else:
                log(f"[提醒] 发送未确认，保留待办下轮重试：{r['what']}")
        return True

    def _remember(self, who: str, text: str) -> None:
        """把一句话记进流水,并叫醒后台提取线程。

        写文件 + set 一个 Event,微秒级,不会拖慢回复。真正花时间的 LLM 提取
        在 Extractor 线程里做。
        """
        if self.journal is None:
            return
        try:
            self.journal.append(who, text)
            if self.extractor is not None:
                self.extractor.nudge()
        except Exception:
            pass          # 记忆写失败也不能影响聊天
    def _send_all(self, name: str, segments: list[str],
                  count_round: bool = True, abort_if=None,
                  visual_mode: int | None = None) -> bool:
        """发送。只在这里抢一次焦点,发完还回去。

        ``abort_if`` 用于普通聊天：用户在模型思考或逐条发送时继续说话，
        就放弃尚未发出的旧内容，下一轮把消息合并后重新回答。
        """
        self._last_send_count = 0
        self.runtime.set("sending", mode=visual_mode, label="正在发送")
        segments = [normalize_segment(s) for s in segments]
        segments = [s for s in segments if s]
        if count_round:
            segments = self._limit_expressions(segments)
        max_segs = getattr(C, "MAX_SEGMENTS_PER_REPLY", 6)
        if len(segments) > max_segs:             # 单轮拆太多条会刷屏
            log(f"[截断] 这轮拆成了 {len(segments)} 条,只发前 {max_segs} 条")
            segments = segments[:max_segs]
        def abort() -> bool:
            if abort_if is not None and abort_if():
                log("[合并消息] 收到新消息，取消尚未发出的旧回复")
                return True
            return False

        def count_first_send() -> None:
            # 熔断按**轮次**计,不是按消息条数；只有真的至少发出一条才计入。
            if count_round and self._last_send_count == 0:
                self.sent_times.append(time.time())
                self.since_user += 1

        if self.dry:
            for seg in segments:
                if abort():
                    return False
                d = self._typing_delay(seg)
                self.wx.send(seg, dry_run=True)
                count_first_send()
                self._last_send_count += 1
                self.sent_log.append(seg)
                log(f"[模拟|应等{d:.1f}s] {seg[:60]}")
                self._remember("saylo", seg)
            return True

        if abort():
            return False
        if not self.wx.activate():
            log("[失败] 抢不到微信前台,这次不发了(消息没丢,下轮会重试)")
            return False
        sent_all = True
        try:
            delays = [self._typing_delay(x) for x in segments]
            if getattr(C, "HUMAN_TYPING_ENABLED", False):
                think_max = getattr(C, "HUMAN_TYPING_THINK_MAX_SEC", 2.2)
                delays = [min(d, think_max) for d in delays]
            budget = getattr(C, "TYPING_TOTAL_MAX_SEC", 22.0)
            if sum(delays) > budget:             # 条数多时按比例压缩,别让他等太久
                k = budget / sum(delays)
                delays = [max(0.6, d * k) for d in delays]
            for seg, d in zip(segments, delays):
                time.sleep(d)                    # 按长度"打字",秒回一大段很假
                if abort():
                    sent_all = False
                    break
                if not self.wx.send(seg, dry_run=False, abort_if=abort):
                    sent_all = False
                    log(f"[失败] 微信未确认发出：{seg[:60]}")
                    break
                count_first_send()
                self._last_send_count += 1
                self.sent_log.append(seg)        # 记下来,下轮才知道这条是自己发的
                # 刻意不把 seg 追加进 seen_msgs:那等于猜微信里的消息顺序。
                # 用户在生成期间又发一条时,真实顺序是 [他的新消息, 自己1, 自己2],
                # 猜的顺序对不上,他那条就被吃掉了。让下一轮真实读取来决定顺序,
                # 自己发的由 _classify_sides 按 sent_log 认出来。
                log(f"[已发|等了{d:.1f}s] {seg[:60]}")
                self._remember("saylo", seg)
                self.last_sent = time.time()
        finally:
            if getattr(C, "RESTORE_FOCUS", True):
                self.wx.restore_focus()
        return sent_all

    def _limit_expressions(self, segments: list[str]) -> list[str]:
        """限制表情、emoji 与颜文字频率；人格提示负责选择，这里负责不刷屏。"""
        self._reply_turn += 1
        tokens = []
        for group in getattr(C, "EMOJI_PALETTE", {}).values():
            tokens.extend(group)
        tokens.extend(getattr(C, "KAOMOJI_PALETTE", ()))
        # 长的先替换，避免短 token 与长 token 重叠时留下残片。
        tokens = sorted(set(tokens), key=len, reverse=True)
        found = []
        for i, seg in enumerate(segments):
            for token in tokens:
                if token in seg:
                    found.append((i, token))
        if not found:
            return segments
        cooldown = getattr(C, "EMOJI_COOLDOWN_TURNS", 3)
        permitted = self._reply_turn - self._last_expression_turn > cooldown
        keep = found[0] if permitted else None
        out = list(segments)
        for i, token in found:
            if keep == (i, token):
                continue
            out[i] = out[i].replace(token, "")
        out = [s.strip() for s in out if s.strip()]
        if keep:
            self._last_expression_turn = self._reply_turn
        return out

    # ------------------------------------------------------------ 主循环

    def run(self, once: bool = False, interval: float | None = None,
            stop_if=None) -> None:
        mode = "真发消息" if not self.dry else "DRY_RUN(只打印,不会真发出去)"
        log(f"启动 | 模式={mode} | 白名单={C.REPLY_WHITELIST or '(空,谁都不回)'}")
        self.runtime.set("idle", label="Saylo 已就绪")
        self.runtime.start_heartbeat()
        if self.dry:
            log("提示:现在是演习模式,只会在这里打印回复。要真发请加 --live")
        self._log_reminder_status(force=True)
        self._warmup_if_safe()
        if not self.dry and not C.REPLY_WHITELIST:
            log("白名单是空的,不会回任何人。先在 config.py 里填 REPLY_WHITELIST。")
        try:
            while True:
                if stop_if is not None and stop_if():
                    log("收到停止请求，Saylo 正在安全退出")
                    break
                try:
                    controls = self.runtime.controls()
                    if controls.get("paused"):
                        self.runtime.set("paused", label="已暂停")
                    else:
                        self.runtime.set("idle", label="Saylo 已就绪")
                        self.tick()
                except Exception as e:
                    log(f"[错误] 轮询异常: {e}")
                    self.runtime.set("error", label="运行异常")
                self._warmup_if_safe()
                if once:
                    break
                # 下一个待办到点时立刻醒来；平时仍按普通轮询间隔处理新消息。
                wait = interval or getattr(C, 'POLL_INTERVAL', 6.0)
                if self.reminders is not None:
                    due = self.reminders.next_due()
                    if due is not None:
                        wait = min(wait, max(0.05, due - time.time()))
                # 连发消息不必等完整个轮询周期，安静窗口一到就立刻处理。
                pending_in = self._pending_ready_in()
                if pending_in is not None:
                    wait = min(wait, max(0.05, pending_in))
                # 隐藏启动器每 0.5 秒检查停止标记，但仍按正常间隔轮询微信。
                # 旧实现把整个轮询间隔压成 0.5 秒，微信离线时会刷爆日志。
                if stop_if is None:
                    time.sleep(wait)
                else:
                    deadline = time.monotonic() + wait
                    while True:
                        if stop_if():
                            log("收到停止请求，Saylo 正在安全退出")
                            return
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(0.5, remaining))
        except KeyboardInterrupt:
            log("已停止")
        finally:
            self.runtime.stopped()
            self.runtime.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--live", action="store_true", help="真的发消息")
    a = ap.parse_args()
    if a.live:
        print("!! --live:会真的往微信发消息 !!")
        if input("   确认请输入 yes: ").strip().lower() != "yes":
            print("已取消"); return
    Agent(live=a.live).run(once=a.once)


if __name__ == "__main__":
    main()
