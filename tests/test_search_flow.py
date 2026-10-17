import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.search_flow import search  # noqa: E402


class BadLLM:
    def expand_keywords(self, query):
        raise RuntimeError("expand broke")

    def rank_results(self, query, results):
        raise RuntimeError("rank broke")


def test_search_falls_back_to_local_when_llm_fails(monkeypatch):
    hits = [
        {"code": "600519", "name": "贵州茅台", "business": "白酒", "keywords": ["白酒"],
         "matched": ["白酒"], "local_score": 5, "snippet": "白酒"},
        {"code": "000001", "name": "平安银行", "business": "银行", "keywords": ["银行"],
         "matched": ["银行"], "local_score": 2, "snippet": "银行"},
    ]
    monkeypatch.setattr("tools.search_flow.db.search", lambda related: hits)
    res = search("白酒", llm=BadLLM())
    assert res["total_hits"] == 2
    assert [r["code"] for r in res["results"]] == ["600519", "000001"]
    assert res["results"][0]["ranked_by"] == "local"
    assert len(res["warnings"]) == 2
