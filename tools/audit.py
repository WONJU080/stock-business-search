"""全量数据审计：索引/明细一致性、名称、报告期、拒绝话术、关键词质量。

用法：
    python3 main.py audit
    python3 main.py audit --json db/audit-report.json --refresh-names
"""

import json
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import db
from .stock_list import name_map, normalize_name

MALFORMED_NAME_RE = re.compile(
    r"股份有限公司|有限责任公司|有限公司|^公司$|年报$|Annual Report|Abstract|"
    r"H股公告|^\d{6}[_：:]|^XD|^3-|_$|-$|：|:"
)
REFUSAL_FRAGMENT_RE = re.compile(
    r"^(?:本段|该段|上文|该文本|文本)[^。\n]{0,60}(?:未|无法|仅)"
)

GENERIC_KEYWORDS = {
    "年报", "年度报告", "管理层讨论与分析", "报告期内", "在营业务", "业务情况",
    "无", "未披露",
}


def _text_stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def audit(check_names=True, refresh_names=False):
    index, index_errors = db.load_index_with_errors()
    files = sorted(p.stem for p in db.STOCKS_DIR.glob("*.txt"))
    file_set = set(files)
    index_set = set(index)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "records": len(index),
        "files": len(files),
        "index_errors": index_errors,
        "missing_files": sorted(index_set - file_set),
        "orphan_files": sorted(file_set - index_set),
        "metadata_mismatch": [],
        "unreadable_files": [],
        "empty_name": [],
        "empty_business": [],
        "refusals": [],
        "refusal_fragments": [],
        "short_business": [],
        "multiline_business": 0,
        "period_distribution": {},
        "period_anomalies": [],
        "empty_keywords": [],
        "junk_keywords": [],
        "keyword_stats": {},
        "name_check_error": None,
        "name_mismatch": [],
        "suspicious_names": [],
    }

    business_lengths = []
    keyword_counts = []
    all_keywords = []
    junk_records = []
    periods = Counter()

    for code in sorted(file_set):
        try:
            data = db.parse_stock_file(code)
        except Exception as exc:
            report["unreadable_files"].append({"code": code, "error": str(exc)})
            continue

        if not data.get("name"):
            report["empty_name"].append(code)
        business = data.get("business") or ""
        business_lengths.append(len(business))
        if not business:
            report["empty_business"].append(code)
        elif db.is_pure_refusal(business):
            report["refusals"].append({
                "code": code,
                "name": data.get("name", ""),
                "period": data.get("report_period", ""),
                "text": business[:100],
            })
        if business and not db.is_pure_refusal(business):
            fragments = [
                line.strip() for line in business.splitlines()
                if line.strip() and REFUSAL_FRAGMENT_RE.search(line)
            ]
            if fragments:
                report["refusal_fragments"].append({
                    "code": code, "name": data.get("name", ""), "fragments": fragments[:3],
                })
        if 0 < len(business) < 100:
            report["short_business"].append({
                "code": code, "name": data.get("name", ""), "chars": len(business),
                "text": business[:80],
            })
        if "\n" in business:
            report["multiline_business"] += 1

        period = data.get("report_period") or "-"
        periods[period] += 1
        if period != "2025-12-31":
            report["period_anomalies"].append({
                "code": code, "name": data.get("name", ""), "period": period,
            })

        kws = data.get("keywords") or []
        keyword_counts.append(len(kws))
        all_keywords.extend(kws)
        if not kws:
            report["empty_keywords"].append(code)
        junk = [k for k in kws if k in GENERIC_KEYWORDS]
        if junk:
            junk_records.append({"code": code, "keywords": junk})
        if any(len(k) == 1 for k in kws):
            pass

        if code in index:
            rec = index[code]
            for field, file_key in (
                ("name", "name"), ("report_period", "report_period"),
                ("analyzed_at", "analyzed_at"), ("updated_at", "updated_at"),
            ):
                if rec[field] != data.get(file_key):
                    report["metadata_mismatch"].append({
                        "code": code, "field": field,
                        "index": rec[field], "file": data.get(file_key),
                    })

        n = normalize_name(data.get("name", ""))
        if (not n or n == "公司" or n.endswith("：") or n.endswith(":")
                or MALFORMED_NAME_RE.search(n) or re.search(r"[A-Za-z]{5,}", n)
                or len(n) > 12):
            report["suspicious_names"].append({"code": code, "name": data.get("name", "")})

    report["period_distribution"] = dict(periods.most_common())
    report["business_length"] = _text_stats(business_lengths)
    report["keyword_stats"] = {
        "total": len(all_keywords),
        "unique": len(set(all_keywords)),
        "per_stock": _text_stats(keyword_counts),
        "one_char": sum(1 for k in all_keywords if len(k) == 1),
        "over_15": sum(1 for c in keyword_counts if c > 15),
        "empty": len(report["empty_keywords"]),
        "junk_records": len(junk_records),
    }
    report["junk_keywords"] = junk_records[:200]

    if check_names:
        try:
            expected = name_map(refresh=refresh_names)
        except Exception as exc:
            report["name_check_error"] = f"{type(exc).__name__}: {exc}"
        else:
            mismatches = []
            for code in sorted(file_set):
                if code not in expected:
                    continue
                try:
                    current = db.parse_stock_file(code).get("name", "")
                except Exception:
                    continue
                if normalize_name(current) != normalize_name(expected[code]):
                    mismatches.append({
                        "code": code, "current": current, "expected": expected[code],
                    })
            report["name_mismatch"] = mismatches
            report["name_expected_count"] = len(expected)

    report["summary"] = {
        "index_errors": len(report["index_errors"]),
        "missing_files": len(report["missing_files"]),
        "orphan_files": len(report["orphan_files"]),
        "metadata_mismatch": len(report["metadata_mismatch"]),
        "unreadable_files": len(report["unreadable_files"]),
        "empty_business": len(report["empty_business"]),
        "refusals": len(report["refusals"]),
        "refusal_fragments": len(report["refusal_fragments"]),
        "short_business": len(report["short_business"]),
        "multiline_business": report["multiline_business"],
        "period_anomalies": len(report["period_anomalies"]),
        "empty_keywords": len(report["empty_keywords"]),
        "junk_keyword_records": len(junk_records),
        "suspicious_names": len(report["suspicious_names"]),
        "name_mismatch": len(report["name_mismatch"]),
    }
    return report


