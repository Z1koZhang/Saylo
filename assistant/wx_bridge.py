# -*- coding: utf-8 -*-
"""微信 4.1 UI 自动化桥接层。

基于实测的 mmui::* 控件树(微信 4.1.13.65):
    mmui::MainWindow
      mmui::ChatSessionList > mmui::XTableView > mmui::ChatSessionCell
          Name = "联系人\n最后一条消息\n时间"
      mmui::ChatMessagePage
          mmui::RecyclerListView > mmui::ChatTextItemView   Name = 消息正文
          mmui::ChatInputField (Edit)  Name = "<当前会话名> 按住 Ctrl + Win ..."

⚠️ 已实测确认的限制:消息条目在 UIA 里**没有发送方字段**
   (Value / Description / Help / ItemStatus 全空)。
   所以谁发的只能靠**气泡左右位置**判断,见 bubble_side()。
"""
from __future__ import annotations

import ctypes
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

import uiautomation as auto

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
from wx_accessibility import activate_qt_accessibility

MAIN_CLASS = "mmui::MainWindow"
SHELL_CLASS = "Qt51514QWindowIcon"

# ---------------------------------------------------------------- 前台焦点

_u32 = ctypes.windll.user32
_SW_RESTORE = 9
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_KEYBOARD = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT),
                ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _unicode_key_inputs(char: str) -> list[_INPUT]:
    """把一个 Unicode 字符变成 SendInput 需要的 UTF-16 按下/抬起事件。"""
    units = [int.from_bytes(char.encode("utf-16-le")[i:i + 2], "little")
             for i in range(0, len(char.encode("utf-16-le")), 2)]
    down = [_INPUT(type=_INPUT_KEYBOARD,
                   ki=_KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE, 0, 0))
            for unit in units]
    up = [_INPUT(type=_INPUT_KEYBOARD,
                 ki=_KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, 0))
          for unit in units]
    return down + up


def _human_typing_interval(char: str, remaining_chars: int) -> float:
    """自然但有上限的逐字间隔；长消息会自动加速。"""
    lo = float(getattr(C, "HUMAN_TYPING_MIN_INTERVAL_SEC", 0.05))
    hi = float(getattr(C, "HUMAN_TYPING_MAX_INTERVAL_SEC", 0.14))
    interval = random.uniform(min(lo, hi), max(lo, hi))
    max_duration = float(getattr(C, "HUMAN_TYPING_MAX_DURATION_SEC", 8.0))
    if remaining_chars > 0:
        interval = min(interval, max_duration / remaining_chars)
    if char in "，。！？；：,.!?;:\n":
        p_lo, p_hi = getattr(C, "HUMAN_TYPING_PUNCTUATION_PAUSE_SEC", (0.18, 0.42))
        interval += random.uniform(float(p_lo), float(p_hi))
    return interval


def _force_foreground(hwnd: int) -> bool:
    """把窗口抢到前台。

    Windows 默认不允许后台进程调用 SetForegroundWindow(防止程序乱抢焦点),
    直接调用会静默失败。标准绕法是先用 AttachThreadInput 把自己的线程
    附加到当前前台窗口的线程上,借用它的“前台权限”,调完再分离。
    """
    try:
        if _u32.IsIconic(hwnd):
            _u32.ShowWindow(hwnd, _SW_RESTORE)
        fg = _u32.GetForegroundWindow()
        if fg == hwnd:
            return True
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        tgt = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(tgt) and bool(_u32.AttachThreadInput(cur, tgt, True))
        try:
            _u32.BringWindowToTop(hwnd)
            _u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _u32.AttachThreadInput(cur, tgt, False)
        return _u32.GetForegroundWindow() == hwnd
    except Exception:
        return False




def _find_by_class(root, cls: str, maxd: int = 24, budget: int = 20000):
    stack = [(root, 0)]
    b = budget
    while stack:
        c, d = stack.pop()
        if b <= 0 or d > maxd:
            continue
        for ch in c.GetChildren():
            b -= 1
            if (ch.ClassName or "") == cls:
                return ch
            stack.append((ch, d + 1))
    return None


