# -*- coding: utf-8 -*-
"""Saylo 桌面悬浮挂件：黑色核心 + 状态光环 + 右键运行控制。"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as C
from process_utils import pid_running as _pid_running
from runtime_state import (CONTROL_FILE, read_control, read_status,
                           request_stop, update_control)

WIDGET_PID = C.STORE / "widget.pid"
TRANSPARENT = "#010203"

MODE_COLORS = {
    1: ("#3988ff", "#68d8ff", "#745cff"),       # 树洞
    2: ("#ff9f32", "#ffd34e", "#ff6d8e"),       # 轻松
    3: ("#26d5e8", "#5b9dff", "#58f2b5"),       # 搭子
    4: ("#9b6cff", "#4d7dff", "#e75cff"),       # 顾问
    5: ("#35bfd1", "#527cff", "#44d99d"),       # 普通
    6: ("#f45bd2", "#9d6cff", "#ff7a9d"),       # 分享
    7: ("#7c8799", "#a8b0be", "#546174"),       # 收尾
    8: ("#ffb23e", "#ffd76a", "#7b9cff"),       # 澄清
    9: ("#ff5cb8", "#ffca55", "#a96cff"),       # 被夸
    10: ("#38e8c6", "#46a8ff", "#b5f05a"),      # 联网
}
PHASE_LABELS = {
    "idle": "待机", "listening": "收到消息", "thinking": "正在思考",
    "searching": "正在联网寻找", "sending": "正在发送", "happy": "开心",
    "paused": "已暂停", "error": "出现问题", "stopped": "未运行",
}


def _claim_singleton() -> bool:
    try:
        pid = int(WIDGET_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if pid and _pid_running(pid):
        return False
    WIDGET_PID.parent.mkdir(parents=True, exist_ok=True)
    WIDGET_PID.write_text(str(os.getpid()), encoding="utf-8")
    return True


class SayloWidget:
    SIZE = 176

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Saylo")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.wm_attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        x = max(20, self.root.winfo_screenwidth() - self.SIZE - 42)
        y = 78
        self.root.geometry(f"{self.SIZE}x{self.SIZE}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self.SIZE, height=self.SIZE, bg=TRANSPARENT,
            highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Double-Button-1>", lambda _e: self._toggle_pause())

        self.drag_x = self.drag_y = 0
        self.t = 0.0
        self.state = read_status()
        controls = read_control()
        self.pause_var = tk.BooleanVar(value=bool(controls["paused"]))
        self.web_var = tk.BooleanVar(value=bool(controls["web_search"]))
        self.think_var = tk.BooleanVar(value=bool(controls["thinking"]))
        self.style_var = tk.StringVar(value=str(controls["reply_style"]))
        self.menu = self._build_menu()
        self.root.protocol("WM_DELETE_WINDOW", self._close_widget)
        self.root.after(100, self._poll_state)
        self.root.after(16, self._animate)

    def _build_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg="#111318", fg="#f1f3f7",
                       activebackground="#2c3140", activeforeground="white")
        menu.add_command(label="Saylo · 正在启动", state="disabled")
        menu.add_separator()
        menu.add_checkbutton(label="暂停自动回复", variable=self.pause_var,
                             command=self._apply_controls)
        styles = tk.Menu(menu, tearoff=0, bg="#111318", fg="#f1f3f7",
                         activebackground="#2c3140", activeforeground="white")
        styles.add_radiobutton(label="自然", value="natural", variable=self.style_var,
                               command=self._apply_controls)
        styles.add_radiobutton(label="更可爱", value="cute", variable=self.style_var,
                               command=self._apply_controls)
        styles.add_radiobutton(label="简洁", value="concise", variable=self.style_var,
                               command=self._apply_controls)
        menu.add_cascade(label="回复风格", menu=styles)
        menu.add_checkbutton(label="允许联网检索", variable=self.web_var,
                             command=self._apply_controls)
        menu.add_checkbutton(label="开启思考模式", variable=self.think_var,
                             command=self._apply_controls)
        menu.add_separator()
        menu.add_command(label="打开运行日志", command=self._open_log)
        menu.add_command(label="仅关闭挂件", command=self._close_widget)
        menu.add_command(label="退出 Saylo", command=self._exit_saylo)
        return menu

    def _apply_controls(self):
        update_control(
            paused=bool(self.pause_var.get()), web_search=bool(self.web_var.get()),
            thinking=bool(self.think_var.get()), reply_style=self.style_var.get(),
        )

    def _toggle_pause(self):
        self.pause_var.set(not self.pause_var.get())
        self._apply_controls()

    def _show_menu(self, event):
        self.state = read_status()
        controls = read_control()
        self.pause_var.set(bool(controls["paused"]))
        self.web_var.set(bool(controls["web_search"]))
        self.think_var.set(bool(controls["thinking"]))
        self.style_var.set(str(controls["reply_style"]))
        label = self.state.get("label") or PHASE_LABELS.get(self.state.get("phase"), "待机")
        self.menu.entryconfigure(0, label=f"Saylo · {label}")
        self.menu.tk_popup(event.x_root, event.y_root)

    def _drag_start(self, event):
        self.drag_x, self.drag_y = event.x, event.y

    def _drag_move(self, event):
        self.root.geometry(
            f"+{self.root.winfo_x() + event.x - self.drag_x}"
            f"+{self.root.winfo_y() + event.y - self.drag_y}"
        )

    def _open_log(self):
        try:
            python = Path(sys.executable).with_name("python.exe")
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f"& '{python}' '{C.ASSISTANT / 'records.py'}' view --stream agent_log --tail 200",
            ])
        except OSError:
            pass

    def _close_widget(self):
        try:
            WIDGET_PID.unlink(missing_ok=True)
        finally:
            self.root.destroy()

    def _exit_saylo(self):
        request_stop()
        self._close_widget()

    def _poll_state(self):
        self.state = read_status()
        self.root.after(300, self._poll_state)

    def _oval(self, cx, cy, radius, **kwargs):
        return self.canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, **kwargs)

    def _animate(self):
        self.t += 0.055
        phase = str(self.state.get("phase", "stopped"))
        mode = self.state.get("mode")
        colors = MODE_COLORS.get(mode, MODE_COLORS[5])
        if phase == "paused" or phase == "stopped":
            colors = ("#596170", "#2f3540", "#7a8493")
        elif phase == "error":
            colors = ("#ff3f55", "#ff7948", "#8d2337")

        self.canvas.delete("all")
        cx = cy = self.SIZE / 2
        speed = 4.2 if phase == "thinking" else 2.7 if phase == "searching" else 1.0
        pulse = math.sin(self.t * (3.3 if phase in {"thinking", "sending"} else 1.7))

        # 暗色外晕 + 三段流动光环，核心始终是黑色。
        for i in range(4, 0, -1):
            radius = 60 + i * 3 + pulse * 1.4
            self._oval(cx, cy, radius, outline="#101827", width=2)
        for i, color in enumerate(colors):
            radius = 59 + i * 5 + pulse * (1.2 + i * .35)
            start = (self.t * speed * (42 + i * 13) + i * 118) % 360
            extent = 92 + 28 * math.sin(self.t * 1.3 + i)
            self.canvas.create_arc(cx-radius, cy-radius, cx+radius, cy+radius,
                                   start=start, extent=extent, style="arc",
                                   outline=color, width=5-i)
            self.canvas.create_arc(cx-radius, cy-radius, cx+radius, cy+radius,
                                   start=start+180, extent=max(38, extent*.55), style="arc",
                                   outline=color, width=2)

        # 黑色球体用多层深色圆模拟柔和体积。
        self._oval(cx, cy, 56 + pulse*.5, fill="#020306", outline="#121722", width=2)
        self._oval(cx-7, cy-8, 42, fill="#05070b", outline="")
        self._oval(cx-16, cy-19, 18, fill="#0a0d14", outline="")

        if phase == "thinking":
            for i, color in enumerate(colors):
                ang = self.t * (2.2 + i*.35) + i * 2.1
                self._oval(cx + math.cos(ang)*35, cy + math.sin(ang)*35,
                           3.5, fill=color, outline="")
        elif phase == "searching":
            # 两个光点在球内来回扫视，外圈同时快速寻找。
            look = math.sin(self.t * 2.4) * 21
            lift = math.sin(self.t * 1.1) * 7
            self._oval(cx + look - 9, cy + lift, 4.2, fill=colors[0], outline="")
            self._oval(cx + look + 9, cy + lift, 4.2, fill=colors[2], outline="")
            self.canvas.create_arc(cx-35, cy-22, cx+35, cy+22, start=190+look,
                                   extent=85, style="arc", outline=colors[1], width=2)
        elif phase == "happy":
            for i in range(7):
                ang = i * math.tau / 7 + self.t*.25
                rad = 43 + 5 * math.sin(self.t*3+i)
                self._oval(cx+math.cos(ang)*rad, cy+math.sin(ang)*rad,
                           1.8 + (i % 2), fill=colors[i % 3], outline="")
        elif phase == "sending":
            radius = 34 + ((self.t * 18) % 22)
            self._oval(cx, cy, radius, outline=colors[1], width=2)

        self.root.after(33, self._animate)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__" and _claim_singleton():
    try:
        SayloWidget().run()
    finally:
        WIDGET_PID.unlink(missing_ok=True)
