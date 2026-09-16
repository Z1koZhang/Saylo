# -*- coding: utf-8 -*-
"""交互式测试台。

    python assistant/cli.py           # 助手模式,问它任何关于你的事
    python assistant/cli.py --reply   # 代回模式,输入“对方说的话”,看它怎么替你回

命令: /reply 切代回模式  /ask 切助手模式  /who <会话名> 指定对话人
      /mem <词> 只看检索结果  /clear 清空上下文  /q 退出
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
from brain import Assistant


def main() -> None:
    mode = "reply" if "--reply" in sys.argv else "ask"
    chat = None
    history: list[dict] = []

    print("载入索引 ...")
    a = Assistant()
    dense = "混合(向量+BM25)" if a.r.has_dense else "仅 BM25"
    print(f"检索: {dense} | 模式: {mode} | 输入 /q 退出, /? 看命令\n")

    while True:
        try:
            line = input(f"[{mode}{'@' + chat if chat else ''}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/q", "/quit", "/exit"):
            break
        if line == "/?":
            print(__doc__)
            continue
        if line == "/reply":
            mode = "reply"; print("-> 代回模式"); continue
        if line == "/ask":
            mode = "ask"; print("-> 助手模式"); continue
        if line == "/clear":
            history.clear(); print("-> 上下文已清空"); continue
        if line.startswith("/who"):
            chat = line[4:].strip() or None
            print(f"-> 对话人: {chat or '(未指定)'}"); continue
        if line.startswith("/mem"):
            for h in a.r.memory(line[4:].strip()):
                print(f"  [{h['_score']}] {h['chat']} {h['time_from'][:10]}")
                print("    " + h["text"][:200].replace("\n", " / "))
            continue

        try:
            if mode == "ask":
                out = a.ask(line, history=history, verbose=True)
                history += [{"role": "user", "content": line},
                            {"role": "assistant", "content": out}]
            else:
                out = a.reply(line, chat=chat, verbose=True)
        except Exception as e:
            print(f"  !! {e}\n")
            continue

        print()
        if mode == "reply":
            for i, seg in enumerate(s.strip() for s in out.split("\n---\n")):
                if seg:
                    print(f"  发送{i + 1}: {seg}")
        else:
            print("  " + out.replace("\n", "\n  "))
        print()


if __name__ == "__main__":
    main()
