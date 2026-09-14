try:
    from . import db
    from .llm import LLMClient, StubLLM
except ImportError:
    import db
    from llm import LLMClient, StubLLM


def _dedupe(items):
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def search(query, llm=None):
    """检索链路：原词 + AI 关联词 -> 程序筛选 -> AI 排序。

    返回 {"query", "related", "results"}；results 各项含 code/name/business/
    keywords/matched 及模型追加的 score/rank。
    """
    llm = llm or StubLLM()
    related = _dedupe([query, *llm.expand_keywords(query)])
    hits = db.search(related)
    ranked = llm.rank_results(query, hits) if hits else []
    return {
        "query": query,
        "related": related,
        "results": ranked,
    }


def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        prog="search_flow.py",
        description="检索链路：关键词 -> 关联词 -> 筛选 -> 排序",
    )
    parser.add_argument("query")
    args = parser.parse_args()

    res = search(args.query)
    print(f"原词  : {res['query']}")
    print(f"关联词: {' / '.join(res['related'])}")
    print(f"命中 {len(res['results'])} 只股票:")
    for r in res["results"]:
        print(f"  {r['code']}  {r['name']}  关联度:{r.get('score', '-')}  命中:{','.join(r['matched'])}")
        print(f"    {r['business'][:100]}")


if __name__ == "__main__":
    _cli()
