from __future__ import annotations

import re
from typing import Dict, List, Optional


class TextCleaner:
    URL_PATTERN = re.compile(r"https?://\S+")
    MENTION_PATTERN = re.compile(r"<@!?(\d+)>|@\w+")
    CHANNEL_PATTERN = re.compile(r"<#\d+>")
    CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```")
    INLINE_CODE_PATTERN = re.compile(r"`[^`]+`")
    REPEAT_PATTERN = re.compile(r"(.)\1{2,}")
    EXTRA_SPACE_PATTERN = re.compile(r"\s+")

    def __init__(
        self,
        remove_urls: bool = True,
        remove_mentions: bool = True,
        remove_special_chars: bool = False,
        collapse_repeats: bool = True,
        min_token_length: int = 1,
    ):
        self.remove_urls = remove_urls
        self.remove_mentions = remove_mentions
        self.remove_special_chars = remove_special_chars
        self.collapse_repeats = collapse_repeats
        self.min_token_length = min_token_length

    def clean(self, text: str) -> str:
        if not text:
            return ""

        value = text
        value = self.CODE_BLOCK_PATTERN.sub(" ", value)
        value = self.INLINE_CODE_PATTERN.sub(" ", value)

        if self.remove_urls:
            value = self.URL_PATTERN.sub(" ", value)
        if self.remove_mentions:
            value = self.MENTION_PATTERN.sub(" ", value)
            value = self.CHANNEL_PATTERN.sub(" ", value)

        if self.collapse_repeats:
            value = self.REPEAT_PATTERN.sub(r"\1\1", value)

        value = self.EXTRA_SPACE_PATTERN.sub(" ", value).strip()
        return value

    def process(self, text: str, tokens: Optional[List[str]] = None) -> Dict[str, object]:
        cleaned_text = self.clean(text)
        token_list = [t for t in (tokens or cleaned_text.split()) if len(t) >= self.min_token_length]
        return {
            "cleaned_text": cleaned_text,
            "tokens": token_list,
            "tokens_json": token_list,
        }
