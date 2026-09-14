try:
    from . import db
    from .llm import StubLLM
    from .fetcher import CninfoFetcher, FileFetcher
    from .universe import resolve_stocks, AkshareUniverse
except ImportError:
    import db
    from llm import StubLLM
    from fetcher import CninfoFetcher, FileFetcher
    from universe import resolve_stocks, AkshareUniverse

from concurrent.futures import ThreadPoolExecutor


def analyze(spec, report_period="-", llm=None, fetcher=None, universe=None, force=False,
            on_progress=None, workers=4):
    """分析入库链路：解析范围 -> 查重 -> 获取财报 -> 提取 -> 入库。

    返回每只股票的处理结果，每项 {"code","name","status"}，
    status ∈ {"saved","skipped","error"}，error 时附 "error" 字段。
    on_progress 可选，每处理完一只调用一次，参数为该项结果。
    workers 为并发数（下载财报 + 提取并发，入库串行）。
    """
    llm = llm or StubLLM()
    fetcher = fetcher or CninfoFetcher()
    universe = universe or AkshareUniverse()

    stocks = resolve_stocks(spec, universe)
    analyzed = db.load_index()
    results = []

    todo = []
    for s in stocks:
        code = s["code"]
        if not force and code in analyzed:
            item = {"code": code, "name": s["name"] or analyzed[code]["name"], "status": "skipped"}
            results.append(item)
            if on_progress:
                on_progress(item)
        else:
            todo.append(s)

    def process(s):
        code = s["code"]
        try:
            fetched = fetcher.fetch_report(code, report_period)
            business_input = fetched.get("business_text") or fetched["text"]
            extracted = llm.extract_business(business_input)
            name = fetched["name"] or s["name"] or code
            actual_period = fetched.get("period") or report_period
            return {
                "code": code, "name": name, "status": "saved",
                "business": extracted["business"],
                "keywords": extracted.get("keywords", []),
                "report_period": actual_period,
            }
        except Exception as e:
            return {"code": code, "name": s["name"], "status": "error", "error": str(e)}

    if todo:
        if workers <= 1 or len(todo) == 1:
            items = [process(s) for s in todo]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                items = list(ex.map(process, todo))

        for it in items:
            if it["status"] == "saved":
                db.save_stock(
                    code=it["code"],
                    name=it["name"],
                    business=it["business"],
                    report_period=it["report_period"],
                    keywords=it["keywords"],
                )
                results.append({"code": it["code"], "name": it["name"], "status": "saved"})
            else:
                results.append({"code": it["code"], "name": it["name"], "status": "error", "error": it["error"]})
            if on_progress:
                on_progress(results[-1])

    return results


def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        prog="analyze_flow.py",
        description="分析入库链路：范围 -> 查重 -> 财报 -> 提取 -> 入库",
    )
    parser.add_argument("spec", help='如 "600519"、"600519 贵州茅台"、"板块:人工智能"')
    parser.add_argument("--report", default="-")
    parser.add_argument("--force", action="store_true", help="忽略查重，强制重新分析")
    parser.add_argument("--reports-dir", default=None, help="使用 FileFetcher 并指定财报目录")
    args = parser.parse_args()

    fetcher = FileFetcher(args.reports_dir) if args.reports_dir else CninfoFetcher()
    results = analyze(
        args.spec,
        report_period=args.report,
        fetcher=fetcher,
        force=args.force,
    )
    for r in results:
        line = f"{r['code']}\t{r.get('name', '')}\t{r['status']}"
        if r["status"] == "error":
            line += f"\t{r['error']}"
        print(line)


if __name__ == "__main__":
    _cli()
