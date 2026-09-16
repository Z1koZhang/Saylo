# -*- coding: utf-8 -*-
"""看图。把微信里的图片消息截下来,交给 DeepSeek 的视觉模型描述成文字,
再把文字塞给聊天模型去回复。

用的是同一个 API key、同一个 /chat/completions 端点,不需要另外申请。

两个实测得到的坑:

1. **`deepseek-v4-flash-vision-exp` 是带推理的视觉模型**,会先花掉一大截 completion token 做
   reasoning。max_tokens 给小了(比如 120),推理就把预算吃光,正文返回空字符串,
   而且 HTTP 依然是 200。所以这里给 800。

2. **图片只能放在 user 消息里**,放 system 或 assistant 会直接 400。
"""
from __future__ import annotations

import base64
import hashlib
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
import secure_records as S

_SYS = """看这张图,用一两句话说清楚里面有什么。这是别人在微信里发来的图片。

- 图里有文字就把文字念出来(聊天截图、通知、菜单、题目)
- 是照片就说拍的是什么(地点、食物、人在做什么、什么东西)
- 是表情包或梗图就说画的什么、大概什么情绪
- 说不清就说"看不太清"

只描述你看到的,不要评论,不要猜拍照的人想表达什么。用中文,别超过两句。"""


def describe(image_path: str | Path, timeout: int = 60) -> str:
    """描述一张图片。失败返回空串——看不了图不该让聊天崩掉。"""
    p = Path(image_path)
    if not p.exists() or p.stat().st_size == 0:
        return ""
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp"}.get(
                p.suffix.lower().lstrip("."), "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode()

    payload = {
        "model": getattr(C, "VISION_MODEL", "deepseek-v4-flash-vision-exp"),
        # 这个模型会先做 reasoning,给少了正文就是空的(HTTP 仍然 200)
        "max_tokens": getattr(C, "VISION_MAX_TOKENS", 800),
        "temperature": 0.2,
        "messages": [{
            "role": "user",                     # 图片只能放 user,放别的会 400
            "content": [
                {"type": "text", "text": _SYS},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    }
    last = ""
    for attempt in range(2):
        try:
            r = requests.post(
                f"{C.DEEPSEEK_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {C.api_key()}",
                         "Content-Type": "application/json"},
                json=payload, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                choice = data["choices"][0]
                message = choice["message"]
                try:
                    S.append_record(
                        S.SECURE_DIR / "reasoning.senc",
                        "reasoning",
                        {"ts": time.time(), "kind": "vision",
                         "request": {"model": payload["model"],
                                     "image_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                                     "prompt": _SYS},
                         "response": {"content": message.get("content"),
                                      "reasoning_content": message.get("reasoning_content"),
                                      "finish_reason": choice.get("finish_reason"),
                                      "usage": data.get("usage")}},
                    )
                except Exception:
                    pass
                txt = (message.get("content") or "").strip()
                if txt:
                    return txt
                last = "模型只输出了推理,没有正文(max_tokens 可能不够)"
            else:
                last = f"HTTP {r.status_code}: {r.text[:120]}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(1.5)
    print(f"  [看图失败] {last}")
    return ""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python assistant/vision.py <图片路径>")
    t0 = time.time()
    out = describe(sys.argv[1])
    print(f"({time.time() - t0:.1f}s) {out or '(没看出来)'}")
