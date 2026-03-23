from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Dict, List, Optional, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .translator import ThaiChineseTranslator

logger = logging.getLogger(__name__)


class TopicClusterer:
    """将扁平关键词列表聚合成 3-5 个话题组。

    算法：
    1. 构建关键词共现矩阵（同一条消息中共同出现的关键词视为相关）
    2. 用贪心社区发现将关键词分成 3-5 组
    3. 每组选 TF-IDF 最高的词作为代表词
    """

    def __init__(self, max_clusters: int = 5, min_cluster_size: int = 2):
        self.max_clusters = max_clusters
        self.min_cluster_size = min_cluster_size

    def cluster(
        self,
        keywords: List[Dict[str, object]],
        messages: List[object],
        translator: Optional["ThaiChineseTranslator"] = None,
    ) -> List[Dict[str, object]]:
        """执行话题聚类。

        Args:
            keywords: TF-IDF 分析结果 [{keyword, tfidf_score, frequency}]
            messages: 原始消息对象列表
            translator: 翻译器（可选，用于生成中文话题标题）

        Returns:
            话题组列表
        """
        if not keywords or len(keywords) < self.min_cluster_size:
            return []

        top_keywords = keywords[: self.max_clusters * 10]
        kw_set = {str(item["keyword"]) for item in top_keywords}

        cooccurrence = self._build_cooccurrence(messages, kw_set)
        clusters = self._greedy_cluster(top_keywords, cooccurrence)

        # 翻译关键词
        all_thai = [str(kw["keyword"]) for kw in top_keywords]
        translations: Dict[str, str] = {}
        if translator:
            translations = translator.translate_keywords(all_thai)

        # 构建输出
        result = []
        for idx, cluster_kws in enumerate(clusters):
            if len(cluster_kws) < self.min_cluster_size:
                continue

            kw_list = []
            for kw_data in cluster_kws:
                thai = str(kw_data["keyword"])
                kw_list.append({
                    "keyword_thai": thai,
                    "keyword_cn": translations.get(thai, thai),
                    "tfidf": float(kw_data.get("tfidf_score", 0)),
                    "frequency": int(kw_data.get("frequency", 0)),
                })

            total_freq = sum(k["frequency"] for k in kw_list)
            max_tfidf = max(k["tfidf"] for k in kw_list) if kw_list else 0

            # 话题标题
            keywords_cn = [k["keyword_cn"] for k in kw_list[:5]]
            title_cn = "、".join(keywords_cn[:3])
            if translator:
                try:
                    title_cn = translator.name_topic(keywords_cn)
                except Exception:
                    pass

            result.append({
                "topic_id": idx + 1,
                "title_cn": title_cn,
                "keywords": kw_list,
                "message_count": total_freq,
                "heat_score": round(min(max_tfidf * 2, 1.0), 2),
            })

        result.sort(key=lambda x: x["heat_score"], reverse=True)
        return result[: self.max_clusters]

    def _build_cooccurrence(self, messages: List[object], kw_set: set) -> Dict[str, Counter]:
        """构建关键词共现矩阵。"""
        cooccurrence: Dict[str, Counter] = defaultdict(Counter)

        for msg in messages:
            tokens = getattr(msg, "tokens", None)
            if not isinstance(tokens, list):
                continue

            msg_keywords = [t for t in tokens if str(t) in kw_set]
            for i, kw1 in enumerate(msg_keywords):
                for kw2 in msg_keywords[i + 1 :]:
                    k1, k2 = str(kw1), str(kw2)
                    cooccurrence[k1][k2] += 1
                    cooccurrence[k2][k1] += 1

        return cooccurrence

    def _greedy_cluster(
        self,
        keywords: List[Dict[str, object]],
        cooccurrence: Dict[str, Counter],
    ) -> List[List[Dict[str, object]]]:
        """贪心聚类：从 TF-IDF 最高的词开始，将共现频率高的词归入同组。"""
        assigned: set = set()
        clusters: List[List[Dict[str, object]]] = []

        sorted_keywords = sorted(keywords, key=lambda x: float(x.get("tfidf_score", 0)), reverse=True)

        for seed in sorted_keywords:
            seed_kw = str(seed["keyword"])
            if seed_kw in assigned:
                continue
            if len(clusters) >= self.max_clusters:
                break

            cluster = [seed]
            assigned.add(seed_kw)

            neighbors = cooccurrence.get(seed_kw, Counter())
            sorted_neighbors = neighbors.most_common(8)

            for neighbor_kw, count in sorted_neighbors:
                if neighbor_kw in assigned or count < 1:
                    continue
                for kw_data in sorted_keywords:
                    if str(kw_data["keyword"]) == neighbor_kw:
                        cluster.append(kw_data)
                        assigned.add(neighbor_kw)
                        break

            clusters.append(cluster)

        # 把未分配的关键词归入最相关的组
        for kw_data in sorted_keywords:
            kw = str(kw_data["keyword"])
            if kw in assigned:
                continue

            best_cluster = -1
            best_score = 0
            for ci, cluster in enumerate(clusters):
                score = sum(
                    cooccurrence.get(kw, Counter()).get(str(ck["keyword"]), 0) for ck in cluster
                )
                if score > best_score:
                    best_score = score
                    best_cluster = ci

            if best_cluster >= 0 and best_score > 0:
                clusters[best_cluster].append(kw_data)
                assigned.add(kw)

        return clusters
