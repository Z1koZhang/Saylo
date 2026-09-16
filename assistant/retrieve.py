# -*- coding: utf-8 -*-
"""混合检索:BM25(关键词) + 稠密向量(语义),分数各自归一化后加权。

    from retrieve import Retriever
    r = Retriever()
    r.memory("我和向志益聊过租房吗", k=6)
    r.style("晚上一起吃饭？", k=8)
"""
from __future__ import annotations

import json
import io
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
import secure_records as S
from indexer import BM25, tokenize


def _minmax(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    lo, hi = float(x.min()), float(x.max())
    return np.zeros_like(x) if hi - lo < 1e-9 else (x - lo) / (hi - lo)


class _Index:
    def __init__(self, name: str):
        self.name = name
        self.bm25 = BM25.load(C.STORE / name)
        self.meta = S.read_records(S.SECURE_DIR / f"{name}_meta.senc",
                                   f"{name}_meta")
        vp = S.SECURE_DIR / f"{name}_vecs.bin"
        self.vecs = (np.load(io.BytesIO(S.read_secure_bytes(vp, f"{name}_vecs")))
                     if vp.exists() else None)

    def search(self, query: str, k: int, alpha: float) -> list[dict]:
        sparse = _minmax(self.bm25.scores(tokenize(query)))
        if self.vecs is not None:
            from indexer import embed
            qv = embed([query], is_query=True)[0]
            dense = _minmax(self.vecs @ qv)
            score = alpha * dense + (1 - alpha) * sparse
        else:
            score = sparse
        if not score.any():
            return []
        k = min(k, len(score))
        top = np.argpartition(-score, k - 1)[:k]
        top = top[np.argsort(-score[top])]
        out = []
        for i in top:
            if score[i] <= 0:
                continue
            r = dict(self.meta[i])
            r["_score"] = round(float(score[i]), 4)
            out.append(r)
        return out


class Retriever:
    def __init__(self):
        self._cache: dict[str, _Index] = {}

    def _idx(self, name: str) -> _Index:
        if name not in self._cache:
            self._cache[name] = _Index(name)
        return self._cache[name]

    @property
    def has_dense(self) -> bool:
        return self._idx("memory").vecs is not None

    def memory(self, query: str, k: int | None = None) -> list[dict]:
        return self._idx("memory").search(query, k or C.TOP_K_MEM, C.HYBRID_ALPHA)

    def style(self, query: str, k: int | None = None, chat: str | None = None) -> list[dict]:
        """检索“在类似情境下我是怎么回的”。chat 非空时优先同一个人的样本。"""
        k = k or C.TOP_K_STYLE
        hits = self._idx("style").search(query, k * 3, C.HYBRID_ALPHA)
        if chat:
            same = [h for h in hits if h.get("chat") == chat]
            rest = [h for h in hits if h.get("chat") != chat]
            hits = same + rest
        return hits[:k]


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "租房"
    r = Retriever()
    print(f"稠密向量: {'已启用' if r.has_dense else '未启用(仅 BM25)'}\n")
    print(f"===== 记忆库 “{q}” =====")
    for h in r.memory(q):
        print(f"[{h['_score']}] {h['chat']} {h['time_from'][:10]}~{h['time_to'][:10]}")
        print("   " + h["text"][:160].replace("\n", " / "))
    print(f"\n===== 语气库 “{q}” =====")
    for h in r.style(q):
        print(f"[{h['_score']}] ({h['chat']} {h['time'][:10]})")
        print(f"   对方: {h['context'][:60]}")
        print(f"   我  : {h['reply'][:80]}")
