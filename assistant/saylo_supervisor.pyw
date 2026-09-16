# -*- coding: utf-8 -*-
"""Saylo 进程监护器：等待后台就绪、启动挂件，并在异常退出时自动恢复一次。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = HERE / "store"
SUPERVISOR_PID = STORE / "supervisor.pid"
AGENT_PID = STORE / "saylo.pid"
STOP_FILE = STORE / "saylo.stop"
STATUS_FILE = STORE / "runtime_status.json"
LOG_FILE = HERE / "supervisor.log"
AGENT_ENTRY = HERE / "saylo_background.pyw"
READY_TIMEOUT_SEC = 30.0
RESTART_LIMIT = 1
ENVIRONMENT_RETRY_SEC = 20.0
KEY_RETRY_INITIAL_SEC = 5.0
KEY_RETRY_MAX_SEC = 60.0
EXIT_ENVIRONMENT_UNAVAILABLE = 2
EXIT_KEY_UNAVAILABLE = 3
sys.path.insert(0, str(HERE))
from process_utils import pid_running
from secure_records import secure_log


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    try:
        secure_log("supervisor_log", line)
    except Exception:
        # 加密未初始化或密钥损坏时绝不退回明文日志。
        pass


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def launch_widget(widget_name: str) -> None:
    widget = (HERE / Path(widget_name).name).resolve()
    if widget.parent != HERE or not widget.is_file():
        log(f"[挂件失败] 非法或不存在的入口: {widget_name}")
        return
    subprocess.Popen(
        [sys.executable, str(widget)], cwd=str(HERE),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log(f"[挂件启动] {widget.name}")


def spawn_agent() -> subprocess.Popen:
    stale = read_pid(AGENT_PID)
    if stale and not pid_running(stale):
        AGENT_PID.unlink(missing_ok=True)
        log(f"[清理] 过期后台 PID {stale}")
    env = os.environ.copy()
    env["SAYLO_SUPERVISED"] = "1"
    child = subprocess.Popen(
        [sys.executable, str(AGENT_ENTRY)], cwd=str(HERE), env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log(f"[后台启动] PID={child.pid}")
    return child


def status_ready(started_at: float) -> bool:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        heartbeat = float(data.get("heartbeat_at", data.get("updated_at", 0.0)))
        status_pid = int(data.get("pid", 0))
        recorded_pid = read_pid(AGENT_PID)
        return (status_pid > 0 and status_pid == recorded_pid and
                pid_running(status_pid) and heartbeat >= started_at - 1.0)
    except (OSError, ValueError, TypeError):
        return False


def wait_ready(child: subprocess.Popen) -> bool:
    started_at = time.time()
    deadline = time.monotonic() + READY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        if status_ready(started_at):
            log(f"[后台就绪] worker PID={read_pid(AGENT_PID)} | launcher PID={child.pid}")
            return True
        if STOP_FILE.exists():
            return False
        time.sleep(0.25)
    log(f"[后台超时] PID={child.pid} 在 {READY_TIMEOUT_SEC:.0f} 秒内没有状态心跳")
    return False


def mark_disconnected(detail: str) -> None:
    try:
        sys.path.insert(0, str(HERE))
        from runtime_state import mark_disconnected as write_disconnected
        write_disconnected(detail)
    except Exception as exc:
        log(f"[状态写入失败] {exc}")


def key_retry_delay(failures: int) -> float:
    """DPAPI 暂不可用时指数退避，最终每分钟检查一次。"""
    exponent = max(0, min(int(failures) - 1, 8))
    return min(KEY_RETRY_MAX_SEC, KEY_RETRY_INITIAL_SEC * (2 ** exponent))


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--widget", default="desktop_widget_v3.pyw")
    args = parser.parse_args()
    STORE.mkdir(parents=True, exist_ok=True)

    old_supervisor = read_pid(SUPERVISOR_PID)
    if old_supervisor and pid_running(old_supervisor):
        # 已有监护器时，重复打开启动器只负责补开挂件，不重启回复后台。
        if not STOP_FILE.exists():
            launch_widget(args.widget)
        return 0

    SUPERVISOR_PID.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    SUPERVISOR_PID.write_text(str(os.getpid()), encoding="utf-8")
    log(f"监护器启动 PID={os.getpid()} | widget={Path(args.widget).name}")

    widget_started = False
    restarts = 0
    key_failures = 0
    child: subprocess.Popen | None = None
    try:
        while True:
            if STOP_FILE.exists():
                log("[停止] 重启前收到停止请求")
                break
            child = spawn_agent()
            ready = wait_ready(child)
            if ready and not widget_started:
                launch_widget(args.widget)
                widget_started = True
            elif not ready and child.poll() is None and not widget_started:
                mark_disconnected("后台初始化超时，请从挂件菜单查看加密运行日志")
                launch_widget(args.widget)
                widget_started = True

            while child.poll() is None:
                time.sleep(0.5)

            code = child.returncode
            intentional = STOP_FILE.exists()
            if intentional:
                log(f"[正常停止] 后台退出码={code}")
                break

            if code == EXIT_ENVIRONMENT_UNAVAILABLE:
                # 微信没打开、在托盘或暂未暴露 UIA 时保持监护，不消耗崩溃重启额度。
                detail = "暂时没有检测到微信聊天窗口；打开微信后会自动重试"
                mark_disconnected(detail)
                if not widget_started:
                    launch_widget(args.widget)
                    widget_started = True
                log(f"[等待微信] {ENVIRONMENT_RETRY_SEC:.0f} 秒后重试")
                deadline = time.monotonic() + ENVIRONMENT_RETRY_SEC
                while time.monotonic() < deadline and not STOP_FILE.exists():
                    time.sleep(0.5)
                continue

            if code == EXIT_KEY_UNAVAILABLE:
                key_failures += 1
                wait_sec = key_retry_delay(key_failures)
                detail = "Windows 用户加密服务尚未就绪；Saylo 正在等待解锁记录"
                mark_disconnected(detail)
                if not widget_started:
                    launch_widget(args.widget)
                    widget_started = True
                log(f"[等待解锁] 第 {key_failures} 次，{wait_sec:.0f} 秒后重试")
                deadline = time.monotonic() + wait_sec
                while time.monotonic() < deadline and not STOP_FILE.exists():
                    time.sleep(0.5)
                continue

            log(f"[异常退出] 后台 PID={child.pid} | 退出码={code}")
            if restarts >= RESTART_LIMIT:
                detail = f"后台连续异常退出，最后退出码 {code}；请从挂件菜单查看加密运行日志"
                mark_disconnected(detail)
                if not widget_started:
                    launch_widget(args.widget)
                    widget_started = True
                break

            restarts += 1
            mark_disconnected("后台意外退出，正在自动恢复")
            AGENT_PID.unlink(missing_ok=True)
            log(f"[自动恢复] 第 {restarts}/{RESTART_LIMIT} 次，2 秒后重启")
            time.sleep(2.0)
    finally:
        SUPERVISOR_PID.unlink(missing_ok=True)
        AGENT_PID.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        log("监护器退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
