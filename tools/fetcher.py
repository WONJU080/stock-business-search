import re
import threading
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import requests


class Fetcher(ABC):
    """财报获取抽象：股票代码 -> 财报原文。"""

    @abstractmethod
    def fetch_report(self, code: str, report_period: str) -> dict:
        """返回 {"name", "text", ...}，text 为财报原文（纯文本）。

        可附加字段：business_text（定位到的业务章节）、period（实际报告期）、title。
        """


class StubFetcher(Fetcher):
    """占位实现：未接入财报数据源。"""

    def fetch_report(self, code: str, report_period: str) -> dict:
        raise NotImplementedError(
            "未接入财报数据源，请实现 Fetcher 或使用 CninfoFetcher/FileFetcher"
        )


class FileFetcher(Fetcher):
    """从本地文件读取财报：{reports_dir}/{code}.txt。

    文件首行可写「名称 = 公司名」以提供名称，否则 name 为空。
    """

    def __init__(self, reports_dir):
        self.reports_dir = Path(reports_dir)

    def fetch_report(self, code: str, report_period: str) -> dict:
        path = self.reports_dir / f"{code}.txt"
        if not path.exists():
            raise FileNotFoundError(f"未找到财报文件: {path}")
        text = path.read_text(encoding="utf-8")
        name = ""
        lines = text.splitlines()
        if lines:
            first = lines[0].strip()
            if first.startswith("名称"):
                name = first.split("=", 1)[1].strip()
        return {"name": name, "text": text}


def pdf_to_text(content: bytes) -> str:
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _find_mda_body(text: str) -> int:
    """定位「管理层讨论与分析」正文章节标题，排除目录与交叉引用。"""
    for m in re.finditer(r"管理层讨论与分析|经营情况讨论与分析", text):
        tail = text[m.end():m.end() + 50]
        if "...." in tail:
            continue
        if "之" in tail[:10]:
            continue
        return m.start()
    return -1


def extract_business_section(text: str, max_chars: int = 30000) -> str:
    """从年报全文中定位「报告期内公司从事的主要业务」小节，作为业务分析输入。

    优先精确匹配业务小节锚点，回退到「管理层讨论与分析」正文标题。
    """
    start = -1
    for anchor in ("报告期内公司从事", "公司从事的主要业务"):
        idx = text.find(anchor)
        if idx >= 0:
            start = idx
            break
    if start < 0:
        start = _find_mda_body(text)
    if start < 0:
        return text[:max_chars]
    end = text.find("报告期内公司所处行业", start + 1)
    if end < 0:
        end = text.find("所处行业", start + 1)
    if end < 0 or end - start > max_chars:
        end = start + max_chars
    return text[start:end]


class CninfoFetcher(Fetcher):
    """巨潮资讯网年报全文获取：代码 -> 最新年度报告纯文本。

    流程：查年报公告列表 -> 下载年报 PDF -> 提取全文 -> 定位业务章节。
    """

    STOCK_JSON = "http://www.cninfo.com.cn/new/data/szse_stock.json"
    QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    STATIC = "http://static.cninfo.com.cn"

    def __init__(self, timeout: int = 120):
        self.timeout = timeout
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        }
        self._org_map = None
        self._lock = threading.Lock()

    def _org_id(self, code: str) -> str:
        if self._org_map is None:
            with self._lock:
                if self._org_map is None:
                    self._org_map = self._load_org_map()
        if code not in self._org_map:
            raise ValueError(f"巨潮未收录该代码: {code}")
        return self._org_map[code]

    def _load_org_map(self) -> dict:
        r = requests.get(self.STOCK_JSON, headers=self._headers, timeout=self.timeout)
        r.raise_for_status()
        return {it["code"]: it["orgId"] for it in r.json()["stockList"]}

    def _query_annual(self, code: str) -> list[dict]:
        end = date.today().strftime("%Y-%m-%d")
        params = {
            "pageNum": "1",
            "pageSize": "30",
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{code},{self._org_id(code)}",
            "searchkey": "",
            "secid": "",
            "category": "category_ndbg_szsh",
            "trade": "",
            "seDate": f"1990-01-01~{end}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        r = requests.post(self.QUERY, data=params, headers=self._headers, timeout=self.timeout)
        r.raise_for_status()
        anns = r.json().get("announcements") or []
        return [
            a for a in anns
            if "摘要" not in a["announcementTitle"]
            and "英文" not in a["announcementTitle"]
        ]

    def latest_annual_period(self, code: str) -> str:
        anns = self._query_annual(code)
        if not anns:
            raise ValueError(f"未找到年报: {code}")
        m = re.search(r"(\d{4})年", anns[0]["announcementTitle"])
        if not m:
            raise ValueError(f"无法解析年报年份: {anns[0]['announcementTitle']}")
        return f"{m.group(1)}-12-31"

    def fetch_report(self, code: str, report_period: str = "-") -> dict:
        code = str(code).strip().zfill(6)
        anns = self._query_annual(code)
        if not anns:
            raise ValueError(f"未找到年报: {code}")

        if report_period == "-":
            target = anns[0]
            m = re.search(r"(\d{4})年", target["announcementTitle"])
            period = f"{m.group(1)}-12-31" if m else "-"
        else:
            year = report_period[:4]
            target = next((a for a in anns if f"{year}年" in a["announcementTitle"]), None)
            if target is None:
                raise ValueError(f"未找到 {year} 年年报: {code}")
            period = report_period

        title = target["announcementTitle"]
        name = re.sub(r"\d{4}年.*", "", title).strip()

        pdf_url = self.STATIC + "/" + target["adjunctUrl"]
        r = requests.get(pdf_url, headers=self._headers, timeout=self.timeout)
        r.raise_for_status()

        text = pdf_to_text(r.content)
        return {
            "name": name,
            "text": text,
            "business_text": extract_business_section(text),
            "period": period,
            "title": title,
        }
