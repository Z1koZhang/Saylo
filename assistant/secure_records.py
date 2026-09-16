# -*- coding: utf-8 -*-
"""Saylo 私密记录的认证加密存储。

数据使用随机 256 位主密钥逐条 AES-256-GCM 加密。主密钥有两份包装：
- 用户口令经 Argon2id 派生后包装，供 records.py 交互查看和恢复；
- Windows DPAPI 包装，供同一 Windows 用户下的 Saylo 后台自动写入。

口令、派生密钥和主密钥都不会写成明文文件。运行时只在进程内存中短暂持有主密钥。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

HERE = Path(__file__).resolve().parent
STORE = HERE / "store"
SECURE_DIR = STORE / "secure"
KEY_META = STORE / "records.key.json"
_LOCAL_APPDATA = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
)
LEGACY_DPAPI_KEY = _LOCAL_APPDATA / "Saylo" / "records.key.dpapi"
# 无人值守启动进程在部分 VM 登录链路中看不到 LocalAppData；DPAPI 密文本身可安全
# 与其他密文放在 store 中，且发布脚本不会复制 store 内容。
DPAPI_KEY = STORE / "records.key.dpapi"

MAGIC = b"SAYLOENC1"
AAD_PREFIX = b"saylo-record-v1:"
KDF_MEMORY_KIB = 64 * 1024
KDF_ITERATIONS = 3
KDF_LANES = 4

_KEY_CACHE: bytes | None = None
_KEY_LOCK = threading.Lock()
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


class SecureRecordsError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).lower()
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.Lock())


def _derive_wrap_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 16:
        raise SecureRecordsError("记录口令至少需要 16 个字符")
    return Argon2id(
        salt=salt,
        length=32,
        iterations=KDF_ITERATIONS,
        lanes=KDF_LANES,
        memory_cost=KDF_MEMORY_KIB,
    ).derive(passphrase.encode("utf-8"))


def configured_dpapi_path() -> Path:
    """Use the absolute path persisted at setup, independent of startup env vars."""
    if KEY_META.exists():
        try:
            configured = json.loads(KEY_META.read_text(encoding="utf-8")).get("dpapi_path")
            if configured:
                path = Path(str(configured))
                if path.is_absolute():
                    return path
        except (OSError, ValueError, TypeError):
            pass
    return DPAPI_KEY


def relocate_dpapi_key(target: Path | None = None) -> Path:
    """Move the DPAPI-wrapped master key to a runtime-visible encrypted store."""
    target = target or DPAPI_KEY
    source = configured_dpapi_path()
    if not source.exists():
        raise SecureRecordsError(f"原 DPAPI 密钥文件不存在：{source}")
    blob = source.read_bytes()
    key = _dpapi_unprotect(blob)
    if len(key) != 32:
        raise SecureRecordsError("原 DPAPI 密钥内容无效")
    _atomic_write(target, blob)
    if _dpapi_unprotect(target.read_bytes()) != key:
        target.unlink(missing_ok=True)
        raise SecureRecordsError("DPAPI 密钥迁移校验失败")
    meta = json.loads(KEY_META.read_text(encoding="utf-8"))
    meta["dpapi_path"] = str(target.resolve())
    _atomic_write(KEY_META, json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
    return target


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_crypto() -> tuple[Any, Any]:
    """Return Win32 crypto functions with pointer-safe ctypes signatures."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(data: bytes) -> tuple[_DATA_BLOB, Any]:
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecureRecordsError("DPAPI 仅能在 Windows 上使用")
    source, keepalive = _blob(data)
    target = _DATA_BLOB()
    crypt32, kernel32 = _windows_crypto()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source), "Saylo records master key", None, None, None,
        0x1, ctypes.byref(target),
    )
    del keepalive
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecureRecordsError("DPAPI 仅能在 Windows 上使用")
    source, keepalive = _blob(data)
    target = _DATA_BLOB()
    crypt32, kernel32 = _windows_crypto()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(target),
    )
    del keepalive
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def initialize(passphrase: str) -> bytes:
    """新建主密钥及双重包装。已有配置时拒绝覆盖。"""
    if KEY_META.exists() or DPAPI_KEY.exists():
        raise SecureRecordsError("记录加密已经初始化，拒绝覆盖现有密钥")
    master = AESGCM.generate_key(bit_length=256)
    salt, nonce = os.urandom(16), os.urandom(12)
    wrap_key = _derive_wrap_key(passphrase, salt)
    wrapped = AESGCM(wrap_key).encrypt(nonce, master, b"saylo-master-key-v1")
    dpapi = _dpapi_protect(master)
    meta = {
        "version": 1,
        "kdf": "argon2id",
        "memory_kib": KDF_MEMORY_KIB,
        "iterations": KDF_ITERATIONS,
        "lanes": KDF_LANES,
        "salt": _b64(salt),
        "wrap": "AES-256-GCM",
        "nonce": _b64(nonce),
        "wrapped_key": _b64(wrapped),
        "dpapi_path": str(DPAPI_KEY),
    }
    _atomic_write(DPAPI_KEY, dpapi)
    try:
        _atomic_write(KEY_META, json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception:
        DPAPI_KEY.unlink(missing_ok=True)
        raise
    global _KEY_CACHE
    _KEY_CACHE = master
    return master


def unlock_with_passphrase(passphrase: str) -> bytes:
    try:
        meta = json.loads(KEY_META.read_text(encoding="utf-8"))
        salt = _unb64(meta["salt"])
        nonce = _unb64(meta["nonce"])
        wrapped = _unb64(meta["wrapped_key"])
        wrap_key = _derive_wrap_key(passphrase, salt)
        master = AESGCM(wrap_key).decrypt(nonce, wrapped, b"saylo-master-key-v1")
    except SecureRecordsError:
        raise
    except Exception as exc:
        raise SecureRecordsError("口令不正确，或密钥配置已损坏") from exc
    if len(master) != 32:
        raise SecureRecordsError("解锁后的数据密钥长度无效")
    return master


def runtime_key() -> bytes:
    """同一 Windows 用户下由 DPAPI 自动解锁，进程内缓存。"""
    global _KEY_CACHE
    with _KEY_LOCK:
        if _KEY_CACHE is not None:
            return _KEY_CACHE
        dpapi_path = configured_dpapi_path()
        if not KEY_META.exists():
            raise SecureRecordsError(f"记录密钥配置不存在：{KEY_META}")
        if not dpapi_path.exists():
            raise SecureRecordsError(f"DPAPI 密钥文件不存在：{dpapi_path}")
        try:
            key = _dpapi_unprotect(dpapi_path.read_bytes())
        except Exception as exc:
            raise SecureRecordsError("当前 Windows 用户无法解锁 Saylo 记录密钥") from exc
        if len(key) != 32:
            raise SecureRecordsError("DPAPI 返回的数据密钥无效")
        _KEY_CACHE = key
        return key


def seal_bytes(data: bytes, stream: str, key: bytes | None = None) -> bytes:
    key = key or runtime_key()
    nonce = os.urandom(12)
    aad = AAD_PREFIX + stream.encode("utf-8")
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, aad)


