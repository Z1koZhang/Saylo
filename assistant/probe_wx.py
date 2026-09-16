# -*- coding: utf-8 -*-
"""微信 UI 自动化可行性探测。

微信 4.1 只对“启用过无障碍模式的账号”暴露 UI 树。这个脚本判断你的号
属于哪种,决定 wx_bridge 能不能做。

运行前:打开并登录微信,让主窗口停在前台。
    python assistant/probe_wx.py
"""
from __future__ import annotations

import sys

import uiautomation as auto

MAIN_CLASS = "mmui::MainWindow"
SHELL_CLASS = "Qt51514QWindowIcon"
WANTED = ("WeChatMainWndForPC", MAIN_CLASS, SHELL_CLASS)


def walk(ctrl, depth=0, maxdepth=4, budget=[400]):
    """浅层遍历控件树,统计能看到多少有名字的元素。"""
    rows = []
    if depth > maxdepth or budget[0] <= 0:
        return rows
    for child in ctrl.GetChildren():
        budget[0] -= 1
        if budget[0] <= 0:
            break
        name = (child.Name or "").strip()
        rows.append((depth, child.ControlTypeName, child.ClassName, name[:40]))
        rows += walk(child, depth + 1, maxdepth, budget)
    return rows


def main() -> None:
    auto.uiautomation.DEBUG_SEARCH_TIME = False
    print("扫描顶层窗口 ...\n")

    root = auto.GetRootControl()
    wins = []
    for w in root.GetChildren():
        cls, name = w.ClassName or "", (w.Name or "").strip()
        if any(k.lower() in cls.lower() for k in WANTED) or "微信" in name or "Weixin" in name:
            wins.append(w)
        if cls or name:
            print(f"  [{cls}] {name[:50]}")

    print()
    if not wins:
        print("!! 没找到微信窗口。确认微信已启动并登录,窗口没有最小化到托盘。")
        sys.exit(1)

    ok = False
    shell_found = False
    for w in wins:
        print("=" * 60)
        print(f"窗口: ClassName={w.ClassName}  Name={w.Name}")
        rows = walk(w)
        named = [r for r in rows if r[3]]
        print(f"子元素总数(浅层): {len(rows)}   其中有名字的: {len(named)}")
        for d, ct, cn, nm in rows[:35]:
            print("   " + "  " * d + f"<{ct}> {cn} | {nm}")
        if (w.ClassName or "") == MAIN_CLASS:
            ok = any(cn == "mmui::MainTabBar" for _, _, cn, _ in rows)
        elif (w.ClassName or "") == SHELL_CLASS and (w.Name or "").strip() == "Weixin":
            shell_found = True

    print("\n" + "=" * 60)
    if ok:
        print("结论: UI 树可见 ✅  你的账号能做 UI 自动化,wx_bridge 可以继续。")
    elif shell_found:
        print("结论: 微信进程已启动，但当前只向 UI Automation 暴露 Qt 空壳。")
        print("      这不代表账号未登录；Saylo 目前无法读取聊天控件树。")
    else:
        print("结论: UI 树是空壳 ❌  控件树没有可用元素。")
        print("      说明你的账号未启用过无障碍模式,微信 4.1 屏蔽了 UIA。")
        print("      可选:降级微信到 3.9.x / 改用 OCR+坐标点击 / 放弃自动收发。")


if __name__ == "__main__":
    main()
