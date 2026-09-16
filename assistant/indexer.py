# -*- coding: utf-8 -*-
"""建索引:记忆库(对话块) + 语气库(我的真实回复)。
BM25 用纯 numpy 实现,稠密向量用本地 bge。两者都可单独使用。

用法:
    python assistant/indexer.py            # 建全部
    python assistant/indexer.py --no-dense # 只建 BM25,不下载模型
"""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C
import secure_records as S

_ZH_STOP = set("的 了 是 我 你 他 她 它 在 有 就 不 也 都 和 与 及 而 啊 吧 呢 吗 嘛 呀 哦 噢 这 那 个 们 着 过 很 会 要 说 一个 什么 怎么 时候".split())
_TOKEN_RE = re.compile(r"[一-鿿]+|[a-zA-Z]+|\d+")


def tokenize(text: str) -> list[str]:
    import jieba
    out = []
    for seg in _TOKEN_RE.findall(text):
        if seg.isascii():
            if len(seg) > 1:
                out.append(seg.lower())
        else:
            out.extend(w for w in jieba.cut_for_search(seg) if len(w) > 1 or w.isdigit())
    return [w for w in out if w not in _ZH_STOP]


class BM25:
    """标准 BM25-Okapi,稀疏倒排表实现,2455 篇文档下毫秒级。"""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(docs_tokens)
        self.doc_len = np.array([len(d) for d in docs_tokens], dtype=np.float32)
        self.avgdl = float(self.doc_len.mean()) if self.N else 0.0
        # term -> (doc_ids array, tf array)
        postings: dict[str, list[tuple[int, int]]] = {}
        for i, toks in enumerate(docs_tokens):
            for term, tf in Counter(toks).items():
                postings.setdefault(term, []).append((i, tf))
        self.index: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.idf: dict[str, float] = {}
        for term, plist in postings.items():
            ids = np.fromiter((p[0] for p in plist), dtype=np.int32, count=len(plist))
            tfs = np.fromiter((p[1] for p in plist), dtype=np.float32, count=len(plist))
            self.index[term] = (ids, tfs)
            df = len(plist)
            self.idf[term] = math.log(1 + (self.N - df + 0.5) / (df + 0.5))

    # ---- 持久化:不用 pickle,避免类路径绑定,也避免加载不可信文件 ----
    def save(self, prefix: Path) -> None:
        terms = list(self.index.keys())
        offs, ids_all, tfs_all = [0], [], []
        for t in terms:
            ids, tfs = self.index[t]
            ids_all.append(ids)
            tfs_all.append(tfs)
            offs.append(offs[-1] + len(ids))
        buffer = io.BytesIO()
        np.savez_compressed(
            buffer,
            doc_len=self.doc_len,
            offsets=np.array(offs, dtype=np.int64),
            ids=np.concatenate(ids_all) if ids_all else np.zeros(0, np.int32),
            tfs=np.concatenate(tfs_all) if tfs_all else np.zeros(0, np.float32),
            idf=np.array([self.idf[t] for t in terms], dtype=np.float32),
            params=np.array([self.k1, self.b, self.N, self.avgdl], dtype=np.float64),
        )
        stream = prefix.name
        S.write_secure_bytes(S.SECURE_DIR / f"{stream}_bm25.bin",
                             f"{stream}_bm25", buffer.getvalue())
        S.write_json(S.SECURE_DIR / f"{stream}_terms.state",
                     f"{stream}_terms", terms)

    @classmethod
    def load(cls, prefix: Path) -> "BM25":
        stream = prefix.name
        raw = S.read_secure_bytes(S.SECURE_DIR / f"{stream}_bm25.bin",
                                  f"{stream}_bm25")
        z = np.load(io.BytesIO(raw))
        terms = S.read_json(S.SECURE_DIR / f"{stream}_terms.state",
                            f"{stream}_terms", default=[])
        o = cls.__new__(cls)
        k1, b, N, avgdl = z["params"]
        o.k1, o.b, o.N, o.avgdl = float(k1), float(b), int(N), float(avgdl)
        o.doc_len = z["doc_len"]
        offs, ids, tfs, idf = z["offsets"], z["ids"], z["tfs"], z["idf"]
        o.index = {t: (ids[offs[i]:offs[i + 1]], tfs[offs[i]:offs[i + 1]])
                   for i, t in enumerate(terms)}
        o.idf = {t: float(idf[i]) for i, t in enumerate(terms)}
        return o

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        s = np.zeros(self.N, dtype=np.float32)
        norm = self.k1 * (1 - self.b + self.b * self.doc_len / (self.avgdl or 1.0))
        for term in set(query_tokens):
            hit = self.index.get(term)
            if hit is None:
                continue
            ids, tfs = hit
            s[ids] += self.idf[term] * (tfs * (self.k1 + 1)) / (tfs + norm[ids])
        return s


# ---------------------------------------------------------------- 数据装载

