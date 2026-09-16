# -*- coding: utf-8 -*-
"""Saylo v3.2：实时程序化流体 + Windows 逐像素透明悬浮窗口。

运行时不读取任何球体图片。每一帧的形状、光带、折射、辉光和 alpha 都由 numpy
重新计算，再通过 UpdateLayeredWindow 交给 Windows 桌面合成器。
"""
from __future__ import annotations

import ctypes
import math
import os
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as C
from process_utils import pid_running as _pid_running
from runtime_state import read_control, read_status, request_stop, update_control

WIDGET_PID = C.STORE / "widget_v3.pid"
WIDGET_LOG = C.ASSISTANT / "widget_v3.log"
VERSION = "3.2"
WIDGET_SIZE = 128
STATUS_STALE_SEC = 12.0
PHASE_MOTION = {
    "idle": (0.0975, 0.18, 0.010),     # 在无序多层漂移上保留较弱、但可察觉的整体公转
    "listening": (0.18, 0.13, 0.012),
    "thinking": (0.54, 0.055, 0.014), # 约 12 秒一圈，整体同速、纹理近乎固定
    "searching": (0.62, 0.16, 0.016),
    "sending": (0.46, 0.08, 0.012),
    "happy": (0.42, 0.09, 0.013),
    "paused": (0.08, 0.05, 0.004),
    "error": (0.16, 0.07, 0.006),
    "stopped": (0.10, 0.05, 0.004),
}

MODE_PALETTES = {
    1: ((0.10, 0.46, 1.00), (0.36, 0.82, 1.00), (0.48, 0.24, 0.95)),
    2: ((1.00, 0.43, 0.22), (1.00, 0.78, 0.22), (0.96, 0.22, 0.58)),
    3: ((0.05, 0.82, 0.86), (0.18, 0.48, 1.00), (0.18, 0.95, 0.62)),
    4: ((0.42, 0.28, 1.00), (0.72, 0.20, 1.00), (0.16, 0.62, 1.00)),
    5: ((0.08, 0.50, 1.00), (0.38, 0.85, 1.00), (0.72, 0.18, 0.98)),
    6: ((0.96, 0.16, 0.74), (0.46, 0.24, 1.00), (1.00, 0.42, 0.56)),
    7: ((0.30, 0.38, 0.54), (0.47, 0.55, 0.70), (0.24, 0.30, 0.45)),
    8: ((1.00, 0.54, 0.15), (1.00, 0.80, 0.32), (0.34, 0.48, 1.00)),
    9: ((1.00, 0.18, 0.70), (1.00, 0.65, 0.20), (0.58, 0.24, 1.00)),
    10: ((0.04, 0.92, 0.72), (0.10, 0.54, 1.00), (0.58, 0.95, 0.24)),
}
PHASE_LABELS = {
    "idle": "待机", "listening": "收到消息", "thinking": "正在思考",
    "searching": "正在联网寻找", "sending": "正在发送", "happy": "开心",
    "paused": "已暂停", "error": "出现问题", "stopped": "未运行",
}


def _claim_singleton() -> bool:
    try:
        old_pid = int(WIDGET_PID.read_text(encoding="utf-8").strip())
        pid_age = max(0.0, time.time() - WIDGET_PID.stat().st_mtime)
    except (OSError, ValueError):
        old_pid, pid_age = 0, 999.0
    if old_pid and _pid_running(old_pid):
        # 虚拟环境的 pythonw 启动链偶尔会留下仍存活但已没有挂件窗口的包装进程。
        # 新写入 3 秒内给窗口创建留时间；之后必须真的拥有对应顶层窗口才算实例存在。
        if pid_age < 3.0 or _widget_window_exists(old_pid):
            return False
    WIDGET_PID.parent.mkdir(parents=True, exist_ok=True)
    WIDGET_PID.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _widget_window_exists(pid: int) -> bool:
    if sys.platform != "win32":
        return _pid_running(pid)
    found = ctypes.c_bool(False)
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            class_name = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value.startswith("SayloRealtimeOrb_"):
                found.value = True
                return False
        return True

    callback = enum_proc(visit)
    ctypes.windll.user32.EnumWindows(callback, 0)
    return bool(found.value)


