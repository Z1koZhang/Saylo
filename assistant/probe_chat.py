# -*- coding: utf-8 -*-
"""激活微信窗口后,重点探测会话列表与消息区是否填充。"""
import sys, time
import uiautomation as auto

def find_wx():
    for w in auto.GetRootControl().GetChildren():
        if (w.ClassName or "") == "mmui::MainWindow":
            return w
    return None

def find_by_class(root, cls, maxd=14, budget=[6000]):
    """深搜第一个 ClassName 匹配的控件。"""
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
    return None

def dump(c, d=0, maxd=6, budget=[1500]):
    if d > maxd or budget[0] <= 0:
        return
    for ch in c.GetChildren():
        budget[0] -= 1
        if budget[0] <= 0:
            return
        nm = (ch.Name or "").strip().replace("\n", " / ")
        r = ch.BoundingRectangle
        print(f"{'  ' * d}<{ch.ControlTypeName.replace('Control','')}> {ch.ClassName} | {nm[:60]} @{r.left},{r.top},{r.width()}x{r.height()}")
        dump(ch, d + 1, maxd, budget)

w = find_wx()
if not w:
    print("没找到微信窗口"); sys.exit(1)

print("激活微信窗口 ...")
try:
    w.SetActive(); w.SetFocus()
except Exception as e:
    print("  激活失败:", e)
time.sleep(1.5)

for cls in ("mmui::ChatSessionList", "mmui::ChatMessagePage", "mmui::ChatTitleBarMasterView"):
    print(f"\n{'='*60}\n{cls}")
    node = find_by_class(w, cls)
    if node is None:
        print("  未找到"); continue
    kids = node.GetChildren()
    print(f"  直接子元素: {len(kids)}")
    dump(node)
