import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch  # noqa: E402
from tools import db  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    root = tmp_path / "db"
    (root / "stocks").mkdir(parents=True)
    monkeypatch.setattr(db, "DB_ROOT", root)
    monkeypatch.setattr(db, "STOCKS_DIR", root / "stocks")
    monkeypatch.setattr(db, "INDEX_PATH", root / "index.txt")
    monkeypatch.setattr(db, "LOCK_PATH", root / ".write.lock")
    monkeypatch.setattr(db, "RUNS_PATH", root / "runs.jsonl")
    return db


class FakeFetcher:
    def __init__(self, quality="ok"):
        self.quality = quality

    def fetch_report(self, code, report_period):
        return {
            "short_name": "测试",
            "business_text": "公司主营锂电池正极材料。" * 10,
            "period": "2025-12-31",
            "title": "2025年年度报告",
            "source_url": "https://example.invalid/a.pdf",
            "announcement_time": "2026-04-01T00:00:00+08:00",
            "pdf_pages": 100,
            "text_chars": 50000,
            "business_chars": 500,
            "quality": self.quality,
            "warnings": [] if self.quality == "ok" else ["业务章节过短"],
        }


class FakeLLM:
    model = "fake-model"

    def extract_business(self, text):
        return {
            "business": "公司主营锂电池正极材料，产品包括三元材料与磷酸铁锂。",
            "keywords": ["锂电池", "三元材料"],
            "prompt_version": "test-v1",
        }


def test_batch_run_codes(temp_db, monkeypatch):
    monkeypatch.setattr(batch, "load_keys", lambda: ["k1"])
    monkeypatch.setattr(batch, "load_config", lambda: {"llm": {}})
    monkeypatch.setattr(batch, "build_llm_from_key", lambda key, cfg: FakeLLM())
    monkeypatch.setattr(batch, "CninfoFetcher", lambda: FakeFetcher())
    res = batch.run(codes=["600519", "000001"], workers_per_key=1)
    assert sorted(r["status"] for r in res) == ["saved", "saved"]
    assert len(temp_db.load_index()) == 2
    assert len(temp_db.read_runs(limit=10)) == 2


def test_batch_rejects_poor_quality(temp_db, monkeypatch):
    monkeypatch.setattr(batch, "load_keys", lambda: ["k1"])
    monkeypatch.setattr(batch, "load_config", lambda: {"llm": {}})
    monkeypatch.setattr(batch, "build_llm_from_key", lambda key, cfg: FakeLLM())
    monkeypatch.setattr(batch, "CninfoFetcher", lambda: FakeFetcher(quality="poor"))
    res = batch.run(codes=["600519"], workers_per_key=1)
    assert res[0]["status"] == "error"
    assert res[0]["stage"] == "fetch"
    assert not (temp_db.STOCKS_DIR / "600519.txt").exists()
