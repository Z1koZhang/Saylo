# -*- coding: utf-8 -*-
"""深挖微信主窗口控件树,定位会话列表/消息列表/输入框。"""
import sys
import uiautomation as auto

MAXD = int(sys.argv[1]) if len(sys.argv) > 1 else 12
BUDGET = [3000]

def find_wx():
    for w in auto.GetRootControl().GetChildren():
        if (w.ClassName or "") == "mmui::MainWindow":
            return w
    return None

def walk(c, d=0, path=""):
    if d > MAXD or BUDGET[0] <= 0:
        return
    for ch in c.GetChildren():
        BUDGET[0] -= 1
        if BUDGET[0] <= 0:
            return
        cn = ch.ClassName or ""
        nm = (ch.Name or "").strip().replace("\n", " ")
        ct = ch.ControlTypeName.replace("Control", "")
        try:
            r = ch.BoundingRectangle
            box = f"{r.left},{r.top},{r.width()}x{r.height()}"
        except Exception:
            box = "?"
        print(f"{'  ' * d}<{ct}> {cn} | {nm[:45]} @{box}")
        walk(ch, d + 1)

w = find_wx()
if not w:
    print("没找到 mmui::MainWindow"); sys.exit(1)
print(f"主窗口 {w.BoundingRectangle}")
walk(w)
print(f"\n(剩余预算 {BUDGET[0]})")
