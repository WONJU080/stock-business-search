"""股票业务数据库：读写/更新/检索/查重。

设计约束：
- 纯文本仍是唯一事实来源，索引可由明细文件重建。
- 所有写操作通过同一个跨进程锁 + 原子替换完成，避免并发覆盖。
- 更新已有股票时保留「首次分析日期」，除非调用方显式传入。
- 写入前做基本字段校验，拒绝会破坏格式的内容（`|`、换行、空业务等）。
"""

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

DB_ROOT = Path(__file__).resolve().parent.parent / "db"
STOCKS_DIR = DB_ROOT / "stocks"
INDEX_PATH = DB_ROOT / "index.txt"
LOCK_PATH = DB_ROOT / ".write.lock"
RUNS_PATH = DB_ROOT / "runs.jsonl"

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

CODE_RE = re.compile(r"^\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KNOWN_SECTIONS = ("在营业务", "关键词")

# 模型/抓取失败时常见的“拒绝话术”，不应作为业务内容入库。
REFUSAL_RE = re.compile(
    r"(无法提炼|无法识别.{0,12}业务|未包含.{0,12}业务|未提供.{0,12}业务|"
    r"未披露.{0,12}业务|文本为空|仅含(目录|标题)|无具体在营业务|"
    r"未提供.{0,12}文本|无法提炼业务)"
)

GENERIC_KEYWORDS = {
    "年报", "年度报告", "管理层讨论与分析", "报告期内", "在营业务", "业务情况",
    "无", "未披露",
}

_THREAD_LOCK = threading.RLock()


def normalize_code(code):
    s = re.sub(r"\D", "", str(code))
    if len(s) != 6:
        raise ValueError(f"股票代码必须为6位数字: {code!r}")
    return s


def _relpath(code):
    return f"stocks/{code}.txt"


# ---------------------------------------------------------------------------
# 锁与原子写
# ---------------------------------------------------------------------------

@contextmanager
def _write_lock():
    """跨进程写锁 + 进程内线程锁。"""
    DB_ROOT.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        lock_file = open(LOCK_PATH, "a+", encoding="utf-8")
        try:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows 兜底
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()


def _atomic_write_text(path, text):
    """同目录临时文件 + fsync + os.replace，避免半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise


# ---------------------------------------------------------------------------
# 字段校验
# ---------------------------------------------------------------------------

def _valid_date(value):
    if not value or value == "-":
        return False
    if not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _clean_name(name):
    if name is None:
        raise ValueError("名称不能为空")
    s = str(name).replace("\r", " ").replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace("|", "｜")
    s = s.rstrip(":：").strip()
    if not s:
        raise ValueError("名称不能为空")
    if len(s) > 80:
        raise ValueError(f"名称过长（>80）: {s[:20]}...")
    return s


def _clean_report_period(report_period):
    if report_period in (None, ""):
        return "-"
    s = str(report_period).strip()
    if s == "-":
        return s
    if not _valid_date(s):
        raise ValueError(f"报告期必须为 YYYY-MM-DD 或 '-': {report_period!r}")
    return s


def _clean_date(value, field, default=None):
    if value in (None, "", "-"):
        value = default
    if value in (None, "", "-"):
        raise ValueError(f"{field}不能为空")
    s = str(value).strip()
    if not _valid_date(s):
        raise ValueError(f"{field}必须为 YYYY-MM-DD: {value!r}")
    return s


def is_pure_refusal(text):
    """短文本且以拒绝话术开头才算拒绝；详细业务里顺带一句“未提供…”不算。"""
    b = (text or "").strip()
    if not b:
        return True
    start_re = (
        r"^(无法提炼|未提供|未包含|未披露|所给文本|给定文本|提供的文本|"
        r"本段未|该段未|文本仅|仅含|经检查|公司无具体|公司未提供|无具体在营业务|"
        r"该文本|上文)"
    )
    if len(b) <= 500 and re.match(start_re, b):
        return True
    if len(b) <= 80 and REFUSAL_RE.search(b):
        return True
    return False


def validate_business(business):
    b = (business or "").strip()
    if not b:
        raise ValueError("在营业务不能为空")
    if is_pure_refusal(b):
        raise ValueError(f"在营业务疑似模型拒绝话术，拒绝入库: {b[:60]!r}")
    for line in b.splitlines():
        if line.strip() in ("[在营业务]", "[关键词]"):
            raise ValueError("在营业务中包含区块标题，拒绝入库以免破坏文件结构")
    return b


def normalize_keywords(keywords, drop_generic=True, limit=30):
    if keywords is None:
        items = []
    elif isinstance(keywords, str):
        items = re.split(r"[,，\n]", keywords)
    else:
        items = list(keywords)
    out = []
    seen = set()
    for item in items:
        s = re.sub(r"\s+", " ", str(item)).strip().strip(",，;；")
        if not s:
            continue
        if drop_generic and s in GENERIC_KEYWORDS:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if limit is not None and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------

def load_index_with_errors():
    """返回 (records, errors)。errors 为格式异常行，便于审计而非静默丢弃。"""
    records = {}
    errors = []
    if not INDEX_PATH.exists():
        return records, errors
    for lineno, raw in enumerate(INDEX_PATH.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 6:
            errors.append({"line": lineno, "error": f"应为6列，实际{len(parts)}列", "raw": raw})
            continue
        code, name, report, analyzed, updated, relpath = [p.strip() for p in parts]
        if not CODE_RE.match(code):
            errors.append({"line": lineno, "error": f"非法代码: {code!r}", "raw": raw})
            continue
        if code in records:
            errors.append({"line": lineno, "error": f"重复代码: {code}", "raw": raw})
        if not _valid_date(analyzed) or not _valid_date(updated):
            errors.append({"line": lineno, "error": "日期格式非法", "raw": raw})
        if report != "-" and not _valid_date(report):
            errors.append({"line": lineno, "error": f"报告期格式非法: {report!r}", "raw": raw})
        expected = _relpath(code)
        if relpath != expected:
            errors.append({"line": lineno, "error": f"文件路径异常: {relpath!r}", "raw": raw})
        records[code] = {
            "code": code,
            "name": name,
            "report_period": report,
            "analyzed_at": analyzed,
            "updated_at": updated,
            "path": relpath,
        }
    return records, errors


def load_index():
    records, _ = load_index_with_errors()
    return records


def _write_index_locked(records):
    """调用方必须已持有 _write_lock。"""
    lines = list(INDEX_HEADER)
    for code in sorted(records):
        r = records[code]
        lines.append("|".join([
            r["code"], r["name"], r["report_period"],
            r["analyzed_at"], r["updated_at"], r["path"],
        ]))
    _atomic_write_text(INDEX_PATH, "\n".join(lines) + "\n")


def rebuild_index():
    """从 db/stocks/*.txt 重建总揽，返回统计与错误。"""
    records = {}
    errors = []
    files = sorted(STOCKS_DIR.glob("*.txt"))
    for path in files:
        code = path.stem
        if not CODE_RE.match(code):
            errors.append({"file": path.name, "error": "文件名不是6位代码"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = _parse_text(text, code=code, path=_relpath(code))
        except Exception as exc:
            errors.append({"file": path.name, "error": str(exc)})
            continue
        if data.get("meta_code") and data["meta_code"] != code:
            errors.append({"file": path.name, "error": f"元数据代码不一致: {data['meta_code']}"})
            continue
        if not data.get("name"):
            errors.append({"file": path.name, "error": "名称为空"})
        records[code] = {
            "code": code,
            "name": data.get("name") or code,
            "report_period": data.get("report_period") or "-",
            "analyzed_at": data.get("analyzed_at") or "-",
            "updated_at": data.get("updated_at") or "-",
            "path": _relpath(code),
        }
    with _write_lock():
        _write_index_locked(records)
    return {"count": len(records), "files": len(files), "errors": errors}


def is_analyzed(code):
    return normalize_code(code) in load_index()


def filter_unanalyzed(codes):
    index = load_index()
    out = []
    for c in codes:
        code = normalize_code(c)
        if code not in index:
            out.append(code)
    return out


def list_analyzed():
    return sorted(load_index().values(), key=lambda r: r["code"])


# ---------------------------------------------------------------------------
# 明细文件
# ---------------------------------------------------------------------------

def parse_stock_file(code):
    code = normalize_code(code)
    path = STOCKS_DIR / f"{code}.txt"
    if not path.exists():
        raise FileNotFoundError(f"未找到股票明细文件: {path}")
    data = _parse_text(path.read_text(encoding="utf-8"), code=code, path=_relpath(code))
    if data.get("meta_code") and data["meta_code"] != code:
        raise ValueError(f"明细文件代码不一致: 文件名={code}, 元数据={data['meta_code']}")
    return data


def _parse_text(text, code=None, path=None):
    meta = {}
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            if section in KNOWN_SECTIONS:
                current = section
                sections.setdefault(current, [])
                continue
        if current is None:
            if "=" in line:
                key, val = line.split("=", 1)
                meta[key.strip()] = val.strip()
        else:
            sections[current].append(line)
    keywords = normalize_keywords(
        "\n".join(sections.get("关键词", [])),
        drop_generic=False,
        limit=None,
    )
    return {
        "code": code or meta.get("代码", ""),
        "meta_code": meta.get("代码", ""),
        "name": meta.get("名称", ""),
        "report_period": meta.get("报告期", "-"),
        "analyzed_at": meta.get("分析日期", "-"),
        "updated_at": meta.get("更新时间", "-"),
        "business": "\n".join(sections.get("在营业务", [])).strip(),
        "keywords": keywords,
        "path": path or "",
    }


def _existing_meta(code):
    """读取已有记录的元数据；用于更新时保留首次分析日期。"""
    path = STOCKS_DIR / f"{code}.txt"
    if not path.exists():
        return None
    try:
        return _parse_text(path.read_text(encoding="utf-8"), code=code, path=_relpath(code))
    except Exception:
        return None


def save_stock(code, name, business, report_period="-", keywords=None,
               analyzed_at=None, updated_at=None):
    code = normalize_code(code)
    name = _clean_name(name)
    business = validate_business(business)
    report_period = _clean_report_period(report_period)
    keywords = normalize_keywords(keywords)
    today = date.today().isoformat()

    with _write_lock():
        existing = _existing_meta(code)
        if analyzed_at in (None, "", "-"):
            analyzed_at = (existing or {}).get("analyzed_at") or today
        analyzed_at = _clean_date(analyzed_at, "分析日期", today)
        updated_at = _clean_date(updated_at, "更新时间", today)

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
            + business
            + "\n\n[关键词]\n"
            + ", ".join(keywords)
            + "\n"
        )

        relpath = _relpath(code)
        _atomic_write_text(STOCKS_DIR / f"{code}.txt", content)

        records = load_index()
        records[code] = {
            "code": code,
            "name": name,
            "report_period": report_period,
            "analyzed_at": analyzed_at,
            "updated_at": updated_at,
            "path": relpath,
        }
        _write_index_locked(records)
    return relpath


def rename_stock(code, name):
    """只改名称，保留业务/关键词/日期；用于修复历史错误名称。"""
    code = normalize_code(code)
    name = _clean_name(name)
    path = STOCKS_DIR / f"{code}.txt"
    if not path.exists():
        raise FileNotFoundError(f"未找到股票明细文件: {path}")
    with _write_lock():
        text = path.read_text(encoding="utf-8")
        new_text, n = re.subn(r"^名称 = .*$", lambda m: f"名称 = {name}", text, count=1, flags=re.M)
        if n == 0:
            raise ValueError(f"文件中没有名称字段: {path}")
        _atomic_write_text(path, new_text)
        records = load_index()
        if code in records and records[code]["name"] != name:
            records[code]["name"] = name
            _write_index_locked(records)
        elif code not in records:
            # 明细存在但索引缺失：补一条，日期取文件内元数据
            data = _parse_text(new_text, code=code, path=_relpath(code))
            records[code] = {
                "code": code,
                "name": name,
                "report_period": data.get("report_period") or "-",
                "analyzed_at": data.get("analyzed_at") or date.today().isoformat(),
                "updated_at": data.get("updated_at") or date.today().isoformat(),
                "path": _relpath(code),
            }
            _write_index_locked(records)
    return True


def bulk_rename(mapping):
    """批量修复名称：一次加锁、一次重写索引；业务/关键词/日期保持不变。"""
    cleaned = {}
    for code, name in (mapping or {}).items():
        cleaned[normalize_code(code)] = _clean_name(name)
    renamed = 0
    skipped = 0
    errors = []
    with _write_lock():
        records = load_index()
        for code, name in sorted(cleaned.items()):
            path = STOCKS_DIR / f"{code}.txt"
            if not path.exists():
                errors.append({"code": code, "error": "明细文件不存在"})
                continue
            try:
                text = path.read_text(encoding="utf-8")
                new_text, n = re.subn(
                    r"^名称 = .*$", lambda m: f"名称 = {name}", text, count=1, flags=re.M
                )
                if n == 0:
                    errors.append({"code": code, "error": "文件中没有名称字段"})
                    continue
                m = re.search(r"^名称 = (.*)$", text, flags=re.M)
                if m and m.group(1).strip() == name:
                    if records.get(code, {}).get("name") != name and code in records:
                        records[code]["name"] = name
                    skipped += 1
                    continue
                _atomic_write_text(path, new_text)
                renamed += 1
                if code in records:
                    records[code]["name"] = name
                else:
                    data = _parse_text(new_text, code=code, path=_relpath(code))
                    records[code] = {
                        "code": code,
                        "name": name,
                        "report_period": data.get("report_period") or "-",
                        "analyzed_at": data.get("analyzed_at") or date.today().isoformat(),
                        "updated_at": data.get("updated_at") or date.today().isoformat(),
                        "path": _relpath(code),
                    }
            except Exception as exc:
                errors.append({"code": code, "error": str(exc)})
        if renamed:
            _write_index_locked(records)
    return {"renamed": renamed, "skipped": skipped, "errors": errors}


def delete_stock(code):
    code = normalize_code(code)
    path = STOCKS_DIR / f"{code}.txt"
    with _write_lock():
        existed = path.exists()
        if existed:
            path.unlink()
        records = load_index()
        in_index = code in records
        if in_index:
            del records[code]
            _write_index_locked(records)
    return existed or in_index


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

def search(keywords, limit=None):
    """检索业务/关键词/名称/代码。

    返回每项含 `matched`、`local_score`、`snippet`，按本地得分降序。
    这是候选召回层；语义排序交给 search_flow / LLM。
    """
    if isinstance(keywords, str):
        keywords = [keywords]
    keywords = [str(k).strip() for k in keywords if str(k).strip()]
    if not keywords:
        return []
    lowered = [(kw, kw.casefold()) for kw in keywords]

    results = []
    for rec in load_index().values():
        try:
            data = parse_stock_file(rec["code"])
        except (FileNotFoundError, ValueError):
            continue
        business = data["business"]
        business_low = business.casefold()
        name_low = (data.get("name") or "").casefold()
        code = data["code"]
        kw_list = [k.casefold() for k in data["keywords"]]
        matched = []
        score = 0
        snippet = ""
        for original, kw_low in lowered:
            hit_score = 0
            if kw_low == code:
                hit_score = 8
            elif kw_low and kw_low == name_low:
                hit_score = 6
            elif kw_low and kw_low in name_low:
                hit_score = 4
            if kw_low and kw_low in business_low:
                hit_score = max(hit_score, 2)
                if not snippet:
                    idx = business_low.find(kw_low)
                    snippet = business[max(0, idx - 30): idx + 90].replace("\n", " ")
            if any(kw_low in item for item in kw_list):
                hit_score = max(hit_score, 3)
            if hit_score:
                matched.append(original)
                score += hit_score
        if matched:
            results.append({
                **data,
                "matched": matched,
                "local_score": score,
                "snippet": snippet or business[:120],
            })
    results.sort(key=lambda r: (-r["local_score"], r["code"]))
    if limit is not None:
        results = results[:limit]
    return results


# ---------------------------------------------------------------------------
# 运行日志（审计/重试用）
# ---------------------------------------------------------------------------

def log_run(record):
    """追加一条分析运行记录到 db/runs.jsonl，永不覆盖。"""
    payload = {"ts": datetime.now().isoformat(timespec="seconds")}
    payload.update(record or {})
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        line = json.dumps({k: str(v) for k, v in payload.items()}, ensure_ascii=False)
    with _write_lock():
        DB_ROOT.mkdir(parents=True, exist_ok=True)
        with open(RUNS_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def read_runs(limit=100, status=None):
    if not RUNS_PATH.exists():
        return []
    rows = []
    for line in RUNS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if status and row.get("status") != status:
            continue
        rows.append(row)
    return rows[-limit:]


def _fmt_index(r):
    return (f"{r['code']}\t{r['name']}\t报告期:{r['report_period']}"
            f"\t分析:{r['analyzed_at']}\t更新:{r['updated_at']}")


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
    p_search.add_argument("--limit", type=int, default=None)
    def _search(a):
        for r in search(a.keywords, limit=a.limit):
            print(f"{r['code']}\t{r['name']}\t命中:{','.join(r['matched'])}")
            print(f"  {r['snippet']}")
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

    p_rename = sub.add_parser("rename", help="修复单只股票名称（保留业务/日期）")
    p_rename.add_argument("code")
    p_rename.add_argument("name")
    def _rename(a):
        rename_stock(a.code, a.name)
        print(f"已改名: {normalize_code(a.code)}\t{a.name}")
    p_rename.set_defaults(func=_rename)

    p_rebuild = sub.add_parser("rebuild-index", help="从 db/stocks 重建总揽")
    def _rebuild(a):
        res = rebuild_index()
        print(f"已重建: {res['count']} 条（扫描 {res['files']} 个文件）")
        for e in res["errors"][:20]:
            print(f"  警告: {e}")
    p_rebuild.set_defaults(func=_rebuild)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    _cli()
