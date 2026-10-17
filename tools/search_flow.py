"""检索链路：原词 + AI 关联词 -> 本地召回 -> AI 重排（带本地兜底）。"""

import re

try:
    from . import db
    from .llm import StubLLM
except ImportError:  # pragma: no cover
    import db
    from llm import StubLLM


def _dedupe(items):
    seen = set()
    out = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _local_rank(results):
    ranked = sorted(
        results,
        key=lambda r: (r.get("local_score", 0), len(r.get("matched", []))),
        reverse=True,
    )
    for i, r in enumerate(ranked):
        r["score"] = r.get("local_score", 0)
        r["rank"] = i + 1
        r["ranked_by"] = "local"
    return ranked


def search(query, llm=None, limit=None):
    """检索链路：原词 + AI 关联词 -> 程序筛选 -> AI 排序。

    返回 {"query", "related", "results", "warnings", "total_hits"}。
    LLM 扩展/排序失败时不会让整个检索失败，会回退本地排序。
    """
    llm = llm or StubLLM()
    warnings = []
    related = [query]
    if re.fullmatch(r"\d{6}", str(query).strip()):
        # 代码检索不做关联词扩展，避免把 600519 扩展到白酒板块
        hits = db.search([str(query).strip()])
        if not hits:
            return {"query": query, "related": [query], "results": [],
                    "warnings": [], "total_hits": 0}
        ranked = _local_rank(hits)
        if limit is not None:
            ranked = ranked[:limit]
        return {"query": query, "related": [query], "results": ranked,
                "warnings": [], "total_hits": len(hits)}
    try:
        expanded = llm.expand_keywords(query)
        if isinstance(expanded, str):
            expanded = [expanded]
        related = _dedupe([query, *(expanded or [])])
    except Exception as exc:
        warnings.append(f"关联词扩展失败，仅用原词检索: {exc}")

    hits = db.search(related)
    if not hits:
        return {
            "query": query, "related": related, "results": [],
            "warnings": warnings, "total_hits": 0,
        }

    try:
        ranked = llm.rank_results(query, hits) or []
    except Exception as exc:
        warnings.append(f"AI 重排失败，已回退本地排序: {exc}")
        ranked = _local_rank(hits)
    if not ranked:
        warnings.append("AI 未返回排序结果，已回退本地排序")
        ranked = _local_rank(hits)

    if limit is not None:
        ranked = ranked[:limit]
    return {
        "query": query,
        "related": related,
        "results": ranked,
        "warnings": warnings,
        "total_hits": len(hits),
    }


def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        prog="search_flow.py",
        description="检索链路：关键词 -> 关联词 -> 筛选 -> 排序",
    )
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    res = search(args.query, limit=args.limit)
    print(f"原词  : {res['query']}")
    print(f"关联词: {' / '.join(res['related'])}")
    print(f"命中 {res['total_hits']} 只股票，展示 {len(res['results'])} 只:")
    for w in res.get("warnings", []):
        print(f"警告: {w}")
    for r in res["results"]:
        print(f"  {r['code']}  {r['name']}  关联度:{r.get('score', '-')}  "
              f"排序:{r.get('ranked_by', '-')}  命中:{','.join(r['matched'])}")
        print(f"    {r.get('snippet', r['business'][:100])}")


if __name__ == "__main__":
    _cli()
