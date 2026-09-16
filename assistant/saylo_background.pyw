# -*- coding: utf-8 -*-
"""供隐藏启动器调用；pythonw 不显示终端，运行记录写入认证加密日志。"""
from pathlib import Path
import faulthandler
import json
import os
import sys
import traceback

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
PID_FILE = HERE / "store" / "saylo.pid"
STOP_FILE = HERE / "store" / "saylo.stop"
SUPERVISED = os.environ.get("SAYLO_SUPERVISED") == "1"
STARTUP_DIAGNOSTIC = HERE / "store" / "startup_diagnostic.json"
sys.path.insert(0, str(HERE))
from process_utils import pid_running
from secure_records import SecureRecordsError, secure_log

EXIT_ENVIRONMENT_UNAVAILABLE = 2
EXIT_KEY_UNAVAILABLE = 3


# 双击启动器两次时只保留一个 Saylo，避免重复自动回复。
try:
    old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
except (OSError, ValueError):
    old_pid = 0
if old_pid and pid_running(old_pid):
    sys.exit(0)

PID_FILE.parent.mkdir(parents=True, exist_ok=True)
STOP_FILE.unlink(missing_ok=True)
PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
# 原生崩溃转储无法边写边做 AEAD，因此不再落明文；可捕获的 Python 异常写入加密日志。
crash_stream = open(os.devnull, "w", encoding="utf-8")
faulthandler.enable(crash_stream, all_threads=True)
exit_code = 0
try:
    from wx_agent import Agent
    Agent(live=True).run(stop_if=STOP_FILE.exists)
except BaseException as exc:
    # pythonw 没有终端；未捕获异常会变成隐藏/难发现的系统弹窗。启动失败写进统一日志，
    # 让“微信没打开”等环境问题可以直接排查，并保证 PID 文件正常释放。
    from datetime import datetime
    try:
        secure_log(
            "crash_log",
            f"{datetime.now():%Y-%m-%d %H:%M:%S} [后台异常退出] "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
    except Exception:
        pass
    message = str(exc)
    # 环境暂不可用不是程序崩溃：交给监护器等待微信窗口重新出现。
    if isinstance(exc, SecureRecordsError):
        # Windows 登录早期 DPAPI 偶尔尚未能解锁当前用户主密钥。让监护器持续等待，
        # 不把短暂的用户加密上下文不可用误判成程序连续崩溃。
        exit_code = EXIT_KEY_UNAVAILABLE
    elif (
        isinstance(exc, RuntimeError) and
        ("没找到微信聊天主窗口" in message or "没有向 Windows UI Automation" in message)
    ):
        exit_code = EXIT_ENVIRONMENT_UNAVAILABLE
    else:
        exit_code = 1
    # 解锁失败时加密日志本身不可用。这里只保留无聊天正文、无密钥的启动诊断，
    # 供监护器和人工排障读取；下一次成功启动后立即删除。
    try:
        STARTUP_DIAGNOSTIC.write_text(
            json.dumps({
                "exception_type": type(exc).__name__,
                "message": message[:500],
                "exit_code": exit_code,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
finally:
    PID_FILE.unlink(missing_ok=True)
    # 受监护运行时由监护器读取并清理停止标记，借此区分主动退出与意外退出。
    if not SUPERVISED:
        STOP_FILE.unlink(missing_ok=True)
    crash_stream.close()

sys.exit(exit_code)
