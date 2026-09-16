# -*- coding: utf-8 -*-
"""Saylo 桌面悬浮挂件 v2：透明黑玻璃、流体渐变与状态动画。"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageTk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as C
from process_utils import pid_running as _pid_running
from runtime_state import read_control, read_status, request_stop, update_control

ASSET = HERE / "assets" / "widget_v2" / "saylo_orb_base.png"
WIDGET_PID = C.STORE / "widget_v2.pid"
TRANSPARENT = "#010203"

MODE_COLORS = {
    1: (56, 139, 255),    # 树洞：安静蓝
    2: (255, 164, 70),    # 轻松：暖橙
    3: (50, 218, 203),    # 搭子：青绿
    4: (154, 105, 255),   # 顾问：紫色
    5: (68, 145, 255),    # 普通：清蓝
    6: (245, 92, 207),    # 分享：粉紫
    7: (124, 136, 156),   # 收尾：灰蓝
    8: (255, 184, 66),    # 澄清：琥珀
    9: (255, 92, 185),    # 被夸：明亮粉
    10: (50, 232, 198),   # 联网：薄荷青
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


def _multiply_alpha(image: Image.Image, factor: float) -> Image.Image:
    image = image.copy()
    alpha = image.getchannel("A").point(lambda value: int(value * factor))
    image.putalpha(alpha)
    return image


def _tint(image: Image.Image, color: tuple[int, int, int], strength: float) -> Image.Image:
    wash = Image.new("RGBA", image.size, (*color, 255))
    mixed = Image.blend(image, wash, strength)
    mixed.putalpha(image.getchannel("A"))
    return mixed


def _center_crop_alpha(image: Image.Image) -> Image.Image:
    bbox = image.getchannel("A").getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    side = max(right - left, bottom - top)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    left = max(0, cx - side // 2)
    top = max(0, cy - side // 2)
    right = min(image.width, left + side)
    bottom = min(image.height, top + side)
    return image.crop((left, top, right, bottom))


class OrbRenderer:
    """用一张透明材质图实时合成呼吸、旋转、扫描和情绪光晕。"""

    def __init__(self, size: int = 224):
        self.size = size
        source = Image.open(ASSET).convert("RGBA")
        self.source = _center_crop_alpha(source)
        self._base_cache: dict[tuple[int, int, int], Image.Image] = {}

    def _orb(self, color: tuple[int, int, int]) -> Image.Image:
        if color not in self._base_cache:
            base = self.source.resize((184, 184), Image.Resampling.LANCZOS)
            self._base_cache[color] = _tint(base, color, 0.10)
        return self._base_cache[color].copy()

    @staticmethod
    def _place(canvas: Image.Image, layer: Image.Image, x: int, y: int) -> None:
        canvas.alpha_composite(layer, (x, y))

    def render(self, phase: str, mode: int | None, t: float) -> Image.Image:
        color = MODE_COLORS.get(mode, MODE_COLORS[5])
        if phase in {"paused", "stopped"}:
            color = (110, 123, 145)
        elif phase == "error":
            color = (255, 63, 84)

        frame = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
        fast = phase in {"thinking", "searching"}
        pulse = math.sin(t * (3.1 if fast else 1.35))
        scale = 1.0 + pulse * (0.026 if fast else 0.014)
        if phase == "listening":
            scale += 0.025
        orb = self._orb(color)

        if phase in {"paused", "stopped"}:
            gray = ImageOps.grayscale(orb).convert("RGBA")
            gray.putalpha(orb.getchannel("A").point(lambda a: int(a * 0.62)))
            orb = gray
        elif phase == "error":
            orb = _tint(orb, color, 0.32)

        target = max(148, int(184 * scale))
        orb = orb.resize((target, target), Image.Resampling.LANCZOS)
        if phase == "thinking":
            angle = (t * 42.0) % 360
        elif phase == "searching":
            angle = 9.0 * math.sin(t * 1.65)
        else:
            angle = 2.4 * math.sin(t * 0.34)
        orb = orb.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)

        x = (self.size - target) // 2
        y = (self.size - target) // 2
        if phase == "searching":
            x += int(math.sin(t * 2.15) * 4)

        # 有色软光使用素材自身 alpha 派生，避免退回到几何线圈。
        halo = _multiply_alpha(orb, 0.40 if fast else 0.28)
        halo = halo.filter(ImageFilter.GaussianBlur(13 if fast else 16))
        self._place(frame, halo, x, y)
        self._place(frame, orb, x, y)

        if phase == "thinking":
            # 两层反向流动的透明材质制造内部转动与纵深。
            inner = self._orb(color).resize((158, 158), Image.Resampling.LANCZOS)
            inner = inner.rotate(-(t * 68.0) % 360, Image.Resampling.BICUBIC, expand=False)
            inner = _multiply_alpha(inner, 0.24)
            self._place(frame, inner, (self.size - 158) // 2, (self.size - 158) // 2)
        elif phase == "searching":
            # 一束模糊折射光在球内往返，表达“正在寻找”，不画眼睛或放大镜。
            scan = Image.new("RGBA", (target, target), (0, 0, 0, 0))
            draw = ImageDraw.Draw(scan)
            sx = int((math.sin(t * 2.0) * 0.42 + 0.5) * target)
            draw.ellipse((sx - 17, 18, sx + 17, target - 18), fill=(*color, 132))
            scan = scan.filter(ImageFilter.GaussianBlur(16))
            scan.putalpha(ImageChops.multiply(scan.getchannel("A"), orb.getchannel("A")))
            self._place(frame, scan, x, y)
        elif phase == "sending":
            wave = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(wave)
            r = int(68 + (t * 26) % 26)
            fade = max(0, 120 - (r - 68) * 4)
            draw.ellipse((self.size//2-r, self.size//2-r, self.size//2+r,
                          self.size//2+r), outline=(*color, fade), width=3)
            frame = Image.alpha_composite(frame, wave.filter(ImageFilter.GaussianBlur(2)))
        elif phase == "happy":
            sparks = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(sparks)
            for index in range(8):
                a = index * math.tau / 8 + t * 0.18
                r = 87 + 5 * math.sin(t * 2.4 + index)
                px = self.size / 2 + math.cos(a) * r
                py = self.size / 2 + math.sin(a) * r
                radius = 1.5 + (index % 3)
                warm = (255, 175, 105) if index % 2 else color
                draw.ellipse((px-radius, py-radius, px+radius, py+radius), fill=(*warm, 190))
            frame = Image.alpha_composite(frame, sparks.filter(ImageFilter.GaussianBlur(1.1)))

        # 压低整体亮度；悬浮物仍以黑玻璃为主，而不是彩色灯球。
        if phase not in {"thinking", "searching", "happy"}:
            frame = ImageEnhance.Brightness(frame).enhance(0.92)
        return frame


class SayloWidgetV2:
    SIZE = 224

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Saylo v2")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.98)
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.wm_attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        x = max(20, self.root.winfo_screenwidth() - self.SIZE - 28)
        self.root.geometry(f"{self.SIZE}x{self.SIZE}+{x}+58")

        self.canvas = tk.Canvas(self.root, width=self.SIZE, height=self.SIZE,
                                bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Double-Button-1>", lambda _event: self._toggle_pause())

        self.drag_x = self.drag_y = 0
        self.started_at = time.monotonic()
        self.state = read_status()
        controls = read_control()
        self.pause_var = tk.BooleanVar(value=bool(controls["paused"]))
        self.web_var = tk.BooleanVar(value=bool(controls["web_search"]))
        self.think_var = tk.BooleanVar(value=bool(controls["thinking"]))
        self.style_var = tk.StringVar(value=str(controls["reply_style"]))
        self.renderer = OrbRenderer(self.SIZE)
        self.photo = None
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")
        self.menu = self._build_menu()
        self.root.protocol("WM_DELETE_WINDOW", self._close_widget)
        self.root.after(100, self._poll_state)
        self.root.after(16, self._animate)

    def _build_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg="#0b0d12", fg="#f2f4f8",
                       activebackground="#292d38", activeforeground="white")
        menu.add_command(label="Saylo v2 · 正在启动", state="disabled")
        menu.add_separator()
        menu.add_checkbutton(label="暂停自动回复", variable=self.pause_var,
                             command=self._apply_controls)
        styles = tk.Menu(menu, tearoff=0, bg="#0b0d12", fg="#f2f4f8",
                         activebackground="#292d38", activeforeground="white")
        for label, value in (("自然", "natural"), ("更可爱", "cute"), ("简洁", "concise")):
            styles.add_radiobutton(label=label, value=value, variable=self.style_var,
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
        update_control(paused=bool(self.pause_var.get()),
                       web_search=bool(self.web_var.get()),
                       thinking=bool(self.think_var.get()),
                       reply_style=self.style_var.get())

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
        self.menu.entryconfigure(0, label=f"Saylo v2 · {label}")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _drag_start(self, event):
        self.drag_x, self.drag_y = event.x, event.y

    def _drag_move(self, event):
        self.root.geometry(f"+{self.root.winfo_x() + event.x - self.drag_x}"
                           f"+{self.root.winfo_y() + event.y - self.drag_y}")

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
        self.root.after(250, self._poll_state)

    def _animate(self):
        phase = str(self.state.get("phase", "stopped"))
        mode = self.state.get("mode")
        frame = self.renderer.render(phase, mode, time.monotonic() - self.started_at)
        self.photo = ImageTk.PhotoImage(frame)
        self.canvas.itemconfigure(self.image_id, image=self.photo)
        self.root.after(40, self._animate)

    def run(self):
        self.root.mainloop()


def _render_preview(path: Path) -> None:
    phases = ("idle", "listening", "thinking", "searching", "happy", "error")
    sheet = Image.new("RGBA", (224 * len(phases), 256), (12, 13, 18, 255))
    renderer = OrbRenderer(224)
    for index, phase in enumerate(phases):
        frame = renderer.render(phase, 10 if phase == "searching" else 9, 2.4)
        sheet.alpha_composite(frame, (224 * index, 0))
        ImageDraw.Draw(sheet).text((224 * index + 84, 228), phase, fill=(225, 228, 238, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--render-preview":
        _render_preview(Path(sys.argv[2]))
    elif _claim_singleton():
        try:
            SayloWidgetV2().run()
        finally:
            WIDGET_PID.unlink(missing_ok=True)
