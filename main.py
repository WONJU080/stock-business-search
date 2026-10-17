import argparse
import sys

from tools import db
from tools.analyze_flow import analyze
from tools.config import CONFIG_PATH, load_config, load_keys, save_config
from tools.llm import build_llm
from tools.search_flow import search


def _read_codes(path):
    codes = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        codes.append(line.split("\t")[0].strip())
    return codes


def _mask(key):
    if not key:
        return "(未设置)"
    if len(key) <= 10:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


def _cmd_analyze(args):
    codes = _read_codes(args.codes_file) if args.codes_file else None
    if not args.spec and not codes:
        print("错误: 需要提供 spec 或 --codes-file", file=sys.stderr)
        return 2
    results = analyze(
        args.spec,
        report_period=args.report,
        llm=build_llm(),
        force=args.force,
        workers=args.workers,
        codes=codes,
    )
    for r in results:
        line = f"{r['code']}\t{r.get('name', '')}\t{r['status']}"
        if r["status"] == "error":
            line += f"\t{r.get('stage', '')}: {r['error']}"
        print(line)
    return 1 if any(r["status"] == "error" for r in results) else 0


def _cmd_search(args):
    res = search(args.query, llm=build_llm(), limit=args.limit)
    print(f"原词  : {res['query']}")
    print(f"关联词: {' / '.join(res['related'])}")
    print(f"命中 {res['total_hits']} 只股票，展示 {len(res['results'])} 只:")
    for w in res.get("warnings", []):
        print(f"警告: {w}")
    for r in res["results"]:
        print(f"  {r['code']}  {r['name']}  关联度:{r.get('score', '-')}  "
              f"排序:{r.get('ranked_by', '-')}  命中:{','.join(r['matched'])}")
        print(f"    {r.get('snippet', r['business'][:100])}")
    return 0


def _cmd_list(args):
    for r in db.list_analyzed():
        print(f"{r['code']}\t{r['name']}\t报告期:{r['report_period']}\t更新:{r['updated_at']}")
    return 0


def _cmd_check(args):
    index = db.load_index()
    for c in args.codes:
        code = db.normalize_code(c)
        status = "已分析" if code in index else "未分析"
        name = index[code]["name"] if code in index else "-"
        print(f"{code}\t{name}\t{status}")
    return 0


def _cmd_config(args):
    if args.action == "show":
        cfg = load_config()
        llm = cfg["llm"]
        keys = load_keys(cfg)
        print(f"base_url     : {llm['base_url']}")
        print(f"api_key      : {_mask(llm.get('api_key', ''))}")
        print(f"api_keys     : {len(keys)} 个" + (f" ({', '.join(_mask(k) for k in keys)})" if keys else ""))
        print(f"model        : {llm['model']}")
        print(f"timeout      : {llm['timeout']}")
        print(f"retries      : {llm.get('retries', 3)}")
        print(f"max_tokens   : {llm.get('max_tokens', 4096)}")
        print(f"max_candidates: {llm.get('max_candidates', 60)}")
        print(f"extract_max_tokens: {llm.get('extract_max_tokens', 16000)}")
        print(f"max_concurrent: {llm.get('max_concurrent', 16)}")
    elif args.action == "init":
        if CONFIG_PATH.exists():
            print(f"已存在: {CONFIG_PATH}")
        else:
            save_config(load_config())
            print(f"已生成模板: {CONFIG_PATH}")
    elif args.action == "set":
        cfg = load_config()
        for field in ("base_url", "api_key", "model", "timeout", "retries",
                      "max_tokens", "max_candidates", "extract_max_tokens",
                      "max_concurrent"):
            val = getattr(args, field, None)
            if val is not None:
                cfg["llm"][field] = val
        if args.api_keys is not None:
            cfg["llm"]["api_keys"] = [k.strip() for k in args.api_keys.split(",") if k.strip()]
        save_config(cfg)
        print(f"已更新: {CONFIG_PATH}")
    return 0


def _cmd_audit(args):
    from tools import audit
    from tools.repair import build_reanalysis_queue

    report = audit.audit(check_names=not args.no_names, refresh_names=args.refresh_names)
    audit.print_summary(report)
    if args.json:
        path = audit.write_report(report, args.json)
        print(f"\n审计报告: {path}")
    if args.queue:
        path, rows = build_reanalysis_queue(report, args.queue)
        print(f"重跑清单: {path}（{len(rows)} 条）")
    if args.fail_on_issues and any(report["summary"].values()):
        return 1
    return 0


