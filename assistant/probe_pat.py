# -*- coding: utf-8 -*-
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
for it in lst.GetChildren():
    print(f"--- {it.ClassName} | {(it.Name or '')[:40]!r}")
    try:
        v = it.GetValuePattern()
        print("   Value.Value   =", repr(v.Value))
        print("   Value.ReadOnly=", v.IsReadOnly)
    except Exception as e:
        print("   ValuePattern:", e)
    try:
        g = it.GetLegacyIAccessiblePattern()
        for f in ("Name", "Value", "Description", "Help", "Role", "State",
                  "DefaultAction", "KeyboardShortcut", "ChildId"):
            try:
                print(f"   Legacy.{f:16s}=", repr(getattr(g, f)))
            except Exception:
                pass
    except Exception as e:
        print("   Legacy:", e)
    print()
