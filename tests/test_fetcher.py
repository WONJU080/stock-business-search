import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.fetcher import (  # noqa: E402
    CninfoFetcher,
    _find_mda_body,
    extract_business_section,
    looks_like_toc_only,
)


def _ann(title, ms=1777305600000):
    return {"announcementTitle": title, "announcementTime": ms,
            "_time_ms": ms, "adjunctUrl": "x.pdf"}


def _fetcher(code="300122", name="智飞生物"):
    return CninfoFetcher(stock_list=[{"code": code, "name": name, "org_id": "x"}])


def test_picks_latest_year_not_first_announcement():
    f = _fetcher()
    anns = [
        _ann("2022年年度报告（更正后）"),
        _ann("2021年年度报告（更正后）"),
        _ann("2023年年度报告 （更正后）"),
        _ann("2020年年度报告（更正后）"),
        _ann("2024年年度报告（更正后）"),
        _ann("2025年年度报告"),
        _ann("2025年年度报告摘要"),
    ]
    chosen = f._pick_annual(anns, "-")
    assert chosen["period"] == "2025-12-31"
    assert chosen["title"] == "2025年年度报告"


def test_prefers_full_report_over_graphics_and_summary():
    f = _fetcher("600177", "雅戈尔")
    anns = [
        _ann("雅戈尔时尚股份有限公司2025年年度报告（图文版）", 1778860800000),
        _ann("雅戈尔时尚股份有限公司2025年年度报告摘要"),
        _ann("雅戈尔时尚股份有限公司2025年年度报告"),
    ]
    chosen = f._pick_annual(anns, "-")
    assert chosen["title"] == "雅戈尔时尚股份有限公司2025年年度报告"


def test_title_without_year_falls_back_to_announcement_time():
    f = _fetcher("600754", "锦江酒店")
    chosen = f._pick_annual([_ann("锦江酒店年报", 1774627200000),
                             _ann("锦江酒店年报摘要", 1774627200000)], "-")
    assert chosen["period"] == "2025-12-31"


def test_english_report_is_fallback_only():
    f = _fetcher("000887", "中鼎股份")
    chosen = f._pick_annual([
        _ann("Anhui Zhongding Sealing Parts Co.,Ltd. Annual Report 2025"),
        _ann("2025年年度报告"),
    ], "-")
    assert chosen["is_english"] is False
    assert chosen["title"] == "2025年年度报告"


def test_missing_year_raises_with_available_years():
    f = _fetcher()
    with pytest.raises(ValueError, match="2025"):
        f._pick_annual([_ann("2024年年度报告"), _ann("2023年年度报告")], "2025-12-31")


def test_fallback_name_cleans_title_artifacts():
    assert CninfoFetcher._fallback_name("公司2025年年度报告（更正后）") == ""
    assert CninfoFetcher._fallback_name("688205_德科立_2025_年度报告") == "德科立"
    assert CninfoFetcher._fallback_name("2025 Annual Report") == ""
    assert CninfoFetcher._fallback_name("雅戈尔时尚股份有限公司2025年年度报告") == "雅戈尔时尚股份有限公司"


def test_extract_business_section_skips_toc_and_stops_at_next_section():
    text = (
        "目录\n第三节 管理层讨论与分析…………12\n"
        "一、报告期内公司从事的主要业务…………18\n"
        "二、报告期内公司所处行业情况…………15\n\n"
        "第三节 管理层讨论与分析\n一、报告期内公司所处行业情况\n行业情况正文……\n"
        "二、报告期内公司从事的主要业务\n公司主营锂电池正极材料，产品包括三元材料、磷酸铁锂，"
        "客户为动力电池厂商，应用于新能源汽车和储能领域。\n"
        "三、核心竞争力分析\n公司拥有技术优势。\n"
        "第四节 公司治理\n公司治理正文。"
    )
    out = extract_business_section(text)
    assert "锂电池正极材料" in out
    assert "公司治理正文" not in out
    assert "目录" not in out
    assert not looks_like_toc_only(out)


def test_empty_text_section():
    assert extract_business_section("") == ""


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.content = b"{}"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return {}


def test_403_falls_back_to_browser_fingerprint(monkeypatch):
    f = _fetcher()
    monkeypatch.setattr(f, "_session", lambda: type(
        "S", (), {"request": lambda self, *a, **k: _Resp(403)}
    )())
    called = []
    monkeypatch.setattr(f, "_browser_request", lambda *a, **k: called.append(1) or _Resp(200))
    r = f._request("POST", "https://example.invalid/query")
    assert r.status_code == 200
    assert f._use_browser is True
    assert called


def test_mda_locator_skips_quoted_cross_reference_and_toc():
    text = (
        "风险提示：具体情况请阅读本报告“管理层讨论与分析”之“四、风险因素”。\n"
        "目录\n重要提示\n释义\n第一章 公司简介\n"
        "第三章 管理层讨论与分析\n一、总体经营概况\n二、业务回顾\n"
        "第四章 股份变动及股东情况\n"
        "中国民生银行股份有限公司\n释义\n本报告中……\n"
        "第三章 管理层讨论与分析\n一、总体经营概况\n"
        "报告期内，本集团强化党建引领，保持战略定力。各项业务稳步发展。\n"
    )
    pos = _find_mda_body(text)
    assert pos > 0
    assert text[pos:pos + 40].startswith("管理层讨论与分析\n一、总体经营概况")