def _smoothstep(edge0: float, edge1: float, value: np.ndarray) -> np.ndarray:
    t = np.clip((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


class ProceduralOrb:
    """小型 CPU fragment shader：输入时间与状态，输出 straight-alpha RGBA。"""

    def __init__(self, size: int = WIDGET_SIZE):
        self.size = size
        # 留出一圈真实透明像素，让更宽的外晕不会在窗口边缘被切断。
        axis = np.linspace(-1.42, 1.42, size, dtype=np.float32)
        self.x, self.y = np.meshgrid(axis, axis)
        self.r = np.sqrt(self.x * self.x + self.y * self.y)
        self.theta = np.arctan2(self.y, self.x)
        # v3.1 把实体更早地融进外晕，整个边缘是松散的雾，而不是一条圆形边线。
        self.inside = 1.0 - _smoothstep(0.64, 1.075, self.r)
        outer = np.maximum(self.r - 0.62, 0.0)
        self.halo = (np.exp(-np.square(outer / 0.305)) *
                     _smoothstep(0.46, 0.82, self.r))
        self.halo[self.r > 1.30] = 0.0
        self.normal_z = np.sqrt(np.clip(1.0 - self.r * self.r, 0.0, 1.0))

    @staticmethod
    def _soft_blur(field: np.ndarray, passes: int = 1) -> np.ndarray:
        """廉价的可分离高斯近似；只模糊程序场，不生成或缩放任何贴图。"""
        result = field
        for _ in range(passes):
            result = (np.roll(result, -2, 0) + 4.0 * np.roll(result, -1, 0) +
                      6.0 * result + 4.0 * np.roll(result, 1, 0) +
                      np.roll(result, 2, 0)) / 16.0
            result = (np.roll(result, -2, 1) + 4.0 * np.roll(result, -1, 1) +
                      6.0 * result + 4.0 * np.roll(result, 1, 1) +
                      np.roll(result, 2, 1)) / 16.0
        return result

    @staticmethod
    def palette(phase: str, mode: int | None) -> np.ndarray:
        colors = MODE_PALETTES.get(mode, MODE_PALETTES[5])
        if phase in {"paused", "stopped"}:
            colors = ((0.26, 0.31, 0.40), (0.42, 0.48, 0.58), (0.22, 0.26, 0.34))
        elif phase == "error":
            colors = ((1.00, 0.08, 0.18), (0.86, 0.18, 0.36), (1.00, 0.38, 0.12))
        elif phase == "searching":
            colors = MODE_PALETTES[10]
        elif phase == "happy":
            colors = MODE_PALETTES[9]
        result = np.asarray(colors, dtype=np.float32)
        if phase not in {"paused", "stopped"}:
            # 亮色不等于纯白：抬高中间调，同时保留三组状态色之间的层次。
            result = np.clip(result * 1.16 + 0.045, 0.0, 1.0)
        return result

    def render(self, phase: str, mode: int | None, seconds: float,
               palette_override: np.ndarray | None = None,
               motion_override: tuple[float, float, float] | None = None,
               effect_weights: dict[str, float] | None = None) -> np.ndarray:
        palette = (palette_override if palette_override is not None else
                   self.palette(phase, mode))
        if motion_override is None:
            rotation = seconds * PHASE_MOTION.get(phase, PHASE_MOTION["idle"])[0]
            t = seconds * PHASE_MOTION.get(phase, PHASE_MOTION["idle"])[1]
            pulse_amount = PHASE_MOTION.get(phase, PHASE_MOTION["idle"])[2]
        else:
            rotation, t, pulse_amount = motion_override
        weights = effect_weights or {phase: 1.0}
        idle_weight = min(1.0, float(weights.get("idle", 0.0)) +
                          0.45 * float(weights.get("listening", 0.0)))
        thinking_weight = float(weights.get("thinking", 1.0 if phase == "thinking" else 0.0))
        sending_weight = float(weights.get("sending", 0.0))
        # 方案 B：发送状态靠缓动权重平滑放大到约 103%，离开后自然恢复。
        pulse = 1.0 + pulse_amount * math.sin(t * 2.1) + 0.030 * sending_weight
        x = self.x / pulse
        y = self.y / pulse
        r = np.sqrt(x * x + y * y)
        theta = np.arctan2(y, x)
        inside = 1.0 - _smoothstep(0.64, 1.075, r)

        # v3.2 使用全场同角速度旋转；不再让内外半径彼此剪切成螺旋。
        idle_wander = idle_weight * (
            0.17 * np.sin(2.1 * theta + 1.6 * r + t * 0.57) +
            0.10 * np.sin(4.3 * theta - 2.4 * r - t * 0.39)
        )
        angle = theta + rotation + 0.035 * math.sin(t * 0.32) + idle_wander
        u = r * np.cos(angle)
        v = r * np.sin(angle)
        # 待机时各层各自漂移；思考/回答时适度收拢，发送阶段再略加强，但不汇成一点。
        u += idle_weight * (0.052 * np.sin(2.7 * v + t * 0.46) +
                            0.024 * np.cos(5.1 * u - t * 0.31))
        v += idle_weight * (0.047 * np.cos(2.3 * u - t * 0.41) -
                            0.022 * np.sin(4.6 * v + t * 0.28))
        # 只有发送阶段向中心收拢；思考和搜索只改变运动方式，不压缩内部光场。
        focus_convergence = 0.450 * sending_weight
        convergence = 1.0 + focus_convergence
        u *= convergence
        v *= convergence
        u += 0.085 * np.sin(3.2 * v + t * 0.71) + 0.035 * np.sin(8.0 * v - t)
        v += 0.075 * np.sin(2.7 * u - t * 0.58) + 0.030 * np.cos(7.0 * u + t * 0.8)
        # 两组不对称的交叉扰动打散规则旋涡，形成会自行生长和消失的小褶皱。
        disorder = (np.sin(4.7 * u + 3.1 * v + t * 0.53) *
                    np.cos(2.2 * u - 5.3 * v - t * 0.39))
        u += 0.060 * disorder + 0.028 * np.sin(11.0 * v + t * 0.31)
        v += 0.052 * np.sin(3.8 * u - 4.4 * v - t * 0.47) - 0.025 * disorder

        width = 0.195 - 0.020 * thinking_weight
        c1 = (v - 0.27 * np.sin(2.15 * u + t * 0.72) -
              0.115 * np.sin(5.8 * u - t * 0.34) + 0.08 * np.cos(3.3 * v + t * 0.2) +
              0.12 + 0.065 * math.sin(t * 0.23))
        c2 = (u + 0.23 * np.sin(3.15 * v - t * 0.63) +
              0.095 * np.cos(6.2 * v + t * 0.45) - 0.16 -
              0.07 * math.sin(t * 0.51))
        c3 = np.sin(2.35 * angle + 4.6 * r - t * 0.82 + disorder * 0.62) - 0.61
        width1 = width * (0.50 + 0.78 * (0.5 + 0.5 * np.sin(
            3.7 * u - 2.1 * v + t + disorder * 1.7)))
        width2 = width * (0.44 + 0.72 * (0.5 + 0.5 * np.cos(
            2.4 * u + 4.2 * v - t * 0.7 - disorder * 1.4)))
        ribbon1 = np.exp(-np.square(c1 / width1))
        ribbon2 = np.exp(-np.square(c2 / width2))
        ribbon3 = np.exp(-np.square(c3 / 0.23)) * (0.35 + 0.65 * np.clip(1.0 - r, 0, 1))
        # 两层刻意不共心、不同频率的流光，避免形成整齐的放射扇叶。
        c4 = (v + 0.30 * np.cos(1.75 * u - t * 0.43) -
              0.14 * np.sin(4.9 * u + 2.1 * v + t * 0.28) - 0.21)
        c5 = (u - v * 0.36 + 0.24 * np.sin(2.4 * v + t * 0.51) +
              0.11 * np.cos(6.4 * u - 1.7 * v - t * 0.37) + 0.18)
        gate4 = 0.48 + 0.52 * np.clip(0.5 + 0.5 * np.sin(3.1 * u + 2.6 * v + t * 0.31), 0, 1)
        gate5 = 0.42 + 0.58 * np.clip(0.5 + 0.5 * np.cos(2.3 * u - 4.1 * v - t * 0.27), 0, 1)
        ribbon4 = np.exp(-np.square(c4 / (width * 1.28))) * gate4
        ribbon5 = np.exp(-np.square(c5 / (width * 1.42))) * gate5

        # 顶层光效只保留少量清晰核心，其余以多次软化后的宽光雾叠加。
        soft1 = self._soft_blur(ribbon1, 2)
        soft2 = self._soft_blur(ribbon2, 2)
        soft3 = self._soft_blur(ribbon3, 1)
        soft4 = self._soft_blur(ribbon4, 2)
        soft5 = self._soft_blur(ribbon5, 2)
        grain = 0.5 + 0.5 * np.sin(9.0 * u + 5.0 * v + t + disorder) * np.sin(6.0 * v - t * 0.7)
        fog = np.power(np.clip(0.5 + 0.5 * np.sin(
            5.2 * u - 3.7 * v + t * 0.29 + 1.4 * disorder), 0.0, 1.0), 4.0)
        # 几团速度和方向不同的软光在场内漂移，打破所有光带都汇向中心的秩序感。
        bx1, by1 = 0.36 * math.sin(t * 0.41), 0.31 * math.cos(t * 0.33)
        bx2, by2 = -0.42 * math.cos(t * 0.27), 0.34 * math.sin(t * 0.52)
        cloud1 = np.exp(-(((u - bx1 + disorder * 0.06) / 0.34) ** 2 +
                          ((v - by1) / 0.22) ** 2))
        cloud2 = np.exp(-(((u - bx2) / 0.25) ** 2 +
                          ((v - by2 - disorder * 0.05) / 0.38) ** 2))
        cloud3 = np.exp(-(((u + 0.12 * math.sin(t * 0.61)) / 0.48) ** 2 +
                          ((v - 0.38 * math.cos(t * 0.22)) / 0.17) ** 2))
        clouds = np.clip(cloud1 + cloud2 * 0.82 + cloud3 * 0.58, 0.0, 1.35)
        fluid_mask = np.clip((soft1 * 0.40 + soft2 * 0.35 + soft3 * 0.28 +
                              soft4 * 0.25 + soft5 * 0.22) *
                             (0.72 + grain * 0.28) + fog * 0.24 + clouds * 0.40,
                             0.0, 1.75) * inside

        ribbon_transparency = (0.48 + 0.34 * grain + 0.18 * (0.5 + 0.5 * disorder))[..., None]
        fluid = ((soft1[..., None] * 0.84 + ribbon1[..., None] * 0.08) * palette[0] * 0.58 +
                 (soft2[..., None] * 0.86 + ribbon2[..., None] * 0.07) * palette[1] * 0.52 +
                 soft3[..., None] * palette[2] * 0.44 +
                 soft4[..., None] * (palette[0] * 0.38 + palette[2] * 0.62) * 0.34 +
                 soft5[..., None] * (palette[1] * 0.58 + palette[2] * 0.42) * 0.30
                 ) * ribbon_transparency
        fog_color = palette[0] * 0.42 + palette[2] * 0.58
        fluid += fog[..., None] * fog_color * 0.20
        fluid += (cloud1[..., None] * palette[2] * 0.29 +
                  cloud2[..., None] * palette[0] * 0.27 +
                  cloud3[..., None] * palette[1] * 0.21)
        fluid *= (0.58 + 0.42 * grain[..., None]) * inside[..., None]

        # 深色玻璃体积、边缘菲涅尔和左上方连续高光。
        light = np.clip(-0.34 * x - 0.48 * y + 0.76 * self.normal_z, 0.0, 1.0)
        core = np.zeros((*x.shape, 3), dtype=np.float32)
        core[:] = (0.006, 0.009, 0.016)
        core += (0.018 + 0.036 * light[..., None]) * inside[..., None]
        paused_weight = max(float(weights.get("paused", 0.0)),
                            float(weights.get("stopped", 0.0)))
        rgb = core + fluid * (1.0 - 0.26 * paused_weight)
        fresnel = np.power(np.clip(r, 0.0, 1.0), 6.0) * inside
        rim_mix = palette[0] * 0.48 + palette[2] * 0.52
        rgb += fresnel[..., None] * rim_mix * 0.62
        spec = np.exp(-(((x + 0.38) / 0.27) ** 2 + ((y + 0.43) / 0.16) ** 2))
        spec *= inside * (0.82 - 0.56 * paused_weight)
        rgb += spec[..., None] * np.asarray((0.68, 0.82, 1.0), np.float32) * 0.72

        alpha = np.clip(inside * 0.86 + self.halo *
                        (0.055 + 0.145 * np.clip(fluid_mask, 0, 1)), 0.0, 1.0)

        searching_weight = float(weights.get("searching", 0.0))
        if searching_weight > 0.001:
            scan_x = 0.58 * math.sin(seconds * 1.9)
            scan = np.exp(-np.square((x - scan_x) / 0.075)) * inside
            rgb += scan[..., None] * palette[1] * (0.42 * searching_weight)
        attention_weight = sending_weight
        if attention_weight > 0.001:
            # 专注状态只略微抬亮中央，仍保留外围流光和玻璃层次。
            focus = np.exp(-np.square(r / 0.38)) * attention_weight
            focus_color = palette[1] * 0.55 + palette[0] * 0.45
            rgb += focus[..., None] * focus_color * 0.105
        # 胶片式压高光，避免简单 RGB 叠加产生廉价霓虹灯感。
        rgb = 1.0 - np.exp(-np.clip(rgb, 0.0, None) * 1.34)
        rgb *= (0.68 + 0.32 * self.inside[..., None])
        rgba = np.empty((*x.shape, 4), dtype=np.uint8)
        rgba[..., :3] = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
        rgba[..., 3] = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
        rgba[self.r > 1.30] = 0
        return rgba


if sys.platform == "win32":
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                    ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class SayloWidgetV3:
    SIZE = WIDGET_SIZE
    WM_DESTROY = 0x0002
    WM_ERASEBKGND = 0x0014
    WM_TIMER = 0x0113
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_NCLBUTTONDOWN = 0x00A1
    HTCAPTION = 2
    CS_DBLCLKS = 0x0008
    WS_POPUP = 0x80000000
    WS_VISIBLE = 0x10000000
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0
    AC_SRC_ALPHA = 1
    DIB_RGB_COLORS = 0
    BI_RGB = 0

    CMD_PAUSE = 1001
    CMD_NATURAL = 1011
    CMD_CUTE = 1012
    CMD_CONCISE = 1013
    CMD_WEB = 1021
    CMD_THINKING = 1022
    CMD_LOG = 1090
    CMD_CLOSE = 1091
    CMD_EXIT = 1092

    MF_STRING = 0x0000
    MF_SEPARATOR = 0x0800
    MF_GRAYED = 0x0001
    MF_CHECKED = 0x0008
    MF_POPUP = 0x0010
    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("v3 layered widget currently requires Windows")
        self.renderer = ProceduralOrb(self.SIZE)
        self.started_at = time.perf_counter()
        self.last_frame_at = self.started_at
        self.state = read_status()
        self.frame_count = 0
        initial_palette = self.renderer.palette("idle", 5)
        self.display_palette_linear = np.power(initial_palette, 2.2)
        self.display_motion = np.asarray(PHASE_MOTION["idle"], dtype=np.float32)
        self.rotation_angle = 0.0
        self.morph_time = 0.0
        self.effect_weights = {name: 0.0 for name in PHASE_MOTION}
        self.effect_weights["idle"] = 1.0
        self.display_frame_pm: np.ndarray | None = None
        self.class_name = f"SayloRealtimeOrb_{os.getpid()}"
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._configure_api()
        self.hinstance = kernel32.GetModuleHandleW(None)
        self.hwnd = None
        self.screen_dc = self.mem_dc = self.bitmap = self.old_bitmap = None
        self.bits = ctypes.c_void_p()
        self._create_window()
        self._create_surface()
        user32.SetTimer(self.hwnd, 1, 33, None)
        self._render()

    @staticmethod
    def _configure_api() -> None:
        handle = wintypes.HANDLE
        uint_ptr = ctypes.c_size_t
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, handle, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
        ]
        user32.SetTimer.argtypes = [wintypes.HWND, uint_ptr, wintypes.UINT, ctypes.c_void_p]
        user32.KillTimer.argtypes = [wintypes.HWND, uint_ptr]
        user32.ReleaseCapture.argtypes = []
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = LRESULT
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.CreatePopupMenu.restype = handle
        user32.AppendMenuW.argtypes = [handle, wintypes.UINT, uint_ptr, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [handle, wintypes.UINT, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                          ctypes.c_void_p]
        user32.DestroyMenu.argtypes = [handle]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), handle, wintypes.DWORD,
        ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

    def _create_window(self) -> None:
        wc = WNDCLASSW()
        wc.style = self.CS_DBLCLKS
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = self.hinstance
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        wc.lpszClassName = self.class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError()
        screen_w = user32.GetSystemMetrics(0)
        self.hwnd = user32.CreateWindowExW(
            self.WS_EX_LAYERED | self.WS_EX_TOPMOST | self.WS_EX_TOOLWINDOW,
            self.class_name, f"Saylo v{VERSION}", self.WS_POPUP | self.WS_VISIBLE,
            max(20, screen_w - self.SIZE - 28), 58, self.SIZE, self.SIZE,
            None, None, self.hinstance, None,
        )
        if not self.hwnd:
            raise ctypes.WinError()

    def _create_surface(self) -> None:
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.SIZE
        bmi.bmiHeader.biHeight = -self.SIZE
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = self.BI_RGB
        self.bitmap = gdi32.CreateDIBSection(
            self.mem_dc, ctypes.byref(bmi), self.DIB_RGB_COLORS,
            ctypes.byref(self.bits), None, 0,
        )
        if not self.bitmap or not self.bits:
            raise ctypes.WinError()
        self.old_bitmap = gdi32.SelectObject(self.mem_dc, self.bitmap)

    @staticmethod
    def _validated_state(state: dict) -> dict:
        """不让旧 JSON 冒充在线状态；后台断开时挂件必须立即说实话。"""
        phase = str(state.get("phase", "stopped"))
        if phase == "stopped":
            return state
        try:
            pid = int(state.get("pid", 0))
            heartbeat = float(state.get("heartbeat_at", state.get("updated_at", 0.0)))
        except (TypeError, ValueError):
            pid, heartbeat = 0, 0.0
        age = max(0.0, time.time() - heartbeat)
        if pid <= 0 or not _pid_running(pid) or age > STATUS_STALE_SEC:
            return {
                "phase": "error", "mode": None, "label": "后台已断开",
                "detail": f"回复进程无心跳（{age:.0f} 秒）", "pid": pid,
            }
        return state

    def _render(self) -> None:
        now_perf = time.perf_counter()
        dt = min(0.10, max(0.001, now_perf - self.last_frame_at))
        self.last_frame_at = now_perf
        self.frame_count += 1
        if self.frame_count % 8 == 1:
            self.state = self._validated_state(read_status())
        phase = str(self.state.get("phase", "stopped"))
        mode = self.state.get("mode")
        target_palette_linear = np.power(self.renderer.palette(phase, mode), 2.2)
        # 普通情绪色约五秒完成主要过渡，避免模式切换时瞬间“换脸”；错误仍快速提示。
        palette_tau = 0.18 if phase == "error" else 1.65
        palette_blend = 1.0 - math.exp(-dt / palette_tau)
        self.display_palette_linear += (
            target_palette_linear - self.display_palette_linear
        ) * palette_blend
        display_palette = np.power(np.clip(self.display_palette_linear, 0.0, 1.0), 1.0 / 2.2)

        target_motion = np.asarray(PHASE_MOTION.get(phase, PHASE_MOTION["idle"]),
                                   dtype=np.float32)
        motion_blend = 1.0 - math.exp(-dt / 0.42)
        self.display_motion += (target_motion - self.display_motion) * motion_blend
        self.rotation_angle = (self.rotation_angle + float(self.display_motion[0]) * dt) % (2 * math.pi)
        self.morph_time += float(self.display_motion[1]) * dt

        effect_tau = 0.15 if phase == "error" else 0.36
        effect_blend = 1.0 - math.exp(-dt / effect_tau)
        for name in self.effect_weights:
            target_weight = 1.0 if name == phase else 0.0
            self.effect_weights[name] += (
                target_weight - self.effect_weights[name]
            ) * effect_blend

        rgba = self.renderer.render(
            phase, mode, now_perf - self.started_at,
            palette_override=display_palette,
            motion_override=(self.rotation_angle, self.morph_time,
                             float(self.display_motion[2])),
            effect_weights=self.effect_weights,
        )
        alpha = rgba[..., 3].astype(np.float32) / 255.0
        current_pm = np.empty(rgba.shape, dtype=np.float32)
        current_pm[..., 0] = rgba[..., 2].astype(np.float32) / 255.0 * alpha
        current_pm[..., 1] = rgba[..., 1].astype(np.float32) / 255.0 * alpha
        current_pm[..., 2] = rgba[..., 0].astype(np.float32) / 255.0 * alpha
        current_pm[..., 3] = alpha
        # 参数连续仍可能被 exp/power 遮罩放大成局部闪跳；短时帧域低通只消除突变，
        # 预乘空间插值不会在透明边缘留下黑边。
        if self.display_frame_pm is None:
            self.display_frame_pm = current_pm
        else:
            frame_tau = 0.035 if phase == "error" else 0.060
            frame_blend = 1.0 - math.exp(-dt / frame_tau)
            self.display_frame_pm += (current_pm - self.display_frame_pm) * frame_blend
        premul = np.clip(self.display_frame_pm * 255.0, 0, 255).astype(np.uint8)
        ctypes.memmove(self.bits, premul.ctypes.data, premul.nbytes)

        rect = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        dst = wintypes.POINT(rect.left, rect.top)
        src = wintypes.POINT(0, 0)
        size = SIZE(self.SIZE, self.SIZE)
        blend = BLENDFUNCTION(self.AC_SRC_OVER, 0, 255, self.AC_SRC_ALPHA)
        if not user32.UpdateLayeredWindow(
                self.hwnd, self.screen_dc, ctypes.byref(dst), ctypes.byref(size),
                self.mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), self.ULW_ALPHA):
            raise ctypes.WinError()

    def _menu_item(self, menu, command: int, label: str, checked: bool = False,
                   disabled: bool = False) -> None:
        flags = self.MF_STRING
        if checked:
            flags |= self.MF_CHECKED
        if disabled:
            flags |= self.MF_GRAYED
        user32.AppendMenuW(menu, flags, command, label)

    def _show_menu(self) -> None:
        """优先打开 v3.2 自绘高 DPI 菜单，启动失败才回退到系统菜单。"""
        try:
            menu_entry = HERE / "widget_menu.pyw"
            subprocess.Popen(
                [sys.executable, str(menu_entry), str(int(self.hwnd)), VERSION],
                cwd=str(HERE), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception as exc:
            self._log_error(exc)
        self._show_native_menu()

    def _show_native_menu(self) -> None:
        controls = read_control()
        state = self._validated_state(read_status())
        menu = user32.CreatePopupMenu()
        styles = user32.CreatePopupMenu()
        label = state.get("label") or PHASE_LABELS.get(state.get("phase"), "待机")
        self._menu_item(menu, 0, f"Saylo v{VERSION} · {label}", disabled=True)
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        self._menu_item(menu, self.CMD_PAUSE, "暂停自动回复", bool(controls["paused"]))
        style = str(controls["reply_style"])
        self._menu_item(styles, self.CMD_NATURAL, "自然", style == "natural")
        self._menu_item(styles, self.CMD_CUTE, "更可爱", style == "cute")
        self._menu_item(styles, self.CMD_CONCISE, "简洁", style == "concise")
        user32.AppendMenuW(menu, self.MF_POPUP, styles, "回复风格")
        self._menu_item(menu, self.CMD_WEB, "允许联网检索", bool(controls["web_search"]))
        self._menu_item(menu, self.CMD_THINKING, "开启思考模式", bool(controls["thinking"]))
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        self._menu_item(menu, self.CMD_LOG, "打开运行日志")
        self._menu_item(menu, self.CMD_CLOSE, "仅关闭挂件")
        self._menu_item(menu, self.CMD_EXIT, "退出 Saylo")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        command = user32.TrackPopupMenu(
            menu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
            point.x, point.y, 0, self.hwnd, None,
        )
        user32.DestroyMenu(menu)
        self._handle_command(command)

    def _handle_command(self, command: int) -> None:
        if not command:
            return
        controls = read_control()
        if command == self.CMD_PAUSE:
            update_control(paused=not bool(controls["paused"]))
        elif command == self.CMD_NATURAL:
            update_control(reply_style="natural")
        elif command == self.CMD_CUTE:
            update_control(reply_style="cute")
        elif command == self.CMD_CONCISE:
            update_control(reply_style="concise")
        elif command == self.CMD_WEB:
            update_control(web_search=not bool(controls["web_search"]))
        elif command == self.CMD_THINKING:
            update_control(thinking=not bool(controls["thinking"]))
        elif command == self.CMD_LOG:
            try:
                python = Path(sys.executable).with_name("python.exe")
                subprocess.Popen([
                    "powershell.exe", "-NoExit", "-Command",
                    f"& '{python}' '{C.ASSISTANT / 'records.py'}' view --stream agent_log --tail 200",
                ])
            except OSError:
                pass
        elif command == self.CMD_CLOSE:
            user32.DestroyWindow(self.hwnd)
        elif command == self.CMD_EXIT:
            request_stop()
            user32.DestroyWindow(self.hwnd)

    def _wndproc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.WM_TIMER:
                self._render()
                return 0
            if message == self.WM_ERASEBKGND:
                return 1
            if message == self.WM_LBUTTONDOWN:
                user32.ReleaseCapture()
                user32.SendMessageW(hwnd, self.WM_NCLBUTTONDOWN, self.HTCAPTION, 0)
                return 0
            if message == self.WM_LBUTTONDBLCLK:
                controls = read_control()
                update_control(paused=not bool(controls["paused"]))
                return 0
            if message == self.WM_RBUTTONUP:
                self._show_menu()
                return 0
            if message == self.WM_DESTROY:
                user32.KillTimer(hwnd, 1)
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:
            self._log_error(exc)
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    @staticmethod
    def _log_error(exc: Exception) -> None:
        try:
            from secure_records import secure_log
            detail = traceback.format_exc()
            secure_log(
                "widget_log",
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(exc).__name__}: {exc}\n{detail}",
            )
        except Exception:
            pass

    def run(self) -> None:
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def close(self) -> None:
        if self.mem_dc and self.old_bitmap:
            gdi32.SelectObject(self.mem_dc, self.old_bitmap)
        if self.bitmap:
            gdi32.DeleteObject(self.bitmap)
        if self.mem_dc:
            gdi32.DeleteDC(self.mem_dc)
        if self.screen_dc:
            user32.ReleaseDC(None, self.screen_dc)
        if self.class_name:
            user32.UnregisterClassW(self.class_name, self.hinstance)


