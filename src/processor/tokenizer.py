from __future__ import annotations

from typing import List


class ThaiTokenizer:
    def __init__(self, engine: str = "newmm", keep_whitespace: bool = False):
        self.engine = engine
        self.keep_whitespace = keep_whitespace
        try:
            from pythainlp.tokenize import word_tokenize

            self._word_tokenize = word_tokenize
        except Exception:
            self._word_tokenize = None

    def tokenize(self, text: str) -> List[str]:
        if not text:
            return []
        if self._word_tokenize is None:
            return [x for x in text.split() if x.strip()]

        tokens = self._word_tokenize(text, engine=self.engine, keep_whitespace=self.keep_whitespace)
        return [t for t in tokens if t and t.strip()]
