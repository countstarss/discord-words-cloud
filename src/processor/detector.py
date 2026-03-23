from __future__ import annotations

import re
from typing import Tuple


class LanguageDetector:
    """Lightweight Thai language detector for chat messages."""

    THAI_PATTERN = re.compile(r"[\u0E00-\u0E7F]")

    def __init__(self, min_confidence: float = 0.2):
        self.min_confidence = min_confidence

    def has_thai_chars(self, text: str) -> bool:
        return bool(self.THAI_PATTERN.search(text or ""))

    def detect(self, text: str) -> Tuple[str, float]:
        if not text:
            return "unknown", 0.0

        thai_char_count = len(self.THAI_PATTERN.findall(text))
        ratio = thai_char_count / max(len(text), 1)

        if ratio >= self.min_confidence:
            return "th", ratio
        return "other", ratio