def print_summary(report, max_examples=8):
    s = report["summary"]
    print(f"审计时间: {report['generated_at']}")
    print(f"索引记录: {report['records']} | 明细文件: {report['files']}")
    print(f"索引格式错误: {s['index_errors']} | 缺失文件: {s['missing_files']} | 孤儿文件: {s['orphan_files']}")
    print(f"元数据不一致: {s['metadata_mismatch']} | 不可读: {s['unreadable_files']}")
    print(f"名称为空: {len(report['empty_name'])} | 名称可疑: {s['suspicious_names']} | 与简称不符: {s['name_mismatch']}")
    if report.get("name_check_error"):
        print(f"名称核对跳过: {report['name_check_error']}")
    print(f"业务为空: {s['empty_business']} | 疑似拒绝话术: {s['refusals']} | "
          f"含拒绝片段: {s['refusal_fragments']} | <100字: {s['short_business']}")
    print(f"业务多段: {s['multiline_business']} | 长度: {report['business_length']}")
    print(f"报告期异常: {s['period_anomalies']} | 分布: {report['period_distribution']}")
    print(f"关键词: {report['keyword_stats']}")

    def _examples(title, items, fmt):
        if not items:
            return
        print(f"\n{title}:")
        for item in items[:max_examples]:
            print("  " + fmt(item))

    _examples("索引错误", report["index_errors"],
              lambda x: f"行{x['line']}: {x['error']} | {x['raw'][:80]}")
    _examples("缺失文件", report["missing_files"], lambda x: x)
    _examples("孤儿文件", report["orphan_files"], lambda x: x)
    _examples("元数据不一致", report["metadata_mismatch"],
              lambda x: f"{x['code']} {x['field']}: index={x['index']!r} file={x['file']!r}")
    _examples("名称可疑", report["suspicious_names"],
              lambda x: f"{x['code']} {x['name']}")
    _examples("名称与简称不符", report["name_mismatch"],
              lambda x: f"{x['code']} 当前={x['current']!r} 应为={x['expected']!r}")
    _examples("拒绝话术", report["refusals"],
              lambda x: f"{x['code']} {x['name']} {x['period']} | {x['text']}")
    _examples("业务含拒绝片段", report["refusal_fragments"],
              lambda x: f"{x['code']} {x['name']} | {x['fragments']}")
    _examples("报告期异常", report["period_anomalies"],
              lambda x: f"{x['code']} {x['name']} {x['period']}")
    _examples("关键词为空", report["empty_keywords"], lambda x: x)
    _examples("关键词含元词", report["junk_keywords"],
              lambda x: f"{x['code']} {x['keywords']}")


def write_report(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
