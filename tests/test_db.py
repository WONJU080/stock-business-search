import multiprocessing as mp
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def _save_worker(root, pid):
    import tools.db as child_db

    root = Path(root)
    child_db.DB_ROOT = root
    child_db.STOCKS_DIR = root / "stocks"
    child_db.INDEX_PATH = root / "index.txt"
    child_db.LOCK_PATH = root / ".write.lock"
    child_db.RUNS_PATH = root / "runs.jsonl"
    for i in range(15):
        code = f"{pid:02d}{i:04d}"
        child_db.save_stock(code, f"股{code}", "经营业务内容" * 5,
                            report_period="2025-12-31", keywords=["测试"])


def test_save_roundtrip_and_preserve_analyzed_at(temp_db):
    temp_db.save_stock("600519", "贵州茅台", "公司主营茅台酒及系列酒。" * 3,
                       report_period="2025-12-31", keywords=["白酒", "白酒", "年报"],
                       analyzed_at="2026-01-01", updated_at="2026-01-01")
    data = temp_db.parse_stock_file("600519")
    assert data["name"] == "贵州茅台"
    assert "年报" not in data["keywords"]
    assert data["analyzed_at"] == "2026-01-01"

    temp_db.save_stock("600519", "贵州茅台", "更新后的业务内容。" * 3,
                       report_period="2025-12-31", keywords=["白酒"])
    rec = temp_db.load_index()["600519"]
    assert rec["analyzed_at"] == "2026-01-01"
    assert rec["updated_at"] == temp_db.date.today().isoformat()


def test_pipe_name_is_sanitized_not_lost(temp_db):
    temp_db.save_stock("000002", "万科|A", "房地产开发与物业服务。" * 3,
                       report_period="2025-12-31", keywords=["地产"])
    rec = temp_db.load_index()["000002"]
    assert rec["name"] == "万科｜A"
    assert "000002" in temp_db.load_index()


def test_invalid_business_rejected(temp_db):
    with pytest.raises(ValueError):
        temp_db.save_stock("000001", "平安银行", "")
    with pytest.raises(ValueError):
        temp_db.save_stock("000001", "平安银行", "无法提炼在营业务")
    with pytest.raises(ValueError):
        temp_db.save_stock("000001", "平安银行", "正常业务" * 10, report_period="2025/12/31")


def test_detailed_business_with_caveat_is_allowed(temp_db):
    business = ("公司主营锂电池正极材料，产品包括三元材料与磷酸铁锂，客户为动力电池厂商。" * 5
                + "报告期内公司未提供细分业务收入数据。")
    temp_db.save_stock("300750", "宁德时代", business,
                       report_period="2025-12-31", keywords=["锂电池"])
    assert "未提供细分业务收入" in temp_db.parse_stock_file("300750")["business"]
    with pytest.raises(ValueError):
        temp_db.save_stock("300750", "宁德时代", "正常业务\n[关键词]\n注入" * 20,
                           report_period="2025-12-31")


def test_search_code_name_and_limit(temp_db):
    temp_db.save_stock("600519", "贵州茅台", "公司主营茅台酒及系列酒。" * 3,
                       report_period="2025-12-31", keywords=["白酒"])
    temp_db.save_stock("000001", "平安银行", "经营商业银行业务，吸收公众存款。" * 3,
                       report_period="2025-12-31", keywords=["银行"])
    assert [r["code"] for r in temp_db.search("600519")] == ["600519"]
    assert [r["code"] for r in temp_db.search("贵州茅台")] == ["600519"]
    assert len(temp_db.search("银行")) == 1
    assert len(temp_db.search("公司", limit=1)) == 1


def test_bulk_rename_preserves_body_dates(temp_db):
    temp_db.save_stock("600000", "上海浦东发展银行股份有限公司", "经营商业银行业务。" * 3,
                       report_period="2025-12-31", keywords=["银行"],
                       analyzed_at="2026-01-01", updated_at="2026-01-02")
    res = temp_db.bulk_rename({"600000": "浦发银行"})
    assert res["renamed"] == 1
    data = temp_db.parse_stock_file("600000")
    assert data["name"] == "浦发银行"
    assert data["analyzed_at"] == "2026-01-01"
    assert "商业银行" in data["business"]
    assert temp_db.load_index()["600000"]["name"] == "浦发银行"


def test_rebuild_index_and_error_reporting(temp_db):
    temp_db.save_stock("600519", "贵州茅台", "公司主营茅台酒。" * 3,
                       report_period="2025-12-31", keywords=["白酒"])
    temp_db.INDEX_PATH.unlink()
    res = temp_db.rebuild_index()
    assert res["count"] == 1
    assert temp_db.load_index()["600519"]["name"] == "贵州茅台"

    temp_db.INDEX_PATH.write_text(
        "# header\n600519|贵州茅台|2025-12-31|2026-01-01|2026-01-01|stocks/600519.txt\n"
        "bad-line-without-pipes\n",
        encoding="utf-8",
    )
    records, errors = temp_db.load_index_with_errors()
    assert "600519" in records
    assert errors and errors[0]["line"] == 3


def test_concurrent_saves_are_not_lost(temp_db):
    root = str(temp_db.DB_ROOT)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_save_worker, args=(root, pid)) for pid in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    files = len(list(temp_db.STOCKS_DIR.glob("*.txt")))
    records = len(temp_db.load_index())
    assert files == 60
    assert records == 60


def test_log_run_appends_jsonl(temp_db):
    temp_db.log_run({"code": "600519", "status": "saved"})
    temp_db.log_run({"code": "000001", "status": "error", "error": "x"})
    rows = temp_db.read_runs(limit=10)
    assert len(rows) == 2
    assert rows[0]["code"] == "600519"
    assert rows[1]["status"] == "error"
