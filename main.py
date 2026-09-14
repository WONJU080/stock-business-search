import argparse

from tools import db
from tools.analyze_flow import analyze
from tools.config import CONFIG_PATH, load_config, save_config
from tools.llm import build_llm
from tools.search_flow import search


def _cmd_analyze(args):
    results = analyze(args.spec, report_period=args.report, llm=build_llm(),
                      force=args.force, workers=args.workers)
    for r in results:
        line = f"{r['code']}\t{r.get('name', '')}\t{r['status']}"
        if r["status"] == "error":
            line += f"\t{r['error']}"
        print(line)


def _cmd_search(args):
    res = search(args.query, llm=build_llm())
    print(f"原词  : {res['query']}")
    print(f"关联词: {' / '.join(res['related'])}")
    print(f"命中 {len(res['results'])} 只股票:")
    for r in res["results"]:
        print(f"  {r['code']}  {r['name']}  关联度:{r.get('score', '-')}  命中:{','.join(r['matched'])}")
        print(f"    {r['business'][:100]}")


def _cmd_list(args):
    for r in db.list_analyzed():
        print(f"{r['code']}\t{r['name']}\t报告期:{r['report_period']}\t更新:{r['updated_at']}")


def _cmd_check(args):
    index = db.load_index()
    for c in args.codes:
        code = db.normalize_code(c)
        status = "已分析" if code in index else "未分析"
        name = index[code]["name"] if code in index else "-"
        print(f"{code}\t{name}\t{status}")


def _mask(key):
    if not key:
        return "(未设置)"
    if len(key) <= 10:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


def _cmd_config(args):
    if args.action == "show":
        cfg = load_config()
        llm = cfg["llm"]
        print(f"base_url : {llm['base_url']}")
        print(f"api_key  : {_mask(llm['api_key'])}")
        print(f"model    : {llm['model']}")
        print(f"timeout  : {llm['timeout']}")
    elif args.action == "init":
        if CONFIG_PATH.exists():
            print(f"已存在: {CONFIG_PATH}")
        else:
            save_config(load_config())
            print(f"已生成模板: {CONFIG_PATH}")
    elif args.action == "set":
        cfg = load_config()
        for field in ("base_url", "api_key", "model", "timeout"):
            val = getattr(args, field, None)
            if val is not None:
                cfg["llm"][field] = val
        save_config(cfg)
        print(f"已更新: {CONFIG_PATH}")


def main():
    parser = argparse.ArgumentParser(prog="main.py", description="股票业务分析检索程序")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_analyze = sub.add_parser("analyze", help="分析入库：范围 -> 查重 -> 财报 -> 提取 -> 入库")
    p_analyze.add_argument("spec", help='如 "科大讯飞"、"600519"、"全A股"、"科创50"、"指数:沪深300"')
    p_analyze.add_argument("--report", default="-", help="指定报告期，默认自动取最新年报")
    p_analyze.add_argument("--force", action="store_true", help="忽略查重，强制重新分析")
    p_analyze.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    p_analyze.set_defaults(func=_cmd_analyze)

    p_search = sub.add_parser("search", help="检索：关键词 -> 关联词 -> 筛选 -> 排序")
    p_search.add_argument("query")
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
    p_config.add_argument("--model")
    p_config.add_argument("--timeout", type=int)
    p_config.set_defaults(func=_cmd_config)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
