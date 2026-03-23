from __future__ import annotations

import hashlib
import re
from typing import Optional


class MessageDeduplicator:
    """消息去重与质量评分。

    - content_hash: 对 cleaned_text 做 SHA-256 指纹
    - quality_score: 基于消息长度、有效词比例等计算 0-1 质量分
    """

    # 泰语 Unicode 范围
    _THAI_PATTERN = re.compile(r"[\u0e00-\u0e7f]")
    # 纯表情/特殊符号
    _EMOJI_PATTERN = re.compile(
        r"[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff"
        r"\U0001f1e0-\U0001f1ff\U00002702-\U000027b0\U0001f900-\U0001f9ff"
        r"\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff\U00002600-\U000026ff]"
    )
    _URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
    _MENTION_PATTERN = re.compile(r"<@!?\d+>|<#\d+>|<@&\d+>")

    def compute_hash(self, text: Optional[str]) -> Optional[str]:
        """对文本计算 SHA-256 哈希。"""
        if not text or not text.strip():
            return None
        normalized = text.strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def compute_quality_score(self, content: str, cleaned_text: Optional[str] = None, tokens: Optional[list] = None) -> float:
        """计算消息质量评分 (0.0 - 1.0)。

        评分规则：
        - 消息太短 (< 3字符): 0.1
        - 纯URL: 0.1
        - 纯表情: 0.1
        - 纯mention: 0.1
        - 有效泰语内容: 按比例计分
        - 有分词结果: 加分
        """
        if not content or not content.strip():
            return 0.0

        text = content.strip()

        # 去除 URL 和 mention 后看剩余内容
        stripped = self._URL_PATTERN.sub("", text)
        stripped = self._MENTION_PATTERN.sub("", stripped).strip()

        if len(stripped) < 3:
            return 0.1

        # 纯表情检查
        no_emoji = self._EMOJI_PATTERN.sub("", stripped).strip()
        if not no_emoji:
            return 0.1

        score = 0.3  # 基础分

        # 长度加分 (最多 +0.2)
        length_bonus = min(len(stripped) / 100, 0.2)
        score += length_bonus

        # 泰语字符比例加分 (最多 +0.3)
        thai_chars = len(self._THAI_PATTERN.findall(stripped))
        if len(stripped) > 0:
            thai_ratio = thai_chars / len(stripped)
            score += min(thai_ratio * 0.5, 0.3)

        # 有 tokens 加分 (+0.2)
        if tokens and len(tokens) >= 2:
            score += 0.2

        return round(min(score, 1.0), 2)
