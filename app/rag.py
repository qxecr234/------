# -*- coding: utf-8 -*-
"""法规检索：中文二元组 BM25。

设计取舍
--------
* 法规库只有几十条，用向量库是过度设计，而且会引入不可控的语义漂移。
* BM25 是确定性的、可解释的：命中哪几个词、哪一条、得分多少，都能打印出来给评委看。
* 中文不分词，用二元组（bigram）切分，对"小规模纳税人""备注栏""进项转出"这类术语召回很稳。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9.%‰]+")

# 检索时给不同字段的权重：标题和关键词比正文更能代表"这条讲什么"
FIELD_WEIGHTS = {"title": 3, "kw": 2, "tags": 2, "text": 1}


def tokenize(text: str) -> list[str]:
    """把中英混排文本切成检索词。中文按二元组，英文数字按整词。"""
    tokens: list[str] = []
    for m in CJK_RUN.finditer(text.lower()):
        s = m.group()
        if re.match(r"[\u4e00-\u9fff]", s):
            if len(s) == 1:
                tokens.append(s)
            else:
                tokens.extend(s[i:i + 2] for i in range(len(s) - 1))
        else:
            tokens.append(s)
    return tokens


class RegulationIndex:
    """法规库索引。加载 JSONL，建倒排表，提供 top-k 检索。"""

    def __init__(self, path: str | Path, k1: float = 1.4, b: float = 0.72):
        self.path = Path(path)
        self.k1 = k1
        self.b = b
        self.docs: list[dict] = []
        self.tf: list[dict[str, int]] = []
        self.doc_len: list[int] = []
        self.df: dict[str, int] = {}
        self.avg_len = 0.0
        self._load()
        self._build()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self.docs.append(json.loads(line))

    def _field_blob(self, doc: dict) -> str:
        parts = []
        for field, weight in FIELD_WEIGHTS.items():
            raw = doc.get(field, "")
            if isinstance(raw, list):
                raw = " ".join(raw)
            parts.append((str(raw) + " ") * weight)
        return "".join(parts)

    def _build(self) -> None:
        total = 0
        for doc in self.docs:
            counts: dict[str, int] = {}
            toks = tokenize(self._field_blob(doc))
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            self.doc_len.append(len(toks))
            total += len(toks)
            for t in counts:
                self.df[t] = self.df.get(t, 0) + 1
        self.avg_len = total / max(len(self.docs), 1)

    def _idf(self, term: str) -> float:
        n = len(self.docs)
        df = self.df.get(term, 0)
        # BM25 标准 idf，加 1 保证非负
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores: list[float] = []
        for i, counts in enumerate(self.tf):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * self.doc_len[i] / max(self.avg_len, 1))
            for t in q_tokens:
                f = counts.get(t, 0)
                if not f:
                    continue
                score += self._idf(t) * (f * (self.k1 + 1)) / (f + norm)
            # 标签/关键词整词命中给一次额外加分，避免长句里被稀释
            tags = " ".join(self.docs[i].get("tags", [])) + " " + self.docs[i].get("kw", "")
            bonus = sum(1.6 for t in set(q_tokens) if len(t) >= 2 and t in tags)
            scores.append(score + bonus)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out = []
        for idx in ranked[:top_k]:
            if scores[idx] <= 0:
                continue
            doc = dict(self.docs[idx])
            doc["_score"] = round(scores[idx], 3)
            out.append(doc)
        return out

    def get(self, reg_id: str) -> dict | None:
        for d in self.docs:
            if d["id"] == reg_id:
                return d
        return None


# 判定"检索不到依据"的绝对阈值。低于它直接拒答，而不是拿最像的条文硬凑。
MIN_EVIDENCE_SCORE = 5.0


def has_evidence(hits: list[dict]) -> bool:
    return bool(hits) and hits[0]["_score"] >= MIN_EVIDENCE_SCORE