def open_bytes(blob: bytes, stream: str, key: bytes | None = None) -> bytes:
    key = key or runtime_key()
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 12 + 16:
        raise SecureRecordsError(f"{stream} 不是有效的 Saylo 加密数据")
    pos = len(MAGIC)
    nonce, ciphertext = blob[pos:pos + 12], blob[pos + 12:]
    try:
        return AESGCM(key).decrypt(
            nonce, ciphertext, AAD_PREFIX + stream.encode("utf-8")
        )
    except Exception as exc:
        raise SecureRecordsError(f"{stream} 解密或完整性校验失败") from exc


def write_secure_bytes(path: Path, stream: str, data: bytes,
                       key: bytes | None = None) -> None:
    _atomic_write(path, seal_bytes(data, stream, key))


def read_secure_bytes(path: Path, stream: str,
                      key: bytes | None = None) -> bytes:
    if not path.exists():
        raise FileNotFoundError(path)
    return open_bytes(path.read_bytes(), stream, key)


def write_json(path: Path, stream: str, value: Any,
               key: bytes | None = None) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    write_secure_bytes(path, stream, raw, key)


def read_json(path: Path, stream: str, default: Any = None,
              key: bytes | None = None) -> Any:
    if not path.exists():
        return default
    return json.loads(read_secure_bytes(path, stream, key).decode("utf-8"))


def encode_record(record: Any, stream: str, key: bytes | None = None) -> bytes:
    raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(seal_bytes(raw, stream, key)) + b"\n"


def append_record(path: Path, stream: str, record: Any,
                  key: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = encode_record(record, stream, key)
    with _file_lock(path), open(path, "ab") as handle:
        handle.write(line)
        handle.flush()


def write_records(path: Path, stream: str, records: Iterable[Any],
                  key: bytes | None = None) -> None:
    key = key or runtime_key()
    body = b"".join(encode_record(item, stream, key) for item in records)
    _atomic_write(path, body)


def read_records(path: Path, stream: str,
                 key: bytes | None = None) -> list[Any]:
    if not path.exists():
        return []
    key = key or runtime_key()
    out = []
    for number, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = open_bytes(base64.b64decode(line, validate=True), stream, key)
            out.append(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            raise SecureRecordsError(f"{path.name} 第 {number} 条记录损坏") from exc
    return out


def secure_log(stream: str, message: str, *, key: bytes | None = None) -> None:
    append_record(
        SECURE_DIR / f"{stream}.slog",
        stream,
        {"ts": datetime.now().timestamp(),
         "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "message": str(message)},
        key,
    )


def key_status() -> dict[str, Any]:
    dpapi_path = configured_dpapi_path()
    return {
        "initialized": KEY_META.exists() and dpapi_path.exists(),
        "key_meta": str(KEY_META),
        "dpapi_key": str(dpapi_path),
    }
