"""财报获取：股票代码 -> 年报原文 / 业务章节。

重点修复：
- 不再假设巨潮公告列表第一条就是最新年报；按报告年份分组，优先中文标准全文。
- 使用巨潮 `zwjc` 作为股票简称，不再从公告标题猜名称。
- 网络请求带重试/退避；PDF 文本质量差时明确报错，而不是把“无法提炼”交给模型。
- 章节定位跳过目录/交叉引用，优先“报告期内公司从事的主要业务”正文。
"""

import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from pathlib import Path

import requests

try:
    from .stock_list import load_stock_list
except ImportError:  # pragma: no cover - 兼容直接脚本运行
    from stock_list import load_stock_list


class Fetcher(ABC):
    """财报获取抽象：股票代码 -> 财报原文。"""

    @abstractmethod
    def fetch_report(self, code: str, report_period: str) -> dict:
        """返回 {"name", "text", ...}，text 为财报原文（纯文本）。

        约定字段：business_text、period、title、source_url、quality、warnings。
        """


class LowQualityTextError(ValueError):
    """PDF/章节文本质量不足，不能交给 LLM 继续抽取。"""


class StubFetcher(Fetcher):
    def fetch_report(self, code: str, report_period: str) -> dict:
        raise NotImplementedError(
            "未接入财报数据源，请实现 Fetcher 或使用 CninfoFetcher/FileFetcher"
        )


class FileFetcher(Fetcher):
    """从本地文件读取财报：{reports_dir}/{code}.txt。"""

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
        cleaned = clean_text(text)
        business = extract_business_section(cleaned)
        return {
            "name": name,
            "short_name": name,
            "text": cleaned,
            "business_text": business,
            "period": report_period if report_period != "-" else "-",
            "title": path.name,
            "source_url": str(path),
            "quality": "ok" if len(cleaned) >= 500 else "poor",
            "warnings": [] if len(cleaned) >= 500 else ["文本过短"],
            "text_chars": len(cleaned),
            "business_chars": len(business),
        }


# ---------------------------------------------------------------------------
# PDF / 文本处理
# ---------------------------------------------------------------------------

def pdf_to_text_with_meta(content: bytes) -> dict:
    """返回 {"text", "pages"}。优先 pymupdf，兼容旧版 fitz。"""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        import fitz as pymupdf
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        pages = doc.page_count
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()
    return {"text": text, "pages": pages}


def pdf_to_text(content: bytes) -> str:
    return pdf_to_text_with_meta(content)["text"]


def _is_toc_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.search(r"(\.{4,}|…{2,}|·{4,})", s):
        return True
    if re.match(r"^第[一二三四五六七八九十百]+[节章]", s) and re.search(r"\d+\s*$", s):
        return True
    if re.match(r"^[一二三四五六七八九十]+[、.]", s) and re.search(r"\d+\s*$", s):
        return True
    return False


def _toc_end(text: str, scan_lines: int = 400) -> int:
    """返回目录疑似结束的字符位置；没有目录返回 0。"""
    pos = 0
    last_toc = -1
    for i, line in enumerate(text.splitlines()):
        if i >= scan_lines:
            break
        if _is_toc_line(line):
            last_toc = pos + len(line)
        pos += len(line) + 1
    return max(0, last_toc)