def _cmd_repair_names(args):
    from tools.repair import repair_names

    res = repair_names(
        dry_run=not args.apply,
        mode=args.mode,
        refresh=args.refresh_names,
        limit=args.limit,
    )
    print(f"模式: {res['mode']} | 待修复: {res['targets']} | "
          f"{'预览（未写入）' if res['dry_run'] else '已写入'}")
    for row in res["preview"]:
        print(f"  {row['code']}  {row['current']!r} -> {row['expected']!r}")
    if not res["dry_run"]:
        print(f"已改名: {res.get('renamed', 0)} | 跳过: {res.get('skipped', 0)} | "
              f"错误: {len(res.get('errors', []))}")
        for err in res.get("errors", [])[:20]:
            print(f"  错误: {err}")
    return 0


def _cmd_rebuild_index(args):
    res = db.rebuild_index()
    print(f"已重建: {res['count']} 条（扫描 {res['files']} 个文件）")
    for e in res["errors"][:20]:
        print(f"  警告: {e}")
    return 1 if res["errors"] else 0


def main():
    parser = argparse.ArgumentParser(prog="main.py", description="股票业务分析检索程序")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_analyze = sub.add_parser("analyze", help="分析入库：范围 -> 查重 -> 财报 -> 提取 -> 入库")
    p_analyze.add_argument("spec", nargs="?", help='如 "科大讯飞"、"600519"、"全A股"、"科创50"')
    p_analyze.add_argument("--codes-file", default=None, help="按代码清单重跑（每行一个代码）")
    p_analyze.add_argument("--report", default="-", help="指定年报报告期（YYYY-12-31），默认最新")
    p_analyze.add_argument("--force", action="store_true", help="忽略查重，强制重新分析")
    p_analyze.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    p_analyze.set_defaults(func=_cmd_analyze)

    p_search = sub.add_parser("search", help="检索：关键词 -> 关联词 -> 筛选 -> 排序")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=None, help="最多展示条数")
    p_search.set_defaults(func=_cmd_search)

    p_list = sub.add_parser("list", help="列出全部已分析股票")
    p_list.set_defaults(func=_cmd_list)

    p_check = sub.add_parser("check", help="检查股票是否已分析")
    p_check.add_argument("codes", nargs="+")
    p_check.set_defaults(func=_cmd_check)

    p_config = sub.add_parser("config", help="查看/设置 API 配置")
    p_config.add_argument("action", choices=["show", "init", "set"])
    p_config.add_argument("--base-url")
    p_config.add_argument("--api-key")
    p_config.add_argument("--api-keys", help="逗号分隔的多个 key")
    p_config.add_argument("--model")
    p_config.add_argument("--timeout", type=int)
    p_config.add_argument("--retries", type=int)
    p_config.add_argument("--max-tokens", type=int)
    p_config.add_argument("--max-candidates", type=int)
    p_config.add_argument("--extract-max-tokens", type=int)
    p_config.add_argument("--max-concurrent", type=int)
    p_config.set_defaults(func=_cmd_config)

    p_audit = sub.add_parser("audit", help="全量审计：索引/名称/报告期/拒绝话术/关键词")
    p_audit.add_argument("--json", default="db/audit-report.json", help="报告输出路径")
    p_audit.add_argument("--no-names", action="store_true", help="不联网核对官方简称")
    p_audit.add_argument("--refresh-names", action="store_true", help="强制刷新巨潮股票列表")
    p_audit.add_argument("--queue", default=None, help="生成重跑清单路径，如 db/reanalysis.txt")
    p_audit.add_argument("--fail-on-issues", action="store_true", help="发现问题时返回 1")
    p_audit.set_defaults(func=_cmd_audit)

    p_repair = sub.add_parser("repair-names", help="修复历史错误名称")
    p_repair.add_argument("--apply", action="store_true", help="实际写入（默认仅预览）")
    p_repair.add_argument("--mode", choices=["malformed", "all"], default="malformed",
                          help="malformed=只修明显错误名称（默认）；all=全量同步为巨潮简称")
    p_repair.add_argument("--refresh-names", action="store_true")
    p_repair.add_argument("--limit", type=int, default=None)
    p_repair.set_defaults(func=_cmd_repair_names)

    p_rebuild = sub.add_parser("rebuild-index", help="从 db/stocks 重建总揽")
    p_rebuild.set_defaults(func=_cmd_rebuild_index)

    args = parser.parse_args()
    try:
        code = args.func(args)
    except BrokenPipeError:
        # 允许 `main.py list | head` 这类管道提前关闭
        try:
            sys.stdout.close()
        except OSError:
            pass
        return
    raise SystemExit(code or 0)


if __name__ == "__main__":
    main()