def load_memory() -> list[dict]:
    """记忆库:RAG 块,原样用。"""
    recs = []
    with open(C.RAG_ALL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


# 恶毒表达。这些是本人跟特定朋友互怼时的说法,不代表他平时怎么说话,
# 更不该成为 Saylo 对他的语气。
_HARSH = re.compile(
    r"傻逼|煞笔|沙比|智障|脑残|神经病|有病|滚出|滚蛋|滚开|操你|草你|草泥马|"
    r"你妈|尼玛|nmsl|垃圾|废物|恶心|贱|蠢货|狗屎|屁话|闭嘴|死开|懒得理|"
    r"爱咋咋|去死|有毛病|神经|欠揍|耳屎")

_NOISE_REPLY = re.compile(r"^[嗯哦噢欸诶啊呀哈额恩唔好行对是的了吧呢吗。，,.!！?？~～\s]*$")


def load_style() -> list[dict]:
    """语气库:从 SFT 样本里挑出能代表我日常语气的回复。

    筛掉:太短(嗯/好/OK)、纯语气词、太长(转发长文)、重复。
    只保留最近 RECENT_DAYS_STYLE 天,因为说话风格会漂移。
    """
    cutoff = None
    if C.RECENT_DAYS_STYLE:
        cutoff = datetime.now() - timedelta(days=C.RECENT_DAYS_STYLE)
    seen: set[str] = set()
    out: list[dict] = []
    kept_old = 0
    dropped_chat = dropped_harsh = 0
    with open(C.SFT_ALL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            msgs = d.get("messages") or []
            if len(msgs) < 2 or msgs[-1].get("role") != "assistant":
                continue
            reply = (msgs[-1].get("content") or "").strip()
            # 上文里最后一句对方说的话
            ctx = ""
            for m in reversed(msgs[:-1]):
                if m.get("role") == "user":
                    ctx = (m.get("content") or "").strip()
                    break
            if not (C.STYLE_MIN_CHARS <= len(reply) <= C.STYLE_MAX_CHARS):
                continue
            if len(ctx) < C.STYLE_MIN_USER_CHARS:
                continue
            if _NOISE_REPLY.match(reply):
                continue
            if d.get("chat") in getattr(C, "STYLE_EXCLUDE_CHATS", ()):
                dropped_chat += 1
                continue
            if getattr(C, "STYLE_DROP_HARSH", True) and _HARSH.search(reply):
                dropped_harsh += 1
                continue
            try:
                ts = datetime.strptime(d["time"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if cutoff and ts < cutoff:
                kept_old += 1
                continue
            key = reply[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append({"chat": d.get("chat", ""), "time": d["time"],
                        "ts": int(ts.timestamp()), "context": ctx, "reply": reply})
    print(f"  语气库: 保留 {len(out)} 条")
    print(f"    丢弃: 超过 {C.RECENT_DAYS_STYLE} 天 {kept_old} 条 | "
          f"排除会话 {dropped_chat} 条 | 恶毒表达 {dropped_harsh} 条")
    return out


# ---------------------------------------------------------------- 向量

def embed(texts: list[str], is_query: bool = False) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    global _MODEL
    try:
        _MODEL
    except NameError:
        print(f"  载入 embedding 模型 {C.EMBED_MODEL} ...")
        # The model is provisioned during VM setup. Avoid network metadata
        # probes on every later load; restricted/offline networks otherwise
        # add several minutes of retries before using the cached files.
        _MODEL = SentenceTransformer(C.EMBED_MODEL, local_files_only=True)
    if is_query:
        texts = [C.EMBED_QUERY_PREFIX + t for t in texts]
    v = _MODEL.encode(texts, batch_size=C.EMBED_BATCH, show_progress_bar=len(texts) > 200,
                      normalize_embeddings=True, convert_to_numpy=True)
    return v.astype(np.float32)


# ---------------------------------------------------------------- 建库

def build(name: str, records: list[dict], text_of, dense: bool) -> None:
    print(f"[{name}] {len(records)} 条")
    texts = [text_of(r) for r in records]
    print("  分词 + BM25 ...")
    toks = [tokenize(t) for t in texts]
    bm25 = BM25(toks)
    C.STORE.mkdir(parents=True, exist_ok=True)
    bm25.save(C.STORE / name)
    S.write_records(S.SECURE_DIR / f"{name}_meta.senc", f"{name}_meta", records)
    if dense:
        print("  编码向量 ...")
        vecs = embed(texts)
        buffer = io.BytesIO()
        np.save(buffer, vecs)
        S.write_secure_bytes(S.SECURE_DIR / f"{name}_vecs.bin",
                             f"{name}_vecs", buffer.getvalue())
        print(f"  向量 {vecs.shape}")
    print(f"  -> {C.STORE}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-dense", action="store_true", help="只建 BM25,跳过向量")
    a = ap.parse_args()
    dense = not a.no_dense
    build("memory", load_memory(), lambda r: r["text"], dense)
    build("style", load_style(), lambda r: r["context"], dense)
    print("完成。")


if __name__ == "__main__":
    main()
