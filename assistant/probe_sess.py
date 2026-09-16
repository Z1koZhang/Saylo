# -*- coding: utf-8 -*-
"""探测会话列表单元格内部:未读徽章、联系人名、最后消息。"""
import time
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

def dump(c, d=1, maxd=5):
    for ch in c.GetChildren():
        nm = (ch.Name or "").strip().replace("\n", " / ")
        r = ch.BoundingRectangle
        print(f"{'  '*d}<{ch.ControlTypeName.replace('Control','')}> {ch.ClassName} | {nm[:45]} @L{r.left} W{r.width()} H{r.height()}")
        if d < maxd:
            dump(ch, d + 1, maxd)

w = find_wx(); w.SetActive(); time.sleep(1.0)
tv = find_by_class(w, "mmui::XTableView")
cells = tv.GetChildren()
print(f"会话列表共 {len(cells)} 个可见单元格\n")
for c in cells:
    nm = (c.Name or "").strip().replace("\n", " | ")
    print(f"=== {c.ClassName} | {nm[:70]}")
    dump(c)
    print()
