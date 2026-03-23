from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage import Database

from .llm_summary import HierarchicalSummarizer

logger = logging.getLogger(__name__)


class ThaiChineseTranslator:
    """缓存优先的泰中翻译服务。

    流程:
    1. 查 keyword_translations 缓存表
    2. 缓存未命中的词批量调 LLM 翻译
    3. 翻译结果写回缓存表
    """

    def __init__(self, db: "Database", summarizer: Optional[HierarchicalSummarizer] = None):
        self.db = db
        self.summarizer = summarizer

    # MARK: - Keyword Translation
    def translate_keywords(self, keywords_thai: List[str]) -> Dict[str, str]:
        """批量翻译泰语关键词，返回 {thai: cn} 映射。"""
        if not keywords_thai:
            return {}

        unique_keywords = list(set(k.strip() for k in keywords_thai if k.strip()))
        cached = self.db.get_keyword_translations(unique_keywords)

        result: Dict[str, str] = {}
        missing: List[str] = []

        for kw in unique_keywords:
            if kw in cached:
                result[kw] = cached[kw]
            else:
                missing.append(kw)

        if missing and self.summarizer:
            translated = self._batch_translate_via_llm(missing)
            for thai, cn in translated.items():
                result[thai] = cn
                self.db.upsert_keyword_translation(
                    keyword_thai=thai,
                    keyword_cn=cn,
                )

        for kw in unique_keywords:
            if kw not in result:
                result[kw] = kw

        return result

    def _batch_translate_via_llm(self, keywords: List[str]) -> Dict[str, str]:
        """调 LLM 批量翻译关键词。"""
        provider = self.summarizer._resolve_provider()
        if not provider:
            return {}

        # 分批翻译，每批最多 25 个
        result: Dict[str, str] = {}
        for i in range(0, len(keywords), 25):
            batch = keywords[i : i + 25]
            translated = self._translate_batch(provider, batch)
            result.update(translated)

        return result

    def _translate_batch(self, provider: Dict[str, str], keywords: List[str]) -> Dict[str, str]:
        prompt = json.dumps(
            {
                "task": "将以下泰语词翻译成中文",
                "output_format": "JSON数组: [{\"thai\": \"原词\", \"cn\": \"中文翻译\", \"category\": \"分类\"}]",
                "words": keywords,
            },
            ensure_ascii=False,
        )

        text = self.summarizer._call_llm(
            provider=provider,
            system_prompt="你是泰中翻译专家。严格输出JSON数组，不要输出其他内容。每个词给出最常用的中文翻译。",
            user_prompt=prompt,
            max_output_tokens=600,
        )

        if not text:
            return {}

        parsed = self.summarizer._extract_json_array(text)
        if not parsed:
            return {}

        result: Dict[str, str] = {}
        for item in parsed:
            thai = str(item.get("thai", "")).strip()
            cn = str(item.get("cn", "")).strip()
            if thai and cn:
                result[thai] = cn
                category = str(item.get("category", "")).strip() or None
                if category:
                    self.db.upsert_keyword_translation(
                        keyword_thai=thai,
                        keyword_cn=cn,
                        category=category,
                    )

        return result

    # MARK: - Summary Translation
    def translate_summary(self, text: str) -> str:
        """将泰语/混合语摘要翻译为纯中文。"""
        if not text or not self.summarizer:
            return text

        provider = self.summarizer._resolve_provider()
        if not provider:
            return text

        result = self.summarizer._call_llm(
            provider=provider,
            system_prompt="你是泰中翻译专家。将以下文本翻译成流畅的中文。保持原意，不要添加额外内容。",
            user_prompt=text,
            max_output_tokens=800,
        )
        return result.strip() if result else text

    # MARK: - Topic Naming
    def name_topic(self, keywords_cn: List[str], sample_messages: Optional[List[str]] = None) -> str:
        """为话题聚类生成中文标题（5-10字）。"""
        if not keywords_cn or not self.summarizer:
            return "、".join(keywords_cn[:3]) if keywords_cn else "其他话题"

        provider = self.summarizer._resolve_provider()
        if not provider:
            return "、".join(keywords_cn[:3])

        prompt = json.dumps(
            {
                "task": "根据以下关键词生成一个5-10字的中文话题标题",
                "keywords": keywords_cn[:10],
                "sample_messages": (sample_messages or [])[:5],
                "output": "只输出标题文字，不要其他内容",
            },
            ensure_ascii=False,
        )

        result = self.summarizer._call_llm(
            provider=provider,
            system_prompt="你是产品分析师。根据关键词给出简短的话题标题。只输出标题，不要多余内容。",
            user_prompt=prompt,
            max_output_tokens=50,
        )
        return result.strip().strip('"').strip("'") if result else "、".join(keywords_cn[:3])