class WeChatUI:
    def __init__(self):
        self.win = None
        self._send_key: str | None = None      # 试出来的发送键,试一次记一次
        self._last_accessibility_wake = 0.0
        self._last_accessibility_detail = ""
        shell = self._scan_top_windows()
        if self.win is None and shell is not None:
            self._materialize_from_shell(shell)
        if self.win is None:
            if shell is not None:
                raise RuntimeError(
                    "微信进程已启动，但没有向 Windows UI Automation 暴露聊天控件树；"
                    f"自动恢复失败：{self._last_accessibility_detail or '原因未知'}"
                )
            raise RuntimeError("没找到微信聊天主窗口。确认微信已启动、已登录、没最小化到托盘。")

    def _scan_top_windows(self):
        """更新主窗口引用，并返回可能存在的 Qt 空壳。"""
        shell = None
        for w in auto.GetRootControl().GetChildren():
            try:
                cls = w.ClassName or ""
                if cls == MAIN_CLASS:
                    self.win = w
                    return None
                if cls == SHELL_CLASS and (w.Name or "").strip() == "Weixin":
                    shell = w
            except Exception:
                continue
        return shell

    def _mmui_visible(self) -> bool:
        self.win = None
        self._scan_top_windows()
        return self.win is not None

    def _materialize_from_shell(self, shell) -> bool:
        """冷启动自愈；同一进程失败后 30 秒内不重复写内存。"""
        now = time.monotonic()
        if now - self._last_accessibility_wake < 30.0:
            return False
        self._last_accessibility_wake = now
        try:
            hwnd = int(shell.NativeWindowHandle)
            result = activate_qt_accessibility(hwnd, self._mmui_visible)
            self._last_accessibility_detail = result.detail
            return result.ok
        except Exception as exc:
            self._last_accessibility_detail = f"{type(exc).__name__}: {exc}"
            return False

    # ---------------------------------------------------------------- 基础

    def _refresh(self) -> bool:
        """重新抓一次主窗口引用。

        微信重绘或窗口重建后,旧的 UIA 元素引用会失效,再访问就抛 COMError
        (“事件无法调用任何订户”)。这时候必须重新从根节点找一遍。
        """
        self.win = None
        shell = self._scan_top_windows()
        if self.win is not None:
            return True
        return shell is not None and self._materialize_from_shell(shell)

    def readable(self) -> bool:
        """能不能读到控件树。**不抢焦点** —— 读取不需要窗口在前台。

        (早先以为必须前台才能读,那是误判:当时窗口正好被最小化了。
         实测焦点移走 12 秒后,消息列表照样读得到。)

        真正读不到的情况只有两种:窗口最小化,或元素引用失效。
        """
        for attempt in range(2):
            try:
                if _find_by_class(self.win, "mmui::MainTabBar") is not None:
                    return True
            except Exception:
                pass
            if attempt == 0:
                self._refresh()          # 引用可能失效了,重新抓一次
        return False

    def restore_if_minimized(self) -> bool:
        """只在窗口真的最小化时才把它恢复出来。

        恢复窗口会在屏幕上弹一下,所以必须先判断,不能无条件调用——
        无条件调用正是“微信反复最小化又弹出”的原因。
        """
        try:
            hwnd = self.win.NativeWindowHandle
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, _SW_RESTORE)
                time.sleep(0.8)
                return True
        except Exception:
            pass
        return False

    def activate(self, wait: float = 0.6, retries: int = 3) -> bool:
        """把微信抢到前台。**只在要发消息时才调用** —— 发送必须有键盘焦点。

        Windows 会阻止后台进程抢前台焦点(SetForegroundWindow 静默失败),
        所以走 _force_foreground 的 AttachThreadInput 绕法。
        """
        self._prev_hwnd = _u32.GetForegroundWindow()
        for i in range(retries):
            try:
                hwnd = self.win.NativeWindowHandle
            except Exception:
                if not self._refresh():
                    return False
                continue
            _force_foreground(hwnd)
            for act in ("SetActive", "SetFocus"):
                fn = getattr(self.win, act, None)
                if fn is not None:
                    try:
                        fn()
                    except Exception:
                        pass
            time.sleep(wait * (1 + i * 0.5))
            if _u32.GetForegroundWindow() == hwnd:
                return True
        return False

    def restore_focus(self) -> None:
        """把焦点还给刚才那个窗口。发完消息就还,别占着用户的前台。"""
        prev = getattr(self, "_prev_hwnd", 0)
        try:
            if prev and prev != self.win.NativeWindowHandle:
                _force_foreground(prev)
        except Exception:
            pass

    def _node(self, cls: str, required: bool = True):
        n = _find_by_class(self.win, cls)
        if n is None and required:
            raise RuntimeError(
                f"控件树里没找到 {cls}。"
                "常见原因:还没打开任何聊天(输入框/消息列表只在会话打开后才存在)、"
                "窗口最小化到托盘、或微信版本变了。")
        return n

    # ---------------------------------------------------------------- 会话

    def sessions(self) -> list[dict]:
        """当前可见的会话列表。列表是虚拟化的,只返回视口内的。"""
        tv = self._node("mmui::XTableView")
        out = []
        for cell in tv.GetChildren():
            if "ChatSessionCell" not in (cell.ClassName or ""):
                continue
            parts = [p.strip() for p in (cell.Name or "").split("\n")]
            parts += [""] * (3 - len(parts))
            r = cell.BoundingRectangle
            out.append({
                "name": parts[0], "preview": parts[1], "time": parts[2],
                "rect": (r.left, r.top, r.width(), r.height()), "_ctrl": cell,
            })
        return out

    def current_chat(self) -> str:
        """输入框的 Name 形如 "Ann 按住 Ctrl + Win 使用语音输入文字",前缀就是会话名。"""
        node = self._node("mmui::ChatInputField", required=False)
        if node is None:
            return ""          # 还没打开任何会话
        return re.sub(r"\s*按住\s*Ctrl.*$", "", node.Name or "").strip()

    def open_chat(self, name: str, timeout: float = 8.0) -> bool:
        """打开指定会话。

        坑:会话列表是虚拟化的,单元格控件会被回收复用,所以每次都要重新取,
        不能缓存 _ctrl。另外点击后微信切换有延迟,必须轮询等待而不是睡一下就判断。
        """
        if self.current_chat() == name:          # 已经开着,不用点
            return True

        for s in self.sessions():                # 现取现用,不用旧引用
            if s["name"] == name:
                try:
                    s["_ctrl"].Click(simulateMove=False)
                except Exception:
                    break
                if self._wait_chat(name, timeout):
                    return True
                break

        box = self._node("mmui::XValidatorTextEdit", required=False)
        if box is None:
            return False
        try:
            box.Click(simulateMove=False)
            time.sleep(0.2)
            box.SendKeys("{Ctrl}a{Delete}", waitTime=0.1)
            box.SendKeys(name, waitTime=0.3)
            time.sleep(0.8)
            box.SendKeys("{Enter}", waitTime=0.3)
        except Exception:
            return False
        return self._wait_chat(name, timeout)

    def _wait_chat(self, name: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.current_chat() == name:
                return True
            time.sleep(0.25)
        return False

    # ---------------------------------------------------------------- 消息

    # 图片类消息。**主要靠 Name 判,不是 ClassName** ——
    # 实测图片走的是 mmui::ChatBubbleReferItemView,而这个控件类同时还用于
    # 动画表情和引用消息,光看 ClassName 区分不出来。Name 才直接写着“图片”。
    # 新版微信也会直接暴露英文 Name="Image"；类名仍是通用的
    # ChatBubbleReferItemView，不能只靠类名判断。
    IMAGE_NAMES = ("图片", "Image", "动画表情", "表情", "视频", "照片")
    IMAGE_CLASSES = ("Image", "Picture", "Emoticon", "Sticker", "Video")

    @classmethod
    def _kind_of(cls, class_name: str, name: str = "") -> str:
        c, n = class_name or "", (name or "").strip()
        if "SystemInfo" in c:
            return "system"
        if n in cls.IMAGE_NAMES or any(k.lower() in c.lower() for k in cls.IMAGE_CLASSES):
            return "image"
        if "TextItem" in c:
            return "text"
        if c.endswith("ChatItemView"):
            return "time"
        return "other"

    def capture(self, ctrl, out_path) -> bool:
        """把一条消息截成 PNG。

        **ToBitmap 抓的是屏幕像素,不是控件自己的渲染。** 微信被别的窗口挡住时,
        截出来的是挡在上面的那个窗口(实测截到过终端里的日志)。所以必须先把
        微信弄到前台,并确认消息在可视区域内。
        """
        from pathlib import Path as _P
        out = _P(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not self.activate():
            return False
        try:
            ctrl.ScrollIntoView()                 # 滚出视口的截不到
        except Exception:
            pass
        time.sleep(0.35)                          # 等重绘
        try:
            r = ctrl.BoundingRectangle
            if r.width() < 8 or r.height() < 8:
                return False
            bmp = ctrl.ToBitmap()
            if bmp is None:
                return False
            ok = bmp.ToFile(str(out))
            return bool(ok) and out.exists() and out.stat().st_size > 0
        except Exception:
            return False


    def messages(self, limit: int = 15) -> list[dict]:
        """读当前会话最后 limit 条。side 由气泡位置推断,可能为 None。"""
        lst = self._node("mmui::RecyclerListView", required=False)
        if lst is None:
            return []
        lr = lst.BoundingRectangle
        mid = (lr.left + lr.right) / 2
        out = []
        for it in lst.GetChildren()[-limit:]:
            cls = it.ClassName or ""
            text = (it.Name or "").strip()
            if not text:
                continue
            kind = self._kind_of(cls, text)
            rec = {"class": cls, "type": kind, "text": text,
                   "side": self.bubble_side(it, mid) if kind == "text" else None}
            if kind == "image":
                rec["_ctrl"] = it        # 截图要用,保留控件引用
            out.append(rec)
        return out

    @staticmethod
    def bubble_side(item, mid: float) -> str | None:
        """判断这条消息是自己发的还是对方发的。

        UIA 不给发送方(实测 Value/Description/Help 全空),只能看像素。
        两个判据,深浅色主题都成立:

        1. **绿色气泡 = 自己**。微信自己发的气泡恒为绿色:
           深色模式 RGB(53,210,141),浅色模式 RGB(149,236,105)。
           共同特征是绿分量显著高于红蓝。
        2. 没有绿色时,取“气泡填充色”(除背景外占面积最大的颜色)的
           **质心横坐标**,偏右为自己,偏左为对方。

        性能:只取中间一条横带,并且用 GetPixelColorsOfRect 一次批量读回来。
        早先逐像素 GetPixelColor 的写法,单条消息就要几千次 Win32 调用。
        """
        try:
            r = item.BoundingRectangle
            w, h = r.width(), r.height()
            if w <= 8 or h <= 8:
                return None
            bmp = item.ToBitmap()
            if bmp is None:
                return None

            band = min(12, max(4, h // 4))
            y0 = max(0, h // 2 - band // 2)
            band = min(band, h - y0)
            colors = bmp.GetPixelColorsOfRect(0, y0, w, band)
            if not colors:
                return None

            hist: Counter = Counter()
            xsum: dict[int, int] = {}
            xcnt: dict[int, int] = {}
            green = 0
            for i in range(0, len(colors), 2):
                c = colors[i]
                x = i % w
                hist[c] += 1
                xsum[c] = xsum.get(c, 0) + x
                xcnt[c] = xcnt.get(c, 0) + 1
                cr, cg, cb = (c >> 16) & 255, (c >> 8) & 255, c & 255
                if cg > 150 and cg - cr > 60 and cg - cb > 40:
                    green += 1

            if green >= 25:                       # 判据 1:绿气泡
                return "self"

            ranked = hist.most_common(4)
            if len(ranked) < 2:
                return None
            bg = ranked[0][0]
            bubble = next((c for c, n in ranked[1:]
                           if n >= 40 and abs(((c >> 16) & 255) - ((bg >> 16) & 255)) > 6), None)
            if bubble is None:
                return None
            return "self" if (xsum[bubble] / xcnt[bubble]) > w / 2 else "other"
        except Exception:
            return None

    # ---------------------------------------------------------------- 发送

    def _input_value(self) -> str:
        try:
            return self._node("mmui::ChatInputField").GetValuePattern().Value or ""
        except Exception:
            return ""

    def clear_input(self) -> bool:
        """清空输入框,并确认真的空了。

        发多条时必须每条前都清一次。之前多条被合并成一条(中间变成换行)
        就是因为没确认清空,第二段直接摞在第一段后面了。
        """
        field = self._node("mmui::ChatInputField")
        try:
            vp = field.GetValuePattern()
            if not (vp.Value or "").strip():
                return True
            vp.SetValue("")
            time.sleep(0.1)
            if not (vp.Value or "").strip():
                return True
        except Exception:
            pass
        try:                                   # 兜底:键盘全选删除
            field.SetFocus()
            time.sleep(0.1)
            field.SendKeys("{Ctrl}a{Delete}", waitTime=0.15)
        except Exception:
            return False
        return not self._input_value().strip()

    def _set_input_humanly(self, field, text: str, abort_if=None) -> bool:
        """用 Unicode 键盘事件逐字输入，并周期性核对微信里的实际内容。"""
        field.SetFocus()
        time.sleep(0.12)
        verify_every = max(1, int(getattr(C, "HUMAN_TYPING_VERIFY_EVERY_CHARS", 6)))
        typed = ""
        started = time.monotonic()
        for index, char in enumerate(text, 1):
            if abort_if is not None and abort_if():
                self.clear_input()
                return False
            if char == "\n":
                field.SendKeys("{Shift}{Enter}", waitTime=0.01)
            else:
                inputs = _unicode_key_inputs(char)
                array = (_INPUT * len(inputs))(*inputs)
                sent = _u32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(_INPUT))
                if sent != len(inputs):
                    self.clear_input()
                    return False
            typed += char
            elapsed = time.monotonic() - started
            remaining = max(1, len(text) - index)
            delay = _human_typing_interval(char, remaining)
            max_duration = float(getattr(C, "HUMAN_TYPING_MAX_DURATION_SEC", 8.0))
            if elapsed < max_duration:
                time.sleep(min(delay, max_duration - elapsed))
            if index % verify_every == 0 or index == len(text):
                actual = self._input_value().replace("\r\n", "\n")
                if actual != typed:
                    self.clear_input()
                    return False
        return True

    def set_input(self, text: str, abort_if=None) -> bool:
        """把文字放进输入框；实验开关打开时逐字输入，否则一次性赋值。

        逐字模式使用 Win32 Unicode SendInput，绕开中文输入法重写；任一步校验
        失败都会清空并返回，不会把半截草稿误发出去。
        """
        if not self.clear_input():
            return False
        field = self._node("mmui::ChatInputField")
        if getattr(C, "HUMAN_TYPING_ENABLED", False):
            return self._set_input_humanly(field, text, abort_if=abort_if)
        try:
            vp = field.GetValuePattern()
            if not vp.IsReadOnly:
                vp.SetValue(text)
                time.sleep(0.15)
                if (vp.Value or "").strip() == text.strip():
                    return True
        except Exception:
            pass
        try:                                   # 兜底:敲键盘。空行只发换行
            field.SetFocus()
            time.sleep(0.15)
            for i, line in enumerate(text.split("\n")):
                if i:
                    field.SendKeys("{Shift}{Enter}", waitTime=0.05)
                if line:
                    field.SendKeys(line, waitTime=0.02)
            return True
        except Exception:
            return False

    # 微信的“发送消息”快捷键可以是 Enter 或 Ctrl+Enter。不去猜,第一次发的时候
    # 试出来,之后就一直用对的那个。
    SEND_KEYS = ("{Enter}", "{Ctrl}{Enter}")

    def _press_send(self, key: str) -> bool:
        """按一次发送键,返回“输入框是否已清空”——空了才说明真发出去了。"""
        field = self._node("mmui::ChatInputField")
        field.SetFocus()
        time.sleep(0.1)
        field.SendKeys(key, waitTime=0.25)
        time.sleep(0.3)
        return not self._input_value().strip()

    def send(self, text: str, dry_run: bool | None = None, abort_if=None) -> bool:
        """发一条消息。

        发完必须确认输入框空了。**没空不能直接清掉了事** —— 那等于把这条
        消息扔了。没空说明这个键不是发送键(微信设成了 Ctrl+Enter),
        所以换另一个键重发,试出来之后记住,后面不用再试。
        """
        dry = C.DRY_RUN if dry_run is None else dry_run
        text = (text or "").strip()
        if not text:
            return False
        if dry:
            print(f"  [DRY_RUN] 本会发给“{self.current_chat()}”: {text!r}")
            return True
        if abort_if is not None and abort_if():
            return False
        if not self.set_input(text, abort_if=abort_if):
            return False
        if abort_if is not None and abort_if():
            self.clear_input()
            return False

        # 已知的键排前面,省得每条都试两次
        keys = list(self.SEND_KEYS)
        if self._send_key in keys:
            keys.remove(self._send_key)
            keys.insert(0, self._send_key)

        for i, key in enumerate(keys):
            if self._press_send(key):
                if self._send_key != key:
                    self._send_key = key
                    print(f"  [微信的发送键是 {key}]")
                return True
            if i + 1 < len(keys):            # 这个键没发出去,内容还在框里,换下一个
                continue

        left = self._input_value()
        self.clear_input()                   # 都试过了还在,清掉免得污染下一条
        raise RuntimeError(
            f"Enter 和 Ctrl+Enter 都没能把消息发出去,输入框里还留着 {left[:30]!r}。"
            "可能是微信窗口没拿到键盘焦点,或者发送键被改成了别的组合。")






if __name__ == "__main__":
    wx = WeChatUI()
    wx.activate()
    print(f"当前会话: {wx.current_chat()!r}\n")
    print("=== 会话列表 ===")
    for s in wx.sessions():
        print(f"  {s['name']:<20} | {s['preview'][:28]:<30} | {s['time']}")
    print("\n=== 当前会话消息 ===")
    for m in wx.messages():
        tag = {"self": "我   ", "other": "对方 ", None: "?    "}[m["side"]] if m["type"] == "text" else f"[{m['type']}]"
        print(f"  {tag} {m['text'][:60]}")
