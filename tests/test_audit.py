import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import audit, db  # noqa: E402
from tools.repair import is_malformed_name  # noqa: E402


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


def test_audit_reports_names_and_periods(temp_db):
    temp_db.save_stock("600000", "上海浦东发展银行股份有限公司",
                       "公司主营商业银行业务，覆盖公司金融、零售金融。" * 20,
                       report_period="2025-12-31", keywords=["银行"])
    temp_db.save_stock("600519", "贵州茅台", "公司主营茅台酒及系列酒。" * 20,
                       report_period="2024-12-31", keywords=["白酒"])
    report = audit.audit(check_names=False)
    assert report["summary"]["suspicious_names"] == 1
    assert report["summary"]["period_anomalies"] == 1
    assert report["summary"]["metadata_mismatch"] == 0
    assert report["summary"]["refusals"] == 0


def test_is_malformed_name():
    assert is_malformed_name("上海浦东发展银行股份有限公司")
    assert is_malformed_name("2025 Annual Report")
    assert is_malformed_name("公司")
    assert is_malformed_name("600137_浪莎股份_")
    assert not is_malformed_name("贵州茅台")
    assert not is_malformed_name("*ST椰岛")


def test_pure_refusal_heuristic():
    assert db.is_pure_refusal("未提供具体业务描述，无法提炼在营业务。")
    assert db.is_pure_refusal("提供的文本仅含目录标题。")
    detailed = ("公司主营锂电池正极材料，产品包括三元材料与磷酸铁锂。" * 5
                + "报告期内公司未提供细分业务收入数据。")
    assert not db.is_pure_refusal(detailed)
