"""历史数据修复：名称纠正、重建索引、生成需重跑清单。"""

from pathlib import Path

from . import db
from .audit import MALFORMED_NAME_RE
from .stock_list import name_map, normalize_name


def is_malformed_name(name):
    """只判断“明显是错误名称”，不把 ST/XD/正常更名当成错误。"""
    n = normalize_name(name)
    return (not n or MALFORMED_NAME_RE.search(n)
            or len(n) > 12)


def repair_names(dry_run=True, mode="malformed", refresh=False, limit=None):
    """修复历史名称。

    mode="malformed"：只修复全称、英文标题、代码前缀、尾冒号等明显错误（默认，安全）。
    mode="all"：把所有与巨潮简称不同的名称都同步为当前简称（会覆盖历史 ST 名称）。
    """
    expected = name_map(refresh=refresh)
    mapping = {}
    current_by_code = {}
    for path in sorted(db.STOCKS_DIR.glob("*.txt")):
        code = path.stem
        if code not in expected:
            continue
        try:
            current = db.parse_stock_file(code).get("name", "")
        except Exception:
            continue
        current_by_code[code] = current
        if normalize_name(current) == normalize_name(expected[code]):
            continue
        if mode == "malformed" and not is_malformed_name(current):
            continue
        mapping[code] = expected[code]
        if limit is not None and len(mapping) >= limit:
            break

    preview = [
        {"code": code, "current": current_by_code.get(code, ""), "expected": name}
        for code, name in list(mapping.items())[:50]
    ]
    result = {"targets": len(mapping), "preview": preview, "dry_run": dry_run, "mode": mode}
    if not dry_run and mapping:
        result.update(db.bulk_rename(mapping))
    return result


def list_name_diffs(limit=None):
    """列出所有与巨潮简称不同的名称，供人工决定是否同步。"""
    expected = name_map()
    rows = []
    for path in sorted(db.STOCKS_DIR.glob("*.txt")):
        code = path.stem
        if code not in expected:
            continue
        try:
            current = db.parse_stock_file(code).get("name", "")
        except Exception:
            continue
        if normalize_name(current) != normalize_name(expected[code]):
            rows.append({
                "code": code, "current": current, "expected": expected[code],
                "malformed": is_malformed_name(current),
            })
            if limit is not None and len(rows) >= limit:
                break
    return rows


def build_reanalysis_queue(report, path=None):
    """把拒绝话术、报告期异常、空业务等高危记录写成 TSV 清单。"""
    rows = []
    seen = set()
    for key, reason in (
        ("refusals", "refusal"),
        ("refusal_fragments", "refusal_fragment"),
        ("period_anomalies", "period"),
        ("empty_business", "empty_business"),
        ("unreadable_files", "unreadable"),
    ):
        for item in report.get(key, []):
            code = item.get("code") if isinstance(item, dict) else item
            if not code or code in seen:
                continue
            seen.add(code)
            if isinstance(item, dict):
                rows.append((code, item.get("name", ""), item.get("period", ""), reason))
            else:
                rows.append((code, "", "", reason))
    path = Path(path) if path else db.DB_ROOT / "reanalysis.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 代码\t名称\t报告期\t原因"]
    lines.extend("\t".join(map(str, row)) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, rows
