# -*- coding: utf-8 -*-
"""Saylo 后台与桌面挂件之间的轻量状态/控制通道。

只写运行阶段、模式编号和开关，不写任何聊天正文。未来把挂件移到主系统时，
可以把这层 JSON 文件替换成共享目录或本地 WebSocket，Agent 本身不用重写。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import config as C

STATUS_FILE = C.STORE / "runtime_status.json"
CONTROL_FILE = C.STORE / "runtime_control.json"
STOP_FILE = C.STORE / "saylo.stop"
HEARTBEAT_INTERVAL_SEC = 3.0

DEFAULT_CONTROL = {
    "paused": False,
    "web_search": True,
    "thinking": True,
    "reply_style": "cute",
}
_WRITE_LOCK = threading.Lock()


def _read_json(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except (OSError, ValueError, TypeError):
        return dict(default)


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Agent 主循环、提醒线程和控件可能在同一 PID 内靠得很近地写状态。旧临时文件名只含
    # PID，会互相覆盖并触发 WinError 5；线程号和随机后缀让每次写入都有独立目标。
    tmp = path.with_suffix(
        path.suffix + f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    with _WRITE_LOCK:
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def read_control() -> dict:
    data = DEFAULT_CONTROL | _read_json(CONTROL_FILE, DEFAULT_CONTROL)
    if data.get("reply_style") not in {"natural", "cute", "concise"}:
        data["reply_style"] = "cute"
    return data


def read_status() -> dict:
    return _read_json(STATUS_FILE, {"phase": "stopped", "label": "未运行"})


def update_control(**changes) -> dict:
    data = read_control()
    for key in DEFAULT_CONTROL:
        if key in changes:
            data[key] = changes[key]
    _atomic_write(CONTROL_FILE, data)
    return data


def request_stop() -> None:
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()


class RuntimeBridge:
    def __init__(self):
        self._last_status: tuple | None = None
        self._current_status: dict | None = None
        self._status_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        if not CONTROL_FILE.exists():
            _atomic_write(CONTROL_FILE, DEFAULT_CONTROL)

    def controls(self) -> dict:
        return read_control()

    def set(self, phase: str, mode: int | None = None,
            label: str = "", detail: str = "") -> None:
        if mode is None and phase == "sending":
            with self._status_lock:
                if self._current_status is not None:
                    mode = self._current_status.get("mode")
        key = (phase, mode, label, detail)
        now = time.time()
        with self._status_lock:
            if key == self._last_status and self._current_status is not None:
                return
            self._last_status = key
            self._current_status = {
                "phase": phase,
                "mode": mode,
                "label": label,
                "detail": detail,
                "updated_at": now,
                "heartbeat_at": now,
                "pid": os.getpid(),
            }
            payload = dict(self._current_status)
        _atomic_write(STATUS_FILE, payload)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL_SEC):
            with self._status_lock:
                if self._current_status is None:
                    continue
                self._current_status["heartbeat_at"] = time.time()
                payload = dict(self._current_status)
            try:
                _atomic_write(STATUS_FILE, payload)
            except OSError:
                # 临时文件争用或系统关机时下一次心跳会重试；不能让心跳线程拖垮回复。
                continue

    def start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="saylo-runtime-heartbeat", daemon=True,
        )
        self._heartbeat_thread.start()

    def close(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)

    def stopped(self) -> None:
        self.set("stopped", label="已退出")


def mark_disconnected(detail: str = "后台进程未运行") -> None:
    """供外部监护器在重启失败后留下不可误判的离线状态。"""
    now = time.time()
    _atomic_write(STATUS_FILE, {
        "phase": "error",
        "mode": None,
        "label": "后台已断开",
        "detail": detail,
        "updated_at": now,
        "heartbeat_at": 0.0,
        "pid": 0,
    })
