import re
import sys
from datetime import date
from pathlib import Path

DB_ROOT = Path(__file__).resolve().parent.parent / "db"
STOCKS_DIR = DB_ROOT / "stocks"
INDEX_PATH = DB_ROOT / "index.txt"

INDEX_HEADER = [
    "# 股票分析总揽",
    "# 字段: 代码|名称|报告期|分析日期|更新时间|文件路径",
]

META_FILE_ORDER = ("代码", "名称", "报告期", "分析日期", "更新时间")
FIELD_MAP = {
    "代码": "code",
    "名称": "name",
    "报告期": "report_period",
    "分析日期": "analyzed_at",
    "更新时间": "updated_at",
}
FIELD_MAP_REV = {v: k for k, v in FIELD_MAP.items()}


def normalize_code(code):
    s = re.sub(r"\D", "", str(code))
    if len(s) != 6:
        raise ValueError(f"股票代码必须为6位数字: {code!r}")
    return s


def _relpath(code):
    return f"stocks/{code}.txt"


def load_index():
    records = {}
    if not INDEX_PATH.exists():
        return records
    for line in INDEX_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 6:
            continue
        code, name, report, analyzed, updated, relpath = parts
        records[code] = {
            "code": code,
            "name": name,
            "report_period": report,
            "analyzed_at": analyzed,
            "updated_at": updated,
            "path": relpath,
        }
    return records


def _write_index(records):
    DB_ROOT.mkdir(parents=True, exist_ok=True)
    lines = list(INDEX_HEADER)
    for code in sorted(records):
        r = records[code]
        lines.append("|".join([
            r["code"], r["name"], r["report_period"],
            r["analyzed_at"], r["updated_at"], r["path"],
        ]))
    INDEX_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_analyzed(code):
    return normalize_code(code) in load_index()


def filter_unanalyzed(codes):
    index = load_index()
    return [normalize_code(c) for c in codes if normalize_code(c) not in index]


def list_analyzed():
    return sorted(load_index().values(), key=lambda r: r["code"])


def parse_stock_file(code):
    code = normalize_code(code)
    path = STOCKS_DIR / f"{code}.txt"
    if not path.exists():
        raise FileNotFoundError(f"未找到股票明细文件: {path}")
    return _parse_text(path.read_text(encoding="utf-8"), code=code, path=_relpath(code))


def _parse_text(text, code=None, path=None):
    meta = {}
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections[current] = []
        elif current is None:
            if "=" in line:
                key, val = line.split("=", 1)
                meta[key.strip()] = val.strip()
        else:
            sections[current].append(line)
    keywords = []
    for kw in ",".join(sections.get("关键词", [])).split(","):
        kw = kw.strip()
        if kw:
            keywords.append(kw)
    return {
        "code": code or meta.get("代码", ""),
        "name": meta.get("名称", ""),
        "report_period": meta.get("报告期", "-"),
        "analyzed_at": meta.get("分析日期", "-"),
        "updated_at": meta.get("更新时间", "-"),
        "business": "\n".join(sections.get("在营业务", [])).strip(),
        "keywords": keywords,
        "path": path or "",
    }


def save_stock(code, name, business, report_period="-", keywords=None,
               analyzed_at=None, updated_at=None):
    code = normalize_code(code)
    today = date.today().isoformat()
    analyzed_at = analyzed_at or today
    updated_at = updated_at or today
    report_period = report_period or "-"
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    meta_lines = [
        f"代码 = {code}",
        f"名称 = {name}",
        f"报告期 = {report_period}",
        f"分析日期 = {analyzed_at}",
        f"更新时间 = {updated_at}",
    ]
    content = (
        "\n".join(meta_lines)
        + "\n\n[在营业务]\n"
        + business.strip()
        + "\n\n[关键词]\n"
        + ", ".join(keywords)
        + "\n"
    )

    STOCKS_DIR.mkdir(parents=True, exist_ok=True)
    relpath = _relpath(code)
    (STOCKS_DIR / f"{code}.txt").write_text(content, encoding="utf-8")

    records = load_index()
    records[code] = {
        "code": code,
        "name": name,
        "report_period": report_period,
        "analyzed_at": analyzed_at,
        "updated_at": updated_at,
        "path": relpath,
    }
    _write_index(records)
    return relpath


def delete_stock(code):
    code = normalize_code(code)
    path = STOCKS_DIR / f"{code}.txt"
    if path.exists():
        path.unlink()
    records = load_index()
    if code in records:
        del records[code]
        _write_index(records)
        return True
    return False


def search(keywords):
    if isinstance(keywords, str):
        keywords = [keywords]
    keywords = [str(k).strip() for k in keywords if str(k).strip()]
    results = []
    for rec in load_index().values():
        try:
            data = parse_stock_file(rec["code"])
        except FileNotFoundError:
            continue
        haystack = data["business"].lower()
        kw_list = [k.lower() for k in data["keywords"]]
        matched = []
        for kw in keywords:
            k = kw.lower()
            if k in haystack or any(k in item for item in kw_list):
                matched.append(kw)
        if matched:
            results.append({**data, "matched": matched})
    results.sort(key=lambda r: len(r["matched"]), reverse=True)
    return results


def _cli():
    import argparse

    parser = argparse.ArgumentParser(prog="db.py", description="股票业务数据库工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出全部已分析股票")
    p_list.set_defaults(func=lambda a: [print(_fmt_index(r)) for r in list_analyzed()])

    p_check = sub.add_parser("check", help="检查股票是否已分析")
    p_check.add_argument("codes", nargs="+")
    def _check(a):
        index = load_index()
        for c in a.codes:
            c = normalize_code(c)
            status = "已分析" if c in index else "未分析"
            name = index[c]["name"] if c in index else "-"
            print(f"{c}\t{name}\t{status}")
    p_check.set_defaults(func=_check)

    p_search = sub.add_parser("search", help="按关键词检索")
    p_search.add_argument("keywords", nargs="+")
    def _search(a):
        for r in search(a.keywords):
            print(f"{r['code']}\t{r['name']}\t命中:{','.join(r['matched'])}")
            print(f"  {r['business'][:120]}")
    p_search.set_defaults(func=_search)

    p_save = sub.add_parser("save", help="保存/更新一只股票")
    p_save.add_argument("code")
    p_save.add_argument("name")
    p_save.add_argument("--business", required=True, help="在营业务文本文件路径")
    p_save.add_argument("--report", default="-")
    p_save.add_argument("--keywords", default="", help="逗号分隔")
    def _save(a):
        business = Path(a.business).read_text(encoding="utf-8")
        keywords = [k for k in a.keywords.split(",") if k.strip()]
        relpath = save_stock(a.code, a.name, business, report_period=a.report, keywords=keywords)
        print(f"已保存: {relpath}")
    p_save.set_defaults(func=_save)

    args = parser.parse_args()
    args.func(args)


def _fmt_index(r):
    return f"{r['code']}\t{r['name']}\t报告期:{r['report_period']}\t分析:{r['analyzed_at']}\t更新:{r['updated_at']}"


if __name__ == "__main__":
    _cli()
