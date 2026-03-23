from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections import Counter
from typing import Dict, List, Optional, TYPE_CHECKING

from ..common import get_secret_cipher

if TYPE_CHECKING:
    from ..storage import Database


# MARK: - Hierarchical LLM Summarizer
# 目标：避免把全量消息一次性喂给模型，改为“候选筛选 -> 分块摘要 -> 二次合并”。
class HierarchicalSummarizer:
    """Optional two-stage LLM summarizer for large chat windows."""

    def __init__(self, config: Optional[dict] = None, db: Optional["Database"] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("llm_enabled", False))
        self.default_model = cfg.get("llm_model", "gpt-4.1-mini")
        self.max_chunks = int(cfg.get("llm_max_chunks", 6))
        self.chunk_chars = int(cfg.get("llm_chunk_chars", 3200))
        self.db = db
        self.cipher = get_secret_cipher()

    # MARK: - Public API
    def summarize_window(
        self,
        texts: List[str],
        keywords: List[Dict[str, object]],
        demand_signals: List[Dict[str, object]],
        force: bool = False,
    ) -> Optional[Dict[str, object]]:
        if not self.enabled and not force:
            return None

        provider = self._resolve_provider()
        if not provider:
            return None

        candidates = [x.strip() for x in texts if x and x.strip()]
        if not candidates:
            return None

        ranked = self._rank_candidates(candidates, demand_signals)
        chunks = self._chunk_lines(ranked)
        if not chunks:
            return None

        chunk_outputs = []
        for idx, chunk in enumerate(chunks[: self.max_chunks], start=1):
            result = self._summarize_chunk(provider, idx, chunk)
            if result:
                chunk_outputs.append(result)

        if not chunk_outputs:
            return None

        final = self._merge_chunks(provider, chunk_outputs, keywords)
        return final

    def explain_keywords_in_chinese(
        self,
        keywords: List[Dict[str, object]],
        demand_signals: List[Dict[str, object]],
        recent_summaries: List[str],
        force: bool = True,
    ) -> Optional[str]:
        """用已启用的 LLM 供应商，把关键词/需求信号转换为中文可读洞察。"""
        if not self.enabled and not force:
            return None

        provider = self._resolve_provider()
        if not provider:
            return None

        payload = {
            "任务": "把泰语关键词和需求信号翻译并总结成中文洞察",
            "输出要求": [
                "仅输出中文",
                "120-220字",
                "先写主要讨论主题，再写用户需求，再给一句产品建议",
                "不得输出JSON",
            ],
            "关键词": keywords[:30],
            "需求信号": demand_signals[:20],
            "历史摘要": recent_summaries[:6],
        }
        text = self._call_llm(
            provider=provider,
            system_prompt=(
                "你是资深中文产品分析师。"
                "你会阅读泰语社群关键词并提炼成业务可执行洞察，避免空话。"
            ),
            user_prompt=json.dumps(payload, ensure_ascii=False),
            max_output_tokens=420,
        )
        if not text:
            return None
        return text.strip()

    # MARK: - Provider Resolution
    # 优先读取 Web 已配置并启用的供应商；兜底读取 OPENAI_API_KEY。
    def _resolve_provider(self) -> Optional[Dict[str, str]]:
        if self.db is not None:
            row = self.db.get_active_llm_provider()
            if row:
                key = self.cipher.decrypt(str(row.get("api_key_encrypted") or ""))
                if key:
                    return {
                        "provider": str(row.get("provider") or "custom"),
                        "provider_type": str(row.get("provider_type") or "openai_compatible"),
                        "api_key": key,
                        "base_url": str(row.get("base_url") or "") or None,
                        "model": str(row.get("model") or "") or self.default_model,
                    }

        env_key = os.getenv("OPENAI_API_KEY")
        if env_key:
            return {
                "provider": "openai",
                "provider_type": "openai_compatible",
                "api_key": env_key,
                "base_url": None,
                "model": self.default_model,
            }
        return None

    # MARK: - Candidate Selection
    # 先用规则分数筛出更可能包含需求表达的消息，降低后续 token 成本。
    def _rank_candidates(self, lines: List[str], demand_signals: List[Dict[str, object]]) -> List[str]:
        signal_words = [str(x.get("signal", "")).lower() for x in demand_signals]

        def score(line: str) -> int:
            low = line.lower()
            signal_hits = sum(1 for s in signal_words if s and s in low)
            return signal_hits * 3 + min(len(line), 200) // 40

        ranked = sorted(lines, key=score, reverse=True)
        return ranked[:500]

    # MARK: - Chunking
    # 按字符长度切块，尽量把每块控制在模型输入上限之内。
    def _chunk_lines(self, lines: List[str]) -> List[List[str]]:
        chunks: List[List[str]] = []
        current: List[str] = []
        current_len = 0

        for line in lines:
            ln = len(line) + 1
            if current and current_len + ln > self.chunk_chars:
                chunks.append(current)
                current = []
                current_len = 0
            current.append(line)
            current_len += ln

        if current:
            chunks.append(current)

        return chunks

    # MARK: - Parsing
    # 兼容模型返回 ```json fenced block 或纯 JSON 文本。
    def _extract_json(self, text: str) -> Optional[Dict[str, object]]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
            return None
        except Exception:
            return None

    def _extract_json_array(self, text: str) -> Optional[list]:
        """解析 JSON 数组，兼容 ```json fenced block。"""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            return None
        except Exception:
            return None

    # MARK: - Provider Clients
    # OpenAI-compatible（OpenAI/OpenRouter/DeepSeek/xAI）统一走 Responses API。
    def _call_openai_compatible(
        self,
        provider: Dict[str, str],
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> Optional[str]:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=provider["api_key"], base_url=provider.get("base_url") or None)
            resp = client.responses.create(
                model=provider.get("model") or self.default_model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_output_tokens=max_output_tokens,
            )
            text = getattr(resp, "output_text", None)
            if text:
                return str(text).strip()

            output = getattr(resp, "output", None) or []
            pieces: List[str] = []
            for item in output:
                content = getattr(item, "content", None) or []
                for c in content:
                    t = getattr(c, "text", None)
                    if t:
                        pieces.append(str(t))
            return "\n".join(pieces).strip()
        except Exception:
            return None

    # Anthropic 原生接口
    def _call_anthropic(
        self,
        provider: Dict[str, str],
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> Optional[str]:
        model = provider.get("model") or "claude-3-5-sonnet-latest"
        payload = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": provider["api_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data.get("content", []) or []
            texts = [str(x.get("text", "")) for x in parts if x.get("type") == "text"]
            return "\n".join([x for x in texts if x]).strip() or None
        except Exception:
            return None

    # Gemini 原生接口
    def _call_gemini(
        self,
        provider: Dict[str, str],
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> Optional[str]:
        model = provider.get("model") or "gemini-1.5-pro"
        encoded_model = urllib.parse.quote(model, safe="")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent"
            f"?key={urllib.parse.quote(provider['api_key'], safe='')}"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", []) or []
            if not candidates:
                return None
            content = candidates[0].get("content", {})
            parts = content.get("parts", []) or []
            texts = [str(x.get("text", "")) for x in parts if x.get("text")]
            return "\n".join(texts).strip() or None
        except Exception:
            return None

    # MARK: - Routing
    # 通过 provider_type 选择具体调用实现。
    def _call_llm(
        self,
        provider: Dict[str, str],
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> Optional[str]:
        provider_type = (provider.get("provider_type") or "openai_compatible").lower()

        if provider_type in {"openai", "openai_compatible"}:
            return self._call_openai_compatible(provider, system_prompt, user_prompt, max_output_tokens)
        if provider_type == "anthropic":
            return self._call_anthropic(provider, system_prompt, user_prompt, max_output_tokens)
        if provider_type in {"gemini", "google"}:
            return self._call_gemini(provider, system_prompt, user_prompt, max_output_tokens)
        return None

    # MARK: - Stage 1
    # 对单个分块执行结构化摘要，提取 demands 列表。
    def _summarize_chunk(self, provider: Dict[str, str], idx: int, lines: List[str]) -> Optional[Dict[str, object]]:
        prompt = {
            "chunk_index": idx,
            "instructions": "提炼本片段中的讨论重点和潜在需求，输出JSON。",
            "schema": {
                "summary": "string",
                "demands": [{"title": "string", "signal": "string", "evidence": "string", "priority": "high|medium|low"}],
            },
            "messages": lines,
        }
        text = self._call_llm(
            provider,
            system_prompt="你是泰语社区产品分析师。严格输出 JSON，不要输出多余文字。",
            user_prompt=json.dumps(prompt, ensure_ascii=False),
            max_output_tokens=900,
        )
        if not text:
            return None
        return self._extract_json(text)

    # MARK: - Stage 2
    # 汇总各分块结果，形成窗口级摘要与统一需求信号。
    def _merge_chunks(
        self,
        provider: Dict[str, str],
        chunk_outputs: List[Dict[str, object]],
        keywords: List[Dict[str, object]],
    ) -> Optional[Dict[str, object]]:
        demand_counter: Counter[str] = Counter()
        demand_examples: Dict[str, str] = {}

        for item in chunk_outputs:
            for demand in item.get("demands", []) or []:
                signal = str(demand.get("signal", "")).strip()
                if not signal:
                    continue
                demand_counter[signal] += 1
                demand_examples.setdefault(signal, str(demand.get("evidence", ""))[:220])

        top_demands = [
            {
                "signal": signal,
                "count": int(count),
                "example": demand_examples.get(signal, ""),
            }
            for signal, count in demand_counter.most_common(8)
        ]

        merge_prompt = {
            "instructions": "把分块摘要合并成一个小时级总结，200字以内。",
            "keywords": keywords[:12],
            "chunk_summaries": [x.get("summary", "") for x in chunk_outputs],
            "demands": top_demands,
        }
        text = self._call_llm(
            provider,
            system_prompt="你是产品策略分析师。给出简洁中文总结，聚焦需求机会与风险。",
            user_prompt=json.dumps(merge_prompt, ensure_ascii=False),
            max_output_tokens=300,
        )

        final_summary = text.strip() if text else None
        if not final_summary:
            merged = " ".join([str(x.get("summary", "")).strip() for x in chunk_outputs if x.get("summary")]).strip()
            final_summary = merged[:260] if merged else ""

        return {
            "summary": final_summary,
            "demand_signals": top_demands,
        }