def _render_preview(path: Path) -> None:
    from PIL import Image, ImageDraw
    phases = ("idle", "listening", "thinking", "searching", "sending", "happy", "error")
    size = WIDGET_SIZE
    sheet = Image.new("RGBA", (size * len(phases), size + 24), (12, 13, 18, 255))
    renderer = ProceduralOrb(size)
    for index, phase in enumerate(phases):
        mode = 10 if phase == "searching" else 9 if phase in {"sending", "happy"} else 5
        frame = Image.fromarray(renderer.render(phase, mode, 2.7), "RGBA")
        sheet.alpha_composite(frame, (index * size, 0))
        ImageDraw.Draw(sheet).text((index * size + 43, size + 4), phase,
                                   fill=(225, 228, 238, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def _benchmark() -> None:
    renderer = ProceduralOrb(WIDGET_SIZE)
    started = time.perf_counter()
    for index in range(120):
        renderer.render("thinking", 4, index / 30.0)
    elapsed = time.perf_counter() - started
    print(f"120 frames: {elapsed:.3f}s | {120 / elapsed:.1f} FPS render capacity")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--render-preview":
        _render_preview(Path(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == "--benchmark":
        _benchmark()
    elif _claim_singleton():
        widget = None
        try:
            widget = SayloWidgetV3()
            widget.run()
        except Exception as exc:
            SayloWidgetV3._log_error(exc)
        finally:
            if widget is not None:
                widget.close()
            WIDGET_PID.unlink(missing_ok=True)
