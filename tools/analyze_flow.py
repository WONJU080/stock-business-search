"""分析入库链路：解析范围 -> 查重 -> 获取财报 -> 提取 -> 入库。"""

from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from . import db
    from .llm import StubLLM, configure_max_concurrent
    from .fetcher import CninfoFetcher, FileFetcher, LowQualityTextError
    from .universe import resolve_stocks, AkshareUniverse
except ImportError:  # pragma: no cover
    import db
    from llm import StubLLM, configure_max_concurrent
    from fetcher import CninfoFetcher, FileFetcher, LowQualityTextError
    from universe import resolve_stocks, AkshareUniverse


def _safe_log(record):
    try:
        db.log_run(record)
    except Exception:
        pass


def _source_fields(fetched):
    return {
        "title": fetched.get("title", ""),
        "source_url": fetched.get("source_url", ""),
        "announcement_time": fetched.get("announcement_time", ""),
        "pdf_pages": fetched.get("pdf_pages"),
        "text_chars": fetched.get("text_chars"),
        "business_chars": fetched.get("business_chars"),
        "quality": fetched.get("quality", "ok"),
        "warnings": fetched.get("warnings", []),
    }


def analyze(spec=None, report_period="-", llm=None, fetcher=None, universe=None, force=False,
            on_progress=None, workers=4, codes=None, stop_event=None):
    """分析入库链路。

    spec 与 codes 二选一；codes 用于按代码清单重跑（如 db/reanalysis.txt）。
    返回每项 {"code","name","status"}，status ∈ {"saved","skipped","error"}。
    """
    try:
        from .config import load_config
    except ImportError:  # pragma: no cover
        from config import load_config
    try:
        configure_max_concurrent(load_config().get("llm", {}).get("max_concurrent", 16))
    except Exception:
        pass
    llm = llm or StubLLM()
    fetcher = fetcher or CninfoFetcher()
    universe = universe or AkshareUniverse()
    workers = max(1, int(workers or 1))

    if codes:
        seen_codes = set()
        stocks = []
        for c in codes:
            code = db.normalize_code(c)
            if code in seen_codes:
                continue
            seen_codes.add(code)
            stocks.append({"code": code, "name": ""})
    else:
        if not spec:
            raise ValueError("必须提供 spec 或 codes")
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
        fetched = {}
        try:
            base_name = fetcher.short_name(code)
        except Exception:
            base_name = s.get("name") or code
        try:
            fetched = fetcher.fetch_report(code, report_period)
            if fetched.get("quality") == "poor":
                raise LowQualityTextError(
                    f"财报文本质量不足: {'; '.join(fetched.get('warnings') or ['未定位到业务章节'])}"
                )
            business_input = fetched.get("business_text") or fetched.get("text") or ""
            if not business_input.strip():
                raise LowQualityTextError("财报业务文本为空")
            try:
                extracted = llm.extract_business(business_input)
            except Exception as exc:
                return {
                    "code": code, "name": fetched.get("short_name") or base_name or code,
                    "status": "error", "stage": "extract", "error": str(exc),
                    "fetched": fetched,
                }
            name = fetched.get("short_name") or base_name or fetched.get("name") or code
            actual_period = fetched.get("period") or report_period
            return {
                "code": code, "name": name, "status": "saved",
                "business": extracted["business"],
                "keywords": extracted.get("keywords", []),
                "report_period": actual_period,
                "prompt_version": extracted.get("prompt_version", ""),
                "fetched": fetched,
            }
        except Exception as exc:
            return {
                "code": code, "name": base_name or s.get("name") or code, "status": "error",
                "stage": "fetch", "error": str(exc), "fetched": fetched,
            }

    def finalize(item):
        fetched = item.pop("fetched", {}) or {}
        prompt_version = item.get("prompt_version", "")
        if item["status"] == "saved":
            try:
                db.save_stock(
                    code=item["code"],
                    name=item["name"],
                    business=item["business"],
                    report_period=item["report_period"],
                    keywords=item["keywords"],
                )
                item = {"code": item["code"], "name": item["name"], "status": "saved"}
            except Exception as exc:
                item = {
                    "code": item["code"], "name": item.get("name", ""),
                    "status": "error", "stage": "save", "error": str(exc),
                }
        if item["status"] == "error":
            _safe_log({
                "code": item["code"], "name": item.get("name", ""), "status": "error",
                "stage": item.get("stage", ""), "error": item.get("error", ""),
                **_source_fields(fetched),
            })
        else:
            _safe_log({
                "code": item["code"], "name": item.get("name", ""), "status": "saved",
                "report_period": fetched.get("period", ""),
                "model": getattr(llm, "model", ""),
                "prompt_version": prompt_version,
                **_source_fields(fetched),
            })
        results.append(item)
        if on_progress:
            on_progress(item)
        return item

    if todo:
        if workers <= 1 or len(todo) == 1:
            for s in todo:
                if stop_event is not None and stop_event.is_set():
                    break
                finalize(process(s))
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(process, s): s for s in todo}
                try:
                    for fut in as_completed(futures):
                        if stop_event is not None and stop_event.is_set():
                            for f in futures:
                                f.cancel()
                            break
                        finalize(fut.result())
                finally:
                    ex.shutdown(wait=True, cancel_futures=True)

    return results


def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        prog="analyze_flow.py",
        description="分析入库链路：范围 -> 查重 -> 财报 -> 提取 -> 入库",
    )
    parser.add_argument("spec", nargs="?", help='如 "600519"、"600519 贵州茅台"、"板块:人工智能"')
    parser.add_argument("--codes-file", default=None, help="按代码清单重跑（每行一个代码，支持 # 注释）")
    parser.add_argument("--report", default="-")
    parser.add_argument("--force", action="store_true", help="忽略查重，强制重新分析")
    parser.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    parser.add_argument("--reports-dir", default=None, help="使用 FileFetcher 并指定财报目录")
    args = parser.parse_args()

    codes = None
    if args.codes_file:
        codes = []
        for line in open(args.codes_file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            codes.append(line.split("\t")[0].strip())

    fetcher = FileFetcher(args.reports_dir) if args.reports_dir else CninfoFetcher()
    results = analyze(
        args.spec,
        report_period=args.report,
        fetcher=fetcher,
        force=args.force,
        workers=args.workers,
        codes=codes,
    )
    for r in results:
        line = f"{r['code']}\t{r.get('name', '')}\t{r['status']}"
        if r["status"] == "error":
            line += f"\t{r.get('stage', '')}: {r['error']}"
        print(line)


if __name__ == "__main__":
    _cli()
