# -*- coding: utf-8 -*-
"""API 密钥加密保管。

设计目标:把整个项目文件夹分享给别人时,对方拿不到你的 DeepSeek 密钥。

    密文  assistant/secrets.enc          <- 跟项目走,可以分享
    钥匙  %USERPROFILE%/.saylo/saylo.key <- **在项目外**,不要分享

钥匙刻意放在项目目录之外。这样你压缩 H:/Saylo 发给别人时,钥匙根本不在
压缩包里,想漏都漏不掉——这是这套东西唯一真正起作用的地方。

**它保护不了什么(别有错觉):**
- 程序运行时密钥必然是明文(在内存里、在发给 DeepSeek 的请求头里)。
- 能访问你这台电脑的人,密文和钥匙两个都能拿到,等于没加密。
- 真正的边界只有一条:**分享出去的那份没有钥匙**。

用法:
    python assistant/vault.py init          # 交互式录入密钥并加密
    python assistant/vault.py show          # 验证能否解开(只显示前后几位)
    python assistant/vault.py rotate        # 换新密钥
    python assistant/vault.py keypath       # 打印钥匙位置
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENC_FILE = Path(__file__).resolve().parent / "secrets.enc"
KEY_FILE = Path(os.environ.get("SAYLO_KEYFILE") or (Path.home() / ".saylo" / "saylo.key"))


# ---------------------------------------------------------------- 钥匙

def create_keyfile(overwrite: bool = False) -> Path:
    if KEY_FILE.exists() and not overwrite:
        return KEY_FILE
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    KEY_FILE.write_text(base64.b64encode(key).decode(), encoding="utf-8")
    try:                                   # 尽量收紧权限:只有当前用户可读
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    return KEY_FILE


def load_key() -> bytes:
    if not KEY_FILE.exists():
        raise SystemExit(
            f"找不到钥匙: {KEY_FILE}\n"
            "如果这份程序是别人给你的,你需要用自己的 DeepSeek 密钥:\n"
            "    python assistant/vault.py init")
    raw = base64.b64decode(KEY_FILE.read_text(encoding="utf-8").strip())
    if len(raw) != 32:
        raise SystemExit(f"钥匙文件损坏(应为 32 字节,实际 {len(raw)}): {KEY_FILE}")
    return raw


# ---------------------------------------------------------------- 加解密

def encrypt(secret: str) -> None:
    """AES-256-GCM。GCM 自带完整性校验,密文被改过会解密失败而不是给出垃圾。"""
    key = load_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, secret.encode("utf-8"), None)
    ENC_FILE.write_text(json.dumps({
        "alg": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct).decode(),
        "note": "钥匙在 ~/.saylo/saylo.key,不在本项目里。分享项目时不会带上它。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def decrypt() -> str:
    if not ENC_FILE.exists():
        raise FileNotFoundError(ENC_FILE)
    d = json.loads(ENC_FILE.read_text(encoding="utf-8"))
    key = load_key()
    try:
        pt = AESGCM(key).decrypt(base64.b64decode(d["nonce"]),
                                 base64.b64decode(d["ct"]), None)
    except Exception:
        raise SystemExit(
            "解密失败。密文和钥匙对不上——多半是这份程序来自别人,\n"
            "而钥匙是你自己的。用你自己的密钥重新初始化:\n"
            "    python assistant/vault.py init")
    return pt.decode("utf-8")


def available() -> bool:
    return ENC_FILE.exists() and KEY_FILE.exists()


# ---------------------------------------------------------------- 命令行

def _mask(s: str) -> str:
    return f"{s[:6]}...{s[-4:]}(共 {len(s)} 位)" if len(s) > 12 else "(太短)"


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "init"

    if cmd == "keypath":
        print(f"钥匙: {KEY_FILE}  {'存在' if KEY_FILE.exists() else '不存在'}")
        print(f"密文: {ENC_FILE}  {'存在' if ENC_FILE.exists() else '不存在'}")
        return

    if cmd == "show":
        print("解密成功:", _mask(decrypt()))
        return

    if cmd in ("init", "rotate"):
        import getpass
        k = getpass.getpass("粘贴 DeepSeek API key(输入不回显): ").strip()
        if not k.startswith("sk-") or len(k) < 20:
            raise SystemExit("看着不像 DeepSeek 密钥(应以 sk- 开头)")
        create_keyfile(overwrite=(cmd == "rotate" and "--newkey" in sys.argv))
        encrypt(k)
        print(f"已加密 -> {ENC_FILE}")
        print(f"钥匙   -> {KEY_FILE}")
        print("验证:", _mask(decrypt()))
        print("\n分享这个项目时,只要不带上钥匙文件,对方就用不了你的密钥。")
        return

    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
