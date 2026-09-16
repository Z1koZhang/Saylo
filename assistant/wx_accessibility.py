# -*- coding: utf-8 -*-
"""微信 4.1 冷启动后的 UI Automation 控件树恢复。

微信退出或系统重启后，Qt accessibility gate 会回到关闭状态，UIA 只能
看到 ``Qt51514QWindowIcon`` 空壳。本模块只支持经过明确验证的微信版本，
只改运行中 Weixin.dll 的一个字节；不修改磁盘文件。写后必须由调用方验证
mmui 控件树已经出现，否则立即恢复原值。

RVA 与机制参考（Apache-2.0）：
https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/uia_driver.py
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 只允许精确命中的已验证版本。微信升级后宁可失败，也不猜地址。
GATE_RVA_BY_VERSION = {
    "4.1.13.65": 0x0AE2B0C8,
}


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


@dataclass(frozen=True)
class ActivationResult:
    ok: bool
    detail: str


def _pid_from_hwnd(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(
        wintypes.HWND(hwnd), ctypes.byref(pid)
    )
    return int(pid.value)


def _process_modules(pid: int) -> Iterator[tuple[int, str, str]]:
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k32.Module32FirstW.restype = wintypes.BOOL
    k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k32.Module32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if snap == INVALID_HANDLE_VALUE:
        return
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not k32.Module32FirstW(snap, ctypes.byref(entry)):
            return
        while True:
            base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
            yield int(base), entry.szModule, entry.szExePath
            if not k32.Module32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)


def _weixin_dll(pid: int) -> tuple[int, str] | None:
    for base, name, path in _process_modules(pid):
        if name.lower() == "weixin.dll":
            return base, path
    return None


def _read_byte(handle, address: int) -> int | None:
    k32 = ctypes.windll.kernel32
    k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.ReadProcessMemory.restype = wintypes.BOOL
    buf = ctypes.c_ubyte()
    read = ctypes.c_size_t()
    ok = k32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), ctypes.byref(buf), 1,
        ctypes.byref(read)
    )
    return int(buf.value) if ok and read.value == 1 else None


def _write_byte(handle, address: int, value: int) -> bool:
    k32 = ctypes.windll.kernel32
    k32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.WriteProcessMemory.restype = wintypes.BOOL
    buf = ctypes.c_ubyte(value)
    written = ctypes.c_size_t()
    ok = k32.WriteProcessMemory(
        handle, ctypes.c_void_p(address), ctypes.byref(buf), 1,
        ctypes.byref(written)
    )
    return bool(ok and written.value == 1)


def activate_qt_accessibility(
    hwnd: int,
    verifier: Callable[[], bool],
    timeout: float = 3.0,
) -> ActivationResult:
    """激活指定微信窗口的无障碍 gate，并验证/回滚。"""
    pid = _pid_from_hwnd(hwnd)
    if not pid:
        return ActivationResult(False, "无法从微信窗口取得进程 ID")

    module = _weixin_dll(pid)
    if module is None:
        return ActivationResult(False, "微信主进程中没有找到 Weixin.dll")
    base, dll_path = module
    version = Path(dll_path).parent.name
    rva = GATE_RVA_BY_VERSION.get(version)
    if rva is None:
        return ActivationResult(False, f"微信版本 {version} 尚未验证，拒绝猜测内存地址")

    access = (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
              PROCESS_VM_WRITE | PROCESS_VM_OPERATION)
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    handle = k32.OpenProcess(access, False, pid)
    if not handle:
        return ActivationResult(False, f"无法以所需权限打开微信进程 PID {pid}")

    address = int(base) + int(rva)
    try:
        original = _read_byte(handle, address)
        if original not in (0, 1):
            return ActivationResult(
                False, f"目标字节校验失败（值={original!r}），未执行写入"
            )
        wrote = False
        if original == 0:
            if not _write_byte(handle, address, 1):
                return ActivationResult(False, "写入无障碍状态字节失败")
            wrote = True

        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            try:
                if verifier():
                    return ActivationResult(
                        True,
                        f"已恢复微信 UIA 控件树（PID {pid}, Weixin.dll+0x{rva:X}）",
                    )
            except Exception:
                pass
            time.sleep(0.2)

        if wrote:
            _write_byte(handle, address, original)
        return ActivationResult(False, "写入后未检测到 mmui 控件树，已恢复原字节")
    finally:
        k32.CloseHandle(handle)
