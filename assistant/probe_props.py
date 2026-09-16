# -*- coding: utf-8 -*-
"""列出消息条目的全部 UIA 属性,找发送方标识。"""
import sys, time
import uiautomation as auto

def find_wx():
    for w in auto.GetRootControl().GetChildren():
        if (w.ClassName or "") == "mmui::MainWindow":
            return w

def find_by_class(root, cls, maxd=16, budget=[8000]):
    stack = [(root, 0)]
    while stack:
        c, d = stack.pop()
        if budget[0] <= 0 or d > maxd:
            continue
        for ch in c.GetChildren():
            budget[0] -= 1
            if (ch.ClassName or "") == cls:
                return ch
            stack.append((ch, d + 1))

w = find_wx(); w.SetActive(); time.sleep(1.0)
lst = find_by_class(w, "mmui::RecyclerListView")
items = [i for i in lst.GetChildren() if "Text" in i.ClassName or "Item" in i.ClassName]
print(f"共 {len(items)} 条\n")

PROPS = ["AutomationId", "Name", "ClassName", "LocalizedControlType", "HelpText",
         "AcceleratorKey", "AccessKey", "ItemType", "ItemStatus", "FrameworkId",
         "IsOffscreen", "Orientation", "Culture", "AriaRole", "AriaProperties"]
for it in items[:6]:
    print(f"--- {it.ClassName} | {(it.Name or '')[:40]}")
    for p in PROPS:
        try:
            v = getattr(it, p, None)
            if v not in (None, "", 0, False):
                print(f"     {p} = {v!r}")
        except Exception:
            pass
    # 支持的模式
    pats = []
    for pname in dir(auto.PatternId):
        if pname.endswith("Pattern"):
            try:
                if it.GetPattern(getattr(auto.PatternId, pname)):
                    pats.append(pname)
            except Exception:
                pass
    print(f"     支持模式: {pats}")
    r = it.BoundingRectangle
    print(f"     矩形: L{r.left} R{r.right} W{r.width()}")
