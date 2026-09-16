# -*- coding: utf-8 -*-
"""自检。改完代码跑一下,比 py_compile 能多抓一层错。

起因:用行号范围替换代码时,误删过整个方法(`_new_start`),
`py_compile` 完全查不出来——语法是对的,只是运行时才 AttributeError,
结果机器人每轮都崩,日志刷满 "'Agent' object has no attribute '_new_start'"。

    python assistant/selfcheck.py
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# 模块 -> 该模块里 self.xxx() 可能落在哪些类上
TARGETS = {
    "wx_agent": ["Agent"],
    "wx_bridge": ["WeChatUI"],
    "brain": ["Assistant", "DeepSeek"],
    "journal": ["Journal", "Memories", "Extractor"],
    "retrieve": ["Retriever"],
    "vault": [],
    "indexer": ["BM25"],
    "config": [],
}


def self_calls(path: pathlib.Path) -> set[str]:
    """源码里所有 self.xxx(...) 的属性名。"""
    out: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "self"):
            out.add(n.func.attr)
    return out


def main() -> int:
    here = pathlib.Path(__file__).resolve().parent
    bad = 0

    for mod, classes in TARGETS.items():
        path = here / f"{mod}.py"
        if not path.exists():
            print(f"  !! {mod}.py 不存在")
            bad += 1
            continue
        try:
            m = importlib.import_module(mod)
        except Exception as e:
            print(f"  !! {mod} 导入失败: {type(e).__name__}: {e}")
            bad += 1
            continue

        have: set[str] = set()
        for c in classes:
            if not hasattr(m, c):
                print(f"  !! {mod}.{c} 不存在")
                bad += 1
                continue
            have |= set(dir(getattr(m, c)))
        # 类里没有的,可能是实例属性(self.foo = ...),扫一遍赋值
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(n, ast.Assign)):
                for t in n.targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        have.add(t.attr)

        missing = sorted(c for c in self_calls(path) if c not in have)
        flag = "!!" if missing else "OK"
        print(f"  {flag} {mod:<12} self 调用 {len(self_calls(path)):2d} 处, "
              f"缺失 {missing or '无'}")
        bad += bool(missing)

    print()
    if bad:
        print(f"发现 {bad} 处问题")
    else:
        print("全部通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
