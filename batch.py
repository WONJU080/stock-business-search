"""批量分析：多 API key 并发处理，保证每只股票只被处理一次。"""

import argparse
import threading
from collections import Counter
from queue import Empty, Queue

from tools import db
from tools.config import load_config, load_keys
from tools.fetcher import CninfoFetcher
from tools.llm import build_llm_from_key, configure_max_concurrent, get_max_concurrent
from tools.universe import resolve_stocks


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


def _safe_log(record):
    try:
        db.log_run(record)
    except Exception:
        pass


def run(spec=None, offset=0, limit=None, report_period="-", workers_per_key=2,
        force=False, on_progress=None, codes=None, stop_event=None):
    """批量分析：多 API key 并发处理。

    返回每只股票的结果 {"code","name","status"}，status ∈ saved/skipped/error。
    """
    if codes:
        seen = set()
        stocks = []
        for c in codes:
            code = db.normalize_code(c)
            if code in seen:
                continue
            seen.add(code)
            stocks.append({"code": code, "name": ""})
        stocks.sort(key=lambda s: s["code"])
    else:
        if not spec:
            raise ValueError("必须提供 spec 或 codes")
        stocks = sorted(resolve_stocks(spec), key=lambda s: s["code"])
        if limit is not None:
            stocks = stocks[offset:offset + limit]
        else:
            stocks = stocks[offset:]

    analyzed = db.load_index()
    results = []

    todo = []
    for s in stocks:
        if not force and s["code"] in analyzed:
            item = {"code": s["code"], "name": s["name"] or analyzed[s["code"]]["name"], "status": "skipped"}
            results.append(item)
            if on_progress:
                on_progress(item)
        else:
            todo.append(s)

    if not todo:
        return results

    keys = load_keys()
    if not keys:
        raise ValueError("未配置任何 API key（config.json 的 llm.api_keys 或 api_key）")
    workers_per_key = max(1, int(workers_per_key or 1))

    cfg = load_config()
    configure_max_concurrent(cfg.get("llm", {}).get("max_concurrent", 16))
    fetcher = CninfoFetcher()
    q = Queue()
    for s in todo:
        q.put(s)

    out_lock = threading.Lock()

    def worker(key):
        llm = build_llm_from_key(key, cfg)
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                s = q.get_nowait()
            except Empty:
                return
            code = s["code"]
            fetched = {}
            prompt_version = ""
            stage = "fetch"
            try:
                base_name = fetcher.short_name(code)
            except Exception:
                base_name = s.get("name") or code
            try:
                fetched = fetcher.fetch_report(code, report_period)
                if fetched.get("quality") == "poor":
                    from tools.fetcher import LowQualityTextError

                    raise LowQualityTextError(
                        f"财报文本质量不足: {'; '.join(fetched.get('warnings') or ['未定位到业务章节'])}"
                    )
                business_input = fetched.get("business_text") or fetched.get("text") or ""
                if not business_input.strip():
                    raise ValueError("财报业务文本为空")
                stage = "extract"
                try:
                    extracted = llm.extract_business(business_input)
                except Exception as exc:
                    item = {
                        "code": code, "name": fetched.get("short_name") or base_name or code,
                        "status": "error", "stage": "extract", "error": str(exc),
                    }
                    _safe_log({
                        "code": code, "name": item["name"], "status": "error",
                        "stage": "extract", "error": str(exc), "model": getattr(llm, "model", ""),
                        **_source_fields(fetched),
                    })
                    with out_lock:
                        results.append(item)
                        if on_progress:
                            on_progress(item)
                    continue
                prompt_version = extracted.get("prompt_version", "")
                stage = "save"
                name = fetched.get("short_name") or base_name or fetched.get("name") or code
                actual_period = fetched.get("period") or report_period
                db.save_stock(
                    code=code,
                    name=name,
                    business=extracted["business"],
                    report_period=actual_period,
                    keywords=extracted.get("keywords", []),
                )
                item = {"code": code, "name": name, "status": "saved"}
                _safe_log({
                    "code": code, "name": name, "status": "saved",
                    "report_period": fetched.get("period", ""),
                    "model": getattr(llm, "model", ""),
                    "prompt_version": prompt_version,
                    **_source_fields(fetched),
                })
            except Exception as exc:
                item = {
                    "code": code, "name": base_name or s.get("name") or code, "status": "error",
                    "stage": stage, "error": str(exc),
                }
                _safe_log({
                    "code": code, "name": item["name"], "status": "error",
                    "stage": item["stage"], "error": str(exc),
                    "model": getattr(llm, "model", ""),
                    **_source_fields(fetched),
                })
            with out_lock:
                results.append(item)
                if on_progress:
                    on_progress(item)

    threads = []
    for key in keys:
        for _ in range(workers_per_key):
            t = threading.Thread(target=worker, args=(key,), daemon=True)
            threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results


def _cli():
    parser = argparse.ArgumentParser(prog="batch.py", description="批量分析程序（多 API 并发）")
    parser.add_argument("--spec", default=None, help='如 "全A股"、"科创50"、"600519"')
    parser.add_argument("--codes-file", default=None, help="按代码清单跑（每行一个代码，支持 # 注释）")
    parser.add_argument("--offset", type=int, default=0, help="切片起始下标（按代码排序）")
    parser.add_argument("--limit", type=int, default=None, help="切片数量，默认不限")
    parser.add_argument("--report", default="-", help="指定报告期，默认自动取最新年报")
    parser.add_argument("--workers-per-key", type=int, default=2, help="每个 key 的并发数")
    parser.add_argument("--force", action="store_true", help="忽略查重，强制重新分析")
    args = parser.parse_args()

    if not args.spec and not args.codes_file:
        parser.error("必须提供 --spec 或 --codes-file")

    codes = None
    if args.codes_file:
        codes = []
        for line in open(args.codes_file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            codes.append(line.split("\t")[0].strip())

    keys = load_keys()
    cfg = load_config()
    configure_max_concurrent(cfg.get("llm", {}).get("max_concurrent", 16))
    print(f"范围: {args.spec or args.codes_file} | 分片 offset={args.offset} limit={args.limit}")
    print(f"API key 数: {len(keys)} | 总并发: {len(keys) * args.workers_per_key} | "
          f"LLM 并发上限: {get_max_concurrent()}")

    def on_progress(item):
        line = f"{item['code']}\t{item.get('name', '')}\t{item['status']}"
        if item["status"] == "error":
            line += f"\t{item.get('stage', '')}: {item.get('error', '')}"
        print(line)

    results = run(
        args.spec,
        offset=args.offset,
        limit=args.limit,
        report_period=args.report,
        workers_per_key=args.workers_per_key,
        force=args.force,
        on_progress=on_progress,
        codes=codes,
    )
    c = Counter(r["status"] for r in results)
    print(f"\n完成: {dict(c)}")


if __name__ == "__main__":
    _cli()
