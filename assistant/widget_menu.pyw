# -*- coding: utf-8 -*-
"""Saylo v3.2 高 DPI 自绘右键菜单；失败时主挂件仍可回退到系统菜单。"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from runtime_state import read_control, read_status, request_stop, update_control

TRANSPARENT = "#010203"
PANEL = "#12151e"
PANEL_HOVER = "#202638"
TEXT = "#f1f4ff"
MUTED = "#929aaf"
ACCENT = "#7b8cff"
BORDER = "#343c52"
WM_CLOSE = 0x0010


def _enable_dpi() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _rounded(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class Menu:
    def __init__(self, parent_hwnd: int, version: str):
        _enable_dpi()
        ctypes.windll.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                                      wintypes.WPARAM, wintypes.LPARAM]
        ctypes.windll.user32.PostMessageW.restype = wintypes.BOOL
        self.parent_hwnd = parent_hwnd
        self.version = version
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(f"Saylo v{version} menu")
        dpi = ctypes.windll.user32.GetDpiForSystem()
        self.scale = max(1.0, min(2.5, dpi / 96.0))
        self.width = round(286 * self.scale)
        self.height = round(348 * self.scale)
        self.canvas = tk.Canvas(self.root, width=self.width, height=self.height,
                                bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.attributes("-alpha", 0.985)
        self.hover: str | None = None
        self.hits: list[tuple[float, float, float, float, str]] = []
        self._place()
        self.draw()
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))
        self.canvas.bind("<Button-1>", self._click)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.bind("<FocusOut>", self._focus_out)
        self.root.deiconify()
        self.root.lift()
        self.root.after(80, self.root.focus_force)

    def _place(self) -> None:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        point = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        x = min(max(8, point.x), max(8, screen_w - self.width - 8))
        y = min(max(8, point.y), max(8, screen_h - self.height - 8))
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

    def _font(self, pixels: int, weight: str = "normal"):
        return ("Microsoft YaHei UI", -round(pixels * self.scale), weight)

    def _focus_out(self, _event) -> None:
        self.root.after(100, lambda: self.root.destroy()
                        if self.root.focus_displayof() is None else None)

    def _set_hover(self, value: str | None) -> None:
        if value != self.hover:
            self.hover = value
            self.draw()

    def _motion(self, event) -> None:
        hit = next((name for x1, y1, x2, y2, name in self.hits
                    if x1 <= event.x <= x2 and y1 <= event.y <= y2), None)
        self._set_hover(hit)

    def _add_hit(self, x1, y1, x2, y2, name: str) -> None:
        self.hits.append((x1, y1, x2, y2, name))

    def _row(self, y: float, label: str, action: str, checked: bool | None = None,
             danger: bool = False) -> None:
        s = self.scale
        x1, x2, h = 10 * s, self.width - 10 * s, 36 * s
        if self.hover == action:
            _rounded(self.canvas, x1, y, x2, y + h, 10 * s, fill=PANEL_HOVER, outline="")
        color = "#ff7788" if danger else TEXT
        self.canvas.create_text(24 * s, y + h / 2, text=label, anchor="w",
                                fill=color, font=self._font(13, "normal"))
        if checked is not None:
            tx1, ty1, tx2, ty2 = self.width - 55 * s, y + 9 * s, self.width - 22 * s, y + 27 * s
            _rounded(self.canvas, tx1, ty1, tx2, ty2, 9 * s,
                     fill=ACCENT if checked else "#34394a", outline="")
            cx = (tx2 - 9 * s) if checked else (tx1 + 9 * s)
            self.canvas.create_oval(cx - 6 * s, y + 12 * s, cx + 6 * s, y + 24 * s,
                                    fill="#ffffff", outline="")
        self._add_hit(x1, y, x2, y + h, action)

    def draw(self) -> None:
        self.canvas.delete("all")
        self.hits.clear()
        s = self.scale
        _rounded(self.canvas, 1, 1, self.width - 1, self.height - 1, 18 * s,
                 fill=PANEL, outline=BORDER, width=max(1, round(s)))
        status = read_status()
        controls = read_control()
        label = str(status.get("label") or "待机")
        self.canvas.create_text(22 * s, 24 * s, text=f"Saylo v{self.version}", anchor="w",
                                fill=TEXT, font=self._font(15, "bold"))
        self.canvas.create_text(self.width - 20 * s, 24 * s, text=label, anchor="e",
                                fill=MUTED, font=self._font(11))
        self.canvas.create_line(18 * s, 45 * s, self.width - 18 * s, 45 * s,
                                fill="#292f40", width=max(1, round(s)))
        self._row(51 * s, "暂停自动回复", "pause", bool(controls["paused"]))

        self.canvas.create_text(23 * s, 101 * s, text="回复风格", anchor="w",
                                fill=MUTED, font=self._font(11))
        style = str(controls["reply_style"])
        chip_y, chip_h = 112 * s, 29 * s
        chips = (("natural", "自然"), ("cute", "可爱"), ("concise", "简洁"))
        chip_w = 76 * s
        for index, (key, title) in enumerate(chips):
            x1 = (22 + index * 82) * s
            active = style == key
            _rounded(self.canvas, x1, chip_y, x1 + chip_w, chip_y + chip_h, 9 * s,
                     fill=ACCENT if active else (PANEL_HOVER if self.hover == f"style:{key}" else "#191e2a"),
                     outline="#48516c" if not active else "")
            self.canvas.create_text(x1 + chip_w / 2, chip_y + chip_h / 2, text=title,
                                    fill="#ffffff" if active else "#c2c8d8",
                                    font=self._font(11))
            self._add_hit(x1, chip_y, x1 + chip_w, chip_y + chip_h, f"style:{key}")

        self._row(150 * s, "允许联网检索", "web", bool(controls["web_search"]))
        self._row(188 * s, "开启思考模式", "thinking", bool(controls["thinking"]))
        self.canvas.create_line(18 * s, 231 * s, self.width - 18 * s, 231 * s,
                                fill="#292f40", width=max(1, round(s)))
        self._row(237 * s, "打开运行日志", "log")
        self._row(273 * s, "仅关闭挂件", "close")
        self._row(309 * s, "退出 Saylo", "exit", danger=True)

    def _click(self, event) -> None:
        action = next((name for x1, y1, x2, y2, name in self.hits
                       if x1 <= event.x <= x2 and y1 <= event.y <= y2), None)
        if not action:
            return
        controls = read_control()
        if action == "pause":
            update_control(paused=not bool(controls["paused"]))
            self.draw()
        elif action == "web":
            update_control(web_search=not bool(controls["web_search"]))
            self.draw()
        elif action == "thinking":
            update_control(thinking=not bool(controls["thinking"]))
            self.draw()
        elif action.startswith("style:"):
            update_control(reply_style=action.split(":", 1)[1])
            self.draw()
        elif action == "log":
            try:
                python = Path(sys.executable).with_name("python.exe")
                subprocess.Popen([
                    "powershell.exe", "-NoExit", "-Command",
                    f"& '{python}' '{HERE / 'records.py'}' view --stream agent_log --tail 200",
                ])
            finally:
                self.root.destroy()
        elif action in {"close", "exit"}:
            if action == "exit":
                request_stop()
            ctypes.windll.user32.PostMessageW(self.parent_hwnd, WM_CLOSE, 0, 0)
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    parent = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    version = sys.argv[2] if len(sys.argv) > 2 else "3.2"
    Menu(parent, version).run()
