import json
import re
from abc import ABC, abstractmethod

import requests


def _parse_json(text, default):
    """从模型回复中提取 JSON（容忍 markdown 代码块与前后缀文字）。"""
    text = (text or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        pass
    text = re.sub(r"```[a-zA-Z]*", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return default
    start = min(starts)
    is_obj = text[start] == "{"
    end = text.rfind("}" if is_obj else "]")
    if end <= start:
        return default
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return default


class LLMClient(ABC):
    """LLM 抽象接口。接入具体模型时继承并实现全部方法。"""

    @abstractmethod
    def expand_keywords(self, query: str) -> list[str]:
        """将搜索词扩展为近义词/关联词列表，不含原词（调用方负责保留原词）。"""

    @abstractmethod
    def rank_results(self, query: str, results: list[dict]) -> list[dict]:
        """对 db.search 返回的结果按关联度排序，返回排序后的列表。

        结果项可追加 "score"（关联度）与 "rank"（名次）字段。
        """

    @abstractmethod
    def extract_business(self, report_text: str) -> dict:
        """从财报原文提取在营业务。

        返回 {"business": str, "keywords": list[str]}，其中 business 为
        不概括的一长段文字，keywords 为提炼的关键词。
        """


class StubLLM(LLMClient):
    """占位实现：不调用模型，仅保证链路可跑通，后续替换为真实模型。"""

    def expand_keywords(self, query: str) -> list[str]:
        return []

    def rank_results(self, query: str, results: list[dict]) -> list[dict]:
        ranked = sorted(
            results,
            key=lambda r: len(r.get("matched", [])),
            reverse=True,
        )
        for i, r in enumerate(ranked):
            r["score"] = len(r.get("matched", []))
            r["rank"] = i + 1
        return ranked

    def extract_business(self, report_text: str) -> dict:
        raise NotImplementedError("StubLLM 未实现财报分析，请接入真实模型")


class OpenAICompatClient(LLMClient):
    """兼容任意 OpenAI 协议 API 的模型客户端（DeepSeek/OpenAI/Ollama/vLLM 等）。"""

    def __init__(self, base_url, api_key, model, timeout=120, temperature=0.1):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        self.temperature = temperature

    def _chat(self, messages, temperature=None):
        if not self.base_url or not self.model:
            raise ValueError("未配置 API（base_url 或 model 为空）")
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        r = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def expand_keywords(self, query):
        prompt = (
            f"你是股票业务检索助手。用户想搜索主营业务与「{query}」相关的股票。\n"
            f"请给出与「{query}」语义相近、可用于检索股票主营业务的关联词或近义词"
            f"（例如「点读笔」可关联「学习机」「早教机」「教育硬件」）。\n"
            f"只输出一个 JSON 数组（3~8 个词），不要输出任何其他内容。\n"
            f'示例格式：["学习机","早教机"]'
        )
        text = self._chat([{"role": "user", "content": prompt}])
        arr = _parse_json(text, default=[])
        if not isinstance(arr, list):
            arr = []
        return [str(x).strip() for x in arr if str(x).strip()]

    def rank_results(self, query, results):
        items = [
            {
                "code": r["code"],
                "name": r.get("name", ""),
                "business": (r.get("business") or "")[:600],
            }
            for r in results
        ]
        prompt = (
            f"用户搜索词：「{query}」\n"
            f"以下是候选股票及其主营业务描述（JSON 数组）：\n"
            f"{json.dumps(items, ensure_ascii=False)}\n"
            f"请按与搜索词的业务关联程度从高到低排序，为每只股票给出 0~1 的关联度分数。\n"
            f'只输出 JSON 数组，每项含 "code" 和 "score"，不要输出其他内容。\n'
            f'示例：[{{"code":"002230","score":0.9}}]'
        )
        text = self._chat([{"role": "user", "content": prompt}])
        scored = _parse_json(text, default=[])
        if not isinstance(scored, list):
            scored = []
        score_map = {
            str(x.get("code", "")).strip(): float(x.get("score", 0))
            for x in scored
            if isinstance(x, dict)
        }
        ranked = sorted(results, key=lambda r: score_map.get(r["code"], -1), reverse=True)
        for i, r in enumerate(ranked):
            r["score"] = score_map.get(r["code"], 0)
            r["rank"] = i + 1
        return ranked

    def extract_business(self, report_text):
        chunks = [report_text[i:i + 15000] for i in range(0, len(report_text), 15000)]
        if len(chunks) == 1:
            data = self._extract_business_chunk(chunks[0], 1, 1)
            return {"business": data["business"], "keywords": data["keywords"]}
        from concurrent.futures import ThreadPoolExecutor

        jobs = [(c, i + 1, len(chunks)) for i, c in enumerate(chunks)]
        with ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as ex:
            results = list(ex.map(lambda a: self._extract_business_chunk(*a), jobs))
        business = "\n".join(r["business"] for r in results if r["business"]).strip()
        all_keywords = []
        for r in results:
            all_keywords.extend(r["keywords"])
        keywords = list(dict.fromkeys(all_keywords))
        if not business:
            raise ValueError("模型未返回有效的业务内容")
        return {"business": business, "keywords": keywords}

    def _extract_business_chunk(self, chunk, part, total):
        seg_note = f"（这是财报文本的第 {part}/{total} 段，请提取本段中的业务信息，避免与前后段重复）\n" if total > 1 else ""
        prompt = (
            "你是财报分析师。请从下面的年报「管理层讨论与分析」文本中，提取该公司全部在营业务信息。\n"
            "要求：不概括、不遗漏，将一切业务信息（主营业务、产品、经营模式、销售渠道、"
            "所处行业、新业务等）整理成一段通顺的中文长文字；同时提炼 5~15 个用于检索的关键词。\n"
            '只输出 JSON，格式：{"business":"...","keywords":["..."]}，不要输出其他内容。\n\n'
            f"{seg_note}财报文本：\n{chunk}"
        )
        text = self._chat([{"role": "user", "content": prompt}])
        data = _parse_json(text, default={})
        if not isinstance(data, dict):
            data = {}
        business = str(data.get("business", "")).strip()
        keywords = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
        if not business:
            raise ValueError("模型未返回有效的业务内容")
        return {"business": business, "keywords": keywords}


def build_llm(cfg=None):
    """根据配置装配 LLM：有 api_key 用 OpenAICompatClient，否则回退 StubLLM。"""
    try:
        from .config import load_config
    except ImportError:
        from config import load_config
    cfg = cfg or load_config()
    llm = cfg.get("llm", {})
    if llm.get("api_key"):
        return OpenAICompatClient(
            base_url=llm.get("base_url", ""),
            api_key=llm["api_key"],
            model=llm.get("model", ""),
            timeout=llm.get("timeout", 120),
            temperature=llm.get("temperature", 0.1),
        )
    return StubLLM()
