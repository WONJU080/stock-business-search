"""股票基础信息（代码/简称/巨潮 orgId）与本地快照。

巨潮的 `szse_stock.json` 同时提供 `zwjc`（正确股票简称）和 `orgId`，
是修复历史错误名称、避免重复抓取股票列表的可靠来源。
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import requests

DB_ROOT = Path(__file__).resolve().parent.parent / "db"
CACHE_PATH = DB_ROOT / "stock_universe.json"
CNINFO_STOCK_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

_FULL_TO_HALF = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)


def normalize_name(value):
    return str(value or "").translate(_FULL_TO_HALF).replace(" ", "").replace("\u3000", "").strip()


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _get(url, timeout=30):
    r = requests.get(url, headers=_HEADERS, timeout=timeout)
    if r.status_code == 403:
        try:
            from curl_cffi import requests as cffi
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("巨潮返回 403，请安装 curl_cffi") from exc
        r = cffi.get(url, headers=_HEADERS, timeout=timeout, impersonate="chrome")
    r.raise_for_status()
    return r


def fetch_stock_list(timeout=30):
    r = _get(CNINFO_STOCK_URL, timeout=timeout)
    raw = r.json().get("stockList") or []
    items = []
    for it in raw:
        code = str(it.get("code", "")).strip().zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue
        items.append({
            "code": code,
            "name": str(it.get("zwjc", "")).strip(),
            "org_id": str(it.get("orgId", "")).strip(),
            "category": str(it.get("category", "")).strip(),
            "pinyin": str(it.get("pinyin", "")).strip(),
        })
    if not items:
        raise ValueError("巨潮股票列表为空")
    _atomic_write_json(CACHE_PATH, {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": CNINFO_STOCK_URL,
        "items": items,
    })
    return items


def load_stock_list(refresh=False):
    if not refresh and CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            items = data.get("items") or []
            if items:
                return items
        except (json.JSONDecodeError, OSError):
            pass
    return fetch_stock_list()


def name_map(refresh=False):
    return {it["code"]: it["name"] for it in load_stock_list(refresh=refresh) if it.get("name")}


def org_map(refresh=False):
    return {it["code"]: it["org_id"] for it in load_stock_list(refresh=refresh) if it.get("org_id")}


def stock_meta_map(refresh=False):
    return {it["code"]: it for it in load_stock_list(refresh=refresh)}
