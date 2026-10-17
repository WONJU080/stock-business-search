import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm import (  # noqa: E402
    ExtractionRefusedError,
    LLMBudgetError,
    OpenAICompatClient,
    build_llm,
    clean_keywords,
    configure_max_concurrent,
    get_max_concurrent,
)


def _client():
    return OpenAICompatClient("http://example.invalid/v1", "k", "m", retries=1)


def test_clean_keywords_drops_generic_and_dedupes():
    assert clean_keywords(["年报", "锂电", "锂电", "主营业务", "储能"]) == ["锂电", "储能"]


def test_extract_business_rejects_refusal():
    c = _client()
    c._chat = lambda *a, **k: '{"business":"未提供具体业务描述，无法提炼在营业务。","keywords":[]}'
    with pytest.raises(ExtractionRefusedError):
        c.extract_business("文本" * 500)


def test_extract_business_dedupes_chunks_and_keywords():
    c = _client()
    sentence = "公司主营锂电池正极材料，产品包括三元材料与磷酸铁锂。"
    c._chat = lambda *a, **k: json.dumps(
        {"business": sentence * 3, "keywords": ["锂电", "年报", "三元材料"]},
        ensure_ascii=False,
    )
    result = c.extract_business("文本" * 10000)
    assert result["business"].count("公司主营") == 1
    assert "年报" not in result["keywords"]
    assert result["prompt_version"].startswith("business-extract")


def test_rank_results_caps_candidates_and_keeps_rest():
    c = _client()
    results = [
        {"code": f"{i:06d}", "name": "x", "business": "b" * 500,
         "keywords": [], "local_score": 100 - i}
        for i in range(100)
    ]
    c._chat = lambda *a, **k: json.dumps(
        [{"code": r["code"], "score": 0.5} for r in results[:60]], ensure_ascii=False
    )
    ranked = c.rank_results("q", results)
    assert len(ranked) == 100
    assert all(r["ranked_by"] == "llm" for r in ranked[:60])
    assert all(r["ranked_by"] == "local" for r in ranked[60:])
    assert [r["rank"] for r in ranked] == list(range(1, 101))


def test_rank_results_raises_if_model_returns_nothing():
    c = _client()
    c._chat = lambda *a, **k: "not-json"
    with pytest.raises(ValueError):
        c.rank_results("q", [{"code": "600519", "name": "x", "business": "b",
                              "keywords": [], "local_score": 1}])


def test_budget_error_splits_chunk_and_merges():
    c = _client()
    calls = []

    def fake(messages, **kwargs):
        content = messages[-1]["content"]
        calls.append(len(content))
        if len(content) > 4000:
            raise LLMBudgetError("reasoning budget exhausted")
        return json.dumps(
            {"business": "公司主营锂电池正极材料。" * 8, "keywords": ["锂电池"]},
            ensure_ascii=False,
        )

    c._chat = fake
    result = c._extract_business_chunk("文本" * 3000, 1, 1)
    assert "锂电池" in result["business"]
    assert "锂电池" in result["keywords"]
    assert len(calls) >= 3


def test_configure_max_concurrent():
    old = get_max_concurrent()
    try:
        assert configure_max_concurrent(16) == 16
        assert get_max_concurrent() == 16
        assert configure_max_concurrent(1) == 1
    finally:
        configure_max_concurrent(old)


def test_build_llm_uses_api_keys_fallback():
    cfg = {
        "llm": {
            "base_url": "http://example.invalid/v1",
            "api_key": "",
            "api_keys": ["sk-test-key"],
            "model": "m",
            "timeout": 1,
        }
    }
    llm = build_llm(cfg)
    assert isinstance(llm, OpenAICompatClient)
    assert llm.api_key == "sk-test-key"