def _find_mda_body(text: str) -> int:
    """定位「管理层讨论与分析」正文章节标题，排除目录、引号引用与交叉引用。

    北交所/部分年报会在“释义”或“风险提示”里写
    “管理层讨论与分析”之“四、风险因素”，旧逻辑会从这里截取到释义表。
    这里优先选“第X节 管理层讨论与分析”这种正文标题。
    """
    candidates = []
    for m in re.finditer(r"管理层讨论与分析|经营情况讨论与分析", text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        stripped = line.strip()
        if _is_toc_line(line):
            continue
        if any(k in stripped for k in ("详见", "参见", "本报告第")):
            continue
        before = text[max(0, line_start - 12):m.start()]
        if any(k in before for k in ("“", "《", "「")):
            continue
        tail = text[m.end():m.end() + 12]
        if "之" in tail[:4]:
            continue
        # 目录块：标题后紧跟一串编号小节（3.1/3.2/...）且几乎没有正文
        immediate = text[m.end():m.end() + 500]
        header_lines = len(re.findall(
            r"(?m)^\s*(?:\d+(?:\.\d+)+|[一二三四五六七八九十]+、|第[一二三四五六七八九十百]+[节章])\s*\S",
            immediate,
        ))
        if header_lines >= 3 and immediate.count("。") < 2:
            continue
        # 目录块：后面紧跟一长串章节标题、几乎没有句号
        block = text[m.end():m.end() + 1500]
        if block.count("。") < 2 and block.count("\n") > 10:
            continue
        # 目录中的“第三章 管理层讨论与分析”：前缀里有“目录”和多个章节标题
        prefix = text[max(0, line_start - 2000):line_start]
        if "目录" in prefix and len(re.findall(r"\n第[一二三四五六七八九十]+章", prefix)) >= 2:
            continue
        score = 1
        if re.match(r"^第[一二三四五六七八九十百]+[节章]", stripped):
            score = 3
        elif stripped.startswith("管理层讨论与分析") or stripped.startswith("经营情况讨论与分析"):
            score = 2
        candidates.append((score, m.start()))
    if not candidates:
        return -1
    best = max(score for score, _ in candidates)
    return min(pos for score, pos in candidates if score == best)


def clean_text(text: str) -> str:
    """清理年报文本噪音：页眉、页码、重复空行。"""
    lines = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", s):
            continue
        if re.fullmatch(r"[-—\s]*\d+[-—\s]*", s):
            continue
        if "年度报告" in s and len(s) < 40:
            continue
        lines.append(s)
    return re.sub(r"\n{2,}", "\n\n", "\n".join(lines))


def _find_anchor(text: str, anchors, start_at=0):
    """找到不在目录/交叉引用里的锚点位置。"""
    for anchor in anchors:
        pos = start_at
        while True:
            idx = text.find(anchor, pos)
            if idx < 0:
                break
            line_start = text.rfind("\n", 0, idx) + 1
            line_end = text.find("\n", idx)
            if line_end < 0:
                line_end = len(text)
            line = text[line_start:line_end]
            before = text[max(0, idx - 40):idx]
            after = text[idx + len(anchor):idx + len(anchor) + 8]
            if _is_toc_line(line):
                pos = idx + len(anchor)
                continue
            if any(k in before for k in ("详见", "参见", "见第", "本报告第")):
                pos = idx + len(anchor)
                continue
            if any(k in before for k in ("“", "《", "「")) or after.startswith("”"):
                pos = idx + len(anchor)
                continue
            return idx
    return -1


def _find_section_end(text: str, start: int, max_chars: int):
    """业务章节延伸到下一个“第X节/章”大标题。

    不在这里用“报告期内公司所处行业”截断：行业情况之后通常还有
    “经营情况讨论与分析”，其中的业务板块/收入构成正是“在营业务”的关键信息。
    """
    patterns = (
        r"\n\s*第[一二三四五六七八九十百]+节\s",
        r"\n\s*第[一二三四五六七八九十百]+章\s",
        r"\n\s*(?:公司治理|环境和社会责任|重要事项|股份变动及股东情况|财务报告)\s*\n",
    )
    best = None
    for pat in patterns:
        m = re.search(pat, text[start + 1:])
        if m:
            pos = start + 1 + m.start()
            if pos > start and (best is None or pos < best):
                best = pos
    if best is not None:
        return min(best, start + max_chars)
    return min(len(text), start + max_chars)


def extract_business_section(text: str, max_chars: int = 40000) -> str:
    """从年报全文中定位「报告期内公司从事的主要业务」小节。

    优先正文锚点，其次「管理层讨论与分析」正文标题，最后才回退全文。
    跳过目录行和“详见……”交叉引用。
    """
    text = text or ""
    if not text.strip():
        return ""
    anchor = _find_anchor(text, ("报告期内公司从事", "公司从事的主要业务"))
    if anchor < 0:
        anchor = _find_mda_body(text)
    if anchor < 0:
        # 没有章节结构时，至少跳过目录再截取
        return clean_text(text[_toc_end(text): _toc_end(text) + max_chars])
    end = _find_section_end(text, anchor, max_chars)
    return clean_text(text[anchor:end])


def looks_like_toc_only(text: str) -> bool:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return True
    toc = sum(1 for l in lines if _is_toc_line(l))
    if len(lines) <= 5 and toc >= max(1, len(lines) // 2):
        return True
    # 形如“3.1 公司总体经营情况 / 3.2 ……”的编号目录，没有正文句号
    numbered = sum(1 for l in lines if re.match(r"^\d+(?:\.\d+)+\s+\S", l))
    return len(text) < 800 and numbered >= 5 and text.count("。") < 2


# ---------------------------------------------------------------------------
# 巨潮
# ---------------------------------------------------------------------------

class CninfoFetcher(Fetcher):
    """巨潮资讯网年报全文获取：代码 -> 最新年度报告纯文本。

    流程：股票列表（代码/简称/orgId）-> 年报公告列表 -> 选择目标年份
          -> 下载 PDF -> 提取全文 -> 定位业务章节 -> 质量检查。
    """

    STOCK_JSON = "https://www.cninfo.com.cn/new/data/szse_stock.json"
    QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    STATIC = "https://static.cninfo.com.cn"

    def __init__(self, timeout: int = 120, retries: int = 3, stock_list=None):
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self._stock_items = list(stock_list) if stock_list else None
        self._stock_map = None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._use_browser = False
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    # ---------- HTTP ----------

    def _session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._headers)
            self._local.session = s
        return s

    def _browser_request(self, method, url, **kwargs):
        """用 curl_cffi 模拟浏览器 TLS 指纹；用于绕过巨潮 WAF 对 requests 的 403。"""
        try:
            from curl_cffi import requests as cffi
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "巨潮接口返回 403，且未安装 curl_cffi；请 pip install curl_cffi"
            ) from exc
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", {}) or {})
        data = kwargs.pop("data", None)
        json_body = kwargs.pop("json", None)
        timeout = kwargs.pop("timeout", self.timeout)
        if method.upper() == "POST":
            if data is not None:
                return cffi.post(url, data=data, headers=headers,
                                 timeout=timeout, impersonate="chrome")
            return cffi.post(url, json=json_body, headers=headers,
                             timeout=timeout, impersonate="chrome")
        return cffi.get(url, headers=headers, timeout=timeout, impersonate="chrome")

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        last_exc = None
        for attempt in range(self.retries):
            try:
                if self._use_browser:
                    r = self._browser_request(method, url, **kwargs)
                else:
                    r = self._session().request(method, url, **kwargs)
                    if r.status_code == 403:
                        # 巨潮 WAF 会按 TLS 指纹拦截 requests；切 curl_cffi 后重试一次
                        self._use_browser = True
                        r = self._browser_request(method, url, **kwargs)
                if r.status_code in (429, 500, 502, 503, 504):
                    retry_after = r.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else min(20, 1.5 * (2 ** attempt))
                    time.sleep(delay)
                    last_exc = requests.HTTPError(f"HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                return r
            except Exception as exc:
                last_exc = exc
                if attempt == self.retries - 1:
                    raise
                time.sleep(min(20, 1.5 * (2 ** attempt)))
        raise last_exc or RuntimeError("请求失败")

    # ---------- 股票列表 ----------

    def _load_stock_map(self):
        if self._stock_map is not None:
            return self._stock_map
        with self._lock:
            if self._stock_map is not None:
                return self._stock_map
            items = self._stock_items
            if items is None:
                items = load_stock_list()
            self._stock_map = {it["code"]: it for it in items}
        return self._stock_map

    def _stock_meta(self, code: str) -> dict:
        meta = self._load_stock_map().get(code)
        if not meta:
            raise ValueError(f"巨潮未收录该代码: {code}")
        return meta

    def _org_id(self, code: str) -> str:
        return self._stock_meta(code)["org_id"]

    def short_name(self, code: str) -> str:
        return self._stock_meta(code).get("name") or ""

    # ---------- 公告 ----------

    def _query_annual(self, code: str, page_size: int = 100) -> list[dict]:
        code = str(code).strip().zfill(6)
        end = date.today().strftime("%Y-%m-%d")
        params = {
            "pageNum": "1",
            "pageSize": str(page_size),
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{code},{self._org_id(code)}",
            "searchkey": "",
            "secid": "",
            "category": "category_ndbg_szsh",
            "trade": "",
            "seDate": f"1990-01-01~{end}",
            "sortName": "announcementTime",
            "sortType": "desc",
            "isHLtitle": "true",
        }
        r = self._request("POST", self.QUERY, data=params)
        anns = r.json().get("announcements") or []
        for a in anns:
            a["_time_ms"] = int(a.get("announcementTime") or 0)
        return anns

    @staticmethod
    def _parse_title_year(title: str):
        m = re.search(r"((?:19|20)\d{2})\s*年", title)
        if m:
            return int(m.group(1)), "title"
        m = re.search(r"(?:Annual\s+Report|Annual\s+Report\s+of)[^\d]{0,15}((?:19|20)\d{2})", title, re.I)
        if m:
            return int(m.group(1)), "title"
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", title)
        if m:
            return int(m.group(1)), "title"
        return None, None

    @staticmethod
    def _announcement_time_iso(ms):
        if not ms:
            return "-"
        try:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return "-"

    @classmethod
    def _classify(cls, ann: dict) -> dict:
        title = str(ann.get("announcementTitle") or "")
        year, year_source = cls._parse_title_year(title)
        if year is None and ann.get("_time_ms"):
            # 年报通常在次年 1-6 月披露；无年份标题用公告时间倒推一年
            year = datetime.fromtimestamp(ann["_time_ms"] / 1000).year - 1
            year_source = "announcement_time"
        low = title.lower()
        is_summary = ("摘要" in title or "abstract" in low or "summary" in low)
        is_english = ("annual report" in low and "年度报告" not in title) or "英文" in title or "english" in low
        is_graphics = any(k in title for k in ("图文版", "图片版", "视觉版", "图文"))
        is_cancelled = any(k in title for k in ("已取消", "取消", "作废"))
        is_correction = any(k in title for k in ("更正", "修订", "修正", "更新后"))
        is_chinese_full = ("年度报告" in title or "年报" in title) and not is_summary
        return {
            "title": title,
            "year": year,
            "year_source": year_source,
            "is_summary": is_summary,
            "is_english": is_english,
            "is_graphics": is_graphics,
            "is_cancelled": is_cancelled,
            "is_correction": is_correction,
            "is_chinese_full": is_chinese_full,
            "time_ms": int(ann.get("_time_ms") or 0),
            "ann": ann,
        }

    def _pick_annual(self, anns: list[dict], report_period: str = "-") -> dict:
        candidates = []
        for ann in anns:
            c = self._classify(ann)
            if c["is_cancelled"] or c["is_summary"] or c["is_graphics"]:
                continue
            if c["year"] is None:
                continue
            candidates.append(c)
        if not candidates:
            raise ValueError("未找到可用的年度报告公告（全部为摘要/图文/已取消或无法解析年份）")

        if report_period and report_period != "-":
            m = re.match(r"((?:19|20)\d{2})", str(report_period))
            target_year = int(m.group(1)) if m else max(c["year"] for c in candidates)
        else:
            target_year = max(c["year"] for c in candidates)

        same_year = [c for c in candidates if c["year"] == target_year]
        if not same_year:
            years = sorted({c["year"] for c in candidates}, reverse=True)
            raise ValueError(f"未找到 {target_year} 年年报（巨潮可用年份: {years[:8]}）")

        def score(c):
            s = 0
            if c["is_chinese_full"]:
                s += 1000
            if not c["is_english"]:
                s += 100
            if c["is_correction"]:
                s += 20
            # 同年多份时优先最新公告
            s += min(c["time_ms"] / 1e13, 9)
            return s

        chosen = max(same_year, key=score)
        # 英文版仅在完全没有中文全文时兜底
        chinese = [c for c in same_year if c["is_chinese_full"] and not c["is_english"]]
        if chinese:
            chosen = max(chinese, key=score)
        chosen["period"] = f"{target_year}-12-31"
        chosen["name"] = self._fallback_name(chosen["title"])
        return chosen

    @staticmethod
    def _fallback_name(title: str) -> str:
        s = str(title or "").strip()
        s = re.sub(r"^\d{6}[_：:\-\s]*", "", s)
        s = re.sub(r"^(?:19|20)\d{2}\s*年?\s*", "", s)
        s = re.sub(r"(?:19|20)\d{2}\s*年.*$", "", s)
        s = re.sub(r"(?:19|20)\d{2}.*$", "", s)
        s = re.sub(r"Annual\s+Report.*$", "", s, flags=re.I)
        s = re.sub(r"(摘要|年报|年度报告).*$", "", s)
        s = re.sub(r"[_：:\-\s]+$", "", s).strip()
        if s in ("", "公司"):
            return ""
        return s[:40]

    def latest_annual_period(self, code: str) -> str:
        chosen = self._pick_annual(self._query_annual(code), "-")
        return chosen["period"]

    # ---------- 主流程 ----------

    def fetch_report(self, code: str, report_period: str = "-") -> dict:
        code = str(code).strip().zfill(6)
        if report_period not in (None, "", "-") and not str(report_period).endswith("-12-31"):
            raise ValueError(f"当前只支持年报报告期（YYYY-12-31），收到: {report_period}")
        meta = self._stock_meta(code)
        anns = self._query_annual(code)
        if not anns:
            raise ValueError(f"未找到年报公告: {code}")
        chosen = self._pick_annual(anns, report_period)
        target = chosen["ann"]
        title = chosen["title"]
        period = chosen["period"]
        name = meta.get("name") or chosen.get("name") or code

        adjunct = str(target.get("adjunctUrl") or "").lstrip("/")
        if not adjunct:
            raise ValueError(f"公告缺少 PDF 地址: {title}")
        pdf_url = f"{self.STATIC}/{adjunct}"
        r = self._request("GET", pdf_url)
        if not r.content:
            raise ValueError(f"PDF 下载为空: {pdf_url}")
        if b"%PDF" not in r.content[:1024] and "pdf" not in r.headers.get("Content-Type", "").lower():
            raise ValueError(f"下载内容不是 PDF（可能是反爬页面）: {pdf_url}")

        meta_pdf = pdf_to_text_with_meta(r.content)
        raw_text = meta_pdf["text"]
        pages = meta_pdf["pages"]
        text = clean_text(raw_text)
        if not text or len(text) < 300:
            raise LowQualityTextError(
                f"PDF 无可提取文本或文本过短（pages={pages}, chars={len(text)}），"
                f"可能为扫描件: {title}"
            )
        business = extract_business_section(text)
        warnings = []
        quality = "ok"
        if not business or len(business) < 80:
            quality = "poor"
            warnings.append(f"业务章节过短（{len(business)}字）")
        if looks_like_toc_only(business):
            quality = "poor"
            warnings.append("业务章节疑似仅含目录")
        if len(text) / max(pages, 1) < 30:
            quality = "poor"
            warnings.append(f"页均文本过少（{len(text) / max(pages, 1):.0f} 字/页），疑似图文版")

        return {
            "name": name,
            "short_name": name,
            "text": text,
            "business_text": business,
            "period": period,
            "title": title,
            "source_url": pdf_url,
            "announcement_time": self._announcement_time_iso(target.get("_time_ms")),
            "announcement_type": "更正/修订" if chosen.get("is_correction") else "年度报告",
            "pdf_pages": pages,
            "text_chars": len(text),
            "business_chars": len(business),
            "quality": quality,
            "warnings": warnings,
        }
