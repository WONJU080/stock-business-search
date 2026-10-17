import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import db  # noqa: E402
from tools.analyze_flow import analyze  # noqa: E402
from tools.llm import ExtractionRefusedError  # noqa: E402


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
        self.calls = []

    def fetch_report(self, code, report_period):
        self.calls.append(code)
        return {
            "name": "测试股份",
            "short_name": "测试",
            "business_text": "报告期内公司从事的主要业务。公司主营锂电池正极材料。" * 5,
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

    def __init__(self, refuse=False):
        self.refuse = refuse

    def extract_business(self, report_text):
        if self.refuse:
            raise ExtractionRefusedError("模型拒绝提炼业务")
        return {
            "business": "公司主营锂电池正极材料，产品包括三元材料与磷酸铁锂。" * 2,
            "keywords": ["锂电池", "三元材料"],
            "prompt_version": "test-v1",
        }


def test_analyze_saves_and_preserves_first_analyzed_at(temp_db):
    temp_db.save_stock("600519", "贵州茅台", "旧业务内容。" * 5,
                       report_period="2025-12-31", keywords=["旧"],
                       analyzed_at="2026-01-01", updated_at="2026-01-01")
    fetcher = FakeFetcher()
    llm = FakeLLM()
    progress = []
    res = analyze(codes=["600519"], fetcher=fetcher, llm=llm, force=True,
                  workers=1, on_progress=progress.append)
    assert res[0]["status"] == "saved"
    assert res[0]["name"] == "测试"
    data = temp_db.parse_stock_file("600519")
    assert data["analyzed_at"] == "2026-01-01"
    assert "三元材料" in data["business"]
    assert data["name"] == "测试"
    assert len(progress) == 1
    rows = temp_db.read_runs(limit=5)
    assert rows[-1]["status"] == "saved"
    assert rows[-1]["title"] == "2025年年度报告"
    assert rows[-1]["prompt_version"] == "test-v1"


def test_analyze_skips_existing_without_force(temp_db):
    temp_db.save_stock("600519", "贵州茅台", "旧业务内容。" * 5,
                       report_period="2025-12-31", keywords=["旧"])
    fetcher = FakeFetcher()
    res = analyze(codes=["600519"], fetcher=fetcher, llm=FakeLLM(), force=False, workers=1)
    assert res[0]["status"] == "skipped"
    assert fetcher.calls == []


def test_analyze_rejects_poor_text_without_calling_llm(temp_db):
    res = analyze(codes=["600519"], fetcher=FakeFetcher(quality="poor"),
                  llm=FakeLLM(), force=True, workers=1)
    assert res[0]["status"] == "error"
    assert res[0]["stage"] == "fetch"
    assert "质量不足" in res[0]["error"]
    assert not (temp_db.STOCKS_DIR / "600519.txt").exists()


def test_analyze_rejects_refusal(temp_db):
    res = analyze(codes=["600519"], fetcher=FakeFetcher(), llm=FakeLLM(refuse=True),
                  force=True, workers=1)
    assert res[0]["status"] == "error"
    assert res[0]["stage"] == "extract"
    assert not (temp_db.STOCKS_DIR / "600519.txt").exists()


def test_analyze_multiple_codes_parallel(temp_db):
    res = analyze(codes=["600519", "000001"], fetcher=FakeFetcher(), llm=FakeLLM(),
                  force=True, workers=2)
    assert sorted(r["status"] for r in res) == ["saved", "saved"]
    assert len(temp_db.load_index()) == 2
