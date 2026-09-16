# -*- coding: utf-8 -*-
"""探测消息条目内部结构,找出区分自己/对方的依据。"""
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

def dump(c, d=0, maxd=5):
    for ch in c.GetChildren():
        nm = (ch.Name or "").strip().replace("\n", " / ")
        r = ch.BoundingRectangle
        print(f"{'  '*d}<{ch.ControlTypeName.replace('Control','')}> {ch.ClassName} | {nm[:50]} @L{r.left} W{r.width()} H{r.height()}")
        if d < maxd:
            dump(ch, d + 1, maxd)

w = find_wx()
w.SetActive(); time.sleep(1.0)
lst = find_by_class(w, "mmui::RecyclerListView")
if not lst:
    print("没找到消息列表"); sys.exit(1)

items = lst.GetChildren()
print(f"消息列表 @L{lst.BoundingRectangle.left} W{lst.BoundingRectangle.width()},共 {len(items)} 条\n")
for it in items:
    nm = (it.Name or "").strip().replace("\n", " / ")
    r = it.BoundingRectangle
    print(f"=== {it.ClassName} | {nm[:50]} @L{r.left} W{r.width()}")
    dump(it, 1, 3)
    print()
