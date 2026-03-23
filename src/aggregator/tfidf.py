from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Iterable, List

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


# MARK: - TF-IDF Analyzer
# 关键词层只做轻量、可解释的统计，作为需求洞察的第一层信号。
class TFIDFAnalyzer:
    def __init__(
        self,
        max_features: int = 50,
        min_df: int = 2,
        max_df: float = 0.9,
        ngram_range: tuple[int, int] = (1, 2),
        min_frequency: int = 2,
    ):
        self.max_features = max_features
        self.min_df = min_df
        self.max_df = max_df
        self.ngram_range = ngram_range
        self.min_frequency = min_frequency
        self.block_tokens = {
            "ae",
            "rt",
            "http",
            "https",
            "www",
            "com",
            "co",
        }

        self.vectorizer = TfidfVectorizer(
            max_features=max_features,
            min_df=min_df,
            max_df=max_df,
            ngram_range=ngram_range,
            tokenizer=lambda text: text.split(),
            preprocessor=lambda text: text,
            token_pattern=None,
        )

    # MARK: - Main
    # 输入：已清洗文本列表
    # 输出：按 TF-IDF 排序后的关键词（附带词频）
    def analyze(self, texts: Iterable[str]) -> List[Dict[str, float]]:
        docs = [self._normalize_document(t) for t in texts if t and str(t).strip()]
        docs = [x for x in docs if x]
        if len(docs) < 2:
            return self._fallback_frequency(docs)

        try:
            matrix = self.vectorizer.fit_transform(docs)
            terms = self.vectorizer.get_feature_names_out()
            mean_tfidf = np.asarray(matrix.mean(axis=0)).flatten()
        except ValueError:
            # 典型场景：min_df/max_df 剪枝后无可用词项，回退到词频模式避免中断。
            return self._fallback_frequency(docs)

        counter = Counter()
        for doc in docs:
            counter.update(doc.split())

        result = []
        for i, term in enumerate(terms):
            freq = int(counter.get(term, 0))
            if freq < self.min_frequency:
                continue
            result.append(
                {
                    "keyword": term,
                    "tfidf_score": float(mean_tfidf[i]),
                    "frequency": freq,
                }
            )

        result.sort(key=lambda x: (x["tfidf_score"], x["frequency"]), reverse=True)
        return result[: self.max_features]

    def _normalize_document(self, text: str) -> str:
        tokens = [x for x in str(text).split() if x and x.strip()]
        filtered = [t for t in tokens if self._is_informative_token(t)]
        return " ".join(filtered).strip()

    def _is_informative_token(self, token: str) -> bool:
        t = token.strip()
        if not t:
            return False
        if len(t) < 2:
            return False
        if t.lower() in self.block_tokens:
            return False
        if t.isdigit():
            return False
        if re.fullmatch(r"[a-zA-Z]{1,2}", t):
            return False
        if re.fullmatch(r"[a-zA-Z0-9_]+", t) and len(t) < 3:
            return False
        # 至少包含一个字母（拉丁或泰文）时才保留，减少噪声符号进入关键词。
        if not re.search(r"[A-Za-z\u0E00-\u0E7F]", t):
            return False
        return True

    def _fallback_frequency(self, docs: Iterable[str]) -> List[Dict[str, float]]:
        counter = Counter()
        for doc in docs:
            if not doc:
                continue
            counter.update(doc.split())

        total = sum(counter.values())
        if total <= 0:
            return []

        result = []
        for term, freq in counter.items():
            if int(freq) < self.min_frequency:
                continue
            result.append(
                {
                    "keyword": term,
                    "tfidf_score": float(freq / total),
                    "frequency": int(freq),
                }
            )
        result.sort(key=lambda x: (x["frequency"], x["tfidf_score"]), reverse=True)
        return result[: self.max_features]
