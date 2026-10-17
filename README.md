**自娱自乐vibe coding第三弹。较第二弹的那个，大大加强了实用性。将关键词检索改善。将数据库改善。后续，为了发挥与ai聊天不同的高效率特点，需要改善ai分析财报的逻辑。以及制作可以脱离出程序之外并行分析不同财报的数据获取工具。否则以目前这个速度得猴年马月才能实现预想中“快人一步找到目标股票”的效果。那还不如看券商的推送。**



# 股票业务数据库

用于保存 AI 分析 A 股年报后提取的「在营业务」信息，支持保存、更新、关键词检索、查重、审计与批量重跑。
格式规范见 [SPEC.md](SPEC.md)。

## 目录

```
app.py                 # 桌面窗口界面（Tkinter，含 API 设置/停止/检索/批量）
main.py                # 统一入口（analyze/search/list/check/config/audit/repair-names/rebuild-index）
batch.py               # 批量分析（多 API 并发、分片、按代码清单重跑）
config.example.json    # API 配置模板（复制为 config.json 后填写）
requirements.txt
db/
├── index.txt          # 总揽：已分析股票清单（可由明细重建）
├── stocks/            # 每股一个纯文本文件
├── runs.jsonl         # 追加式运行日志（来源/错误/模型/prompt 版本）
└── stock_universe.json# 巨潮股票简称/orgId 快照（自动缓存）
tools/
├── db.py              # 数据层：锁/原子写/校验/检索/查重/重建索引
├── config.py          # 配置加载（文件 + 环境变量覆盖，多 key）
├── llm.py             # LLM：抽象接口 + OpenAICompatClient + StubLLM
├── universe.py        # 股票池解析（全A股/指数/单一）
├── stock_list.py      # 巨潮股票列表/简称/orgId 快照
├── fetcher.py         # 财报获取（按年份选年报 + PDF 文本 + 章节定位 + 质量门槛）
├── search_flow.py     # 检索链路（本地召回 + AI 重排 + 本地兜底）
├── analyze_flow.py    # 分析入库链路（增量保存/错误日志）
├── audit.py           # 全量审计
└── repair.py          # 历史名称修复/重跑清单
tests/                 # pytest 回归测试
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

依赖：`requests`、`curl_cffi`、`PyMuPDF`、`akshare`；测试用 `pytest`。Tkinter 使用系统 Python 自带组件。

## 快速上手

```bash
# 配置（也可用环境变量 STOCK_API_BASE / STOCK_API_KEY / STOCK_API_KEYS / STOCK_API_MODEL）
python3 main.py config set --base-url https://api.deepseek.com/v1 \
  --api-key sk-xxx --api-keys sk-xxx,sk-yyy --model deepseek-chat

# 单只 / 范围
python3 main.py analyze "600519 贵州茅台"
python3 main.py analyze 科创50
python3 main.py analyze 全A股 --workers 4

# 检索（支持代码/名称/业务关键词；大结果集只把前 60 条交给 AI 重排）
python3 main.py search 点读笔
python3 main.py search 600519
python3 main.py search AI --limit 20

# 清单 / 查重
python3 main.py list
python3 main.py check 600519

# 全量审计（索引/名称/报告期/拒绝话术/关键词），并生成重跑清单
python3 main.py audit --json db/audit-report.json --queue db/reanalysis.txt

# 修复明显错误的名称（默认只预览；--apply 才写入）
python3 main.py repair-names
python3 main.py repair-names --apply

# 索引损坏时从明细重建
python3 main.py rebuild-index

# 批量分析/按清单重跑（多 key 并发）
python3 batch.py --spec 全A股 --offset 0 --limit 500 --workers-per-key 2
python3 batch.py --codes-file db/reanalysis.txt --force --workers-per-key 2
```

API 密钥也可用环境变量注入：`STOCK_API_BASE` / `STOCK_API_KEY` / `STOCK_API_KEYS` / `STOCK_API_MODEL`。
多 key 在 `config.json` 的 `llm.api_keys` 里配置为数组；单 key 与 key 池会自动互相兜底。

## 数据安全与并发

- 所有写操作通过 `tools/db.py` 的跨进程文件锁（`db/.write.lock`）串行化。
- 明细/索引使用“临时文件 + fsync + `os.replace`”原子替换，进程被杀不会留下半截文件。
- `db/index.txt` 可由明细文件随时重建；建议定期备份 `db/`（`.backups/` 已加入 .gitignore）。
- 批量分片可以并行跑：索引写入有锁，不会互相覆盖。先小规模验证，再放大并发。

## 性能与并发（2026-10 实测）

- LLM 请求的进程内并发上限由 `llm.max_concurrent` 控制（默认 16），`batch.py` 启动时按配置设置；多个 batch 进程各自持有一个上限。
- 4 个 key × `--workers-per-key 4` = 16 个股票线程，配合 `max_concurrent=16` 才能真正跑到约 16 个并发请求。
- 同题实测（`000151`，31,943 字）：`deepseek-flash` 15.8s / 1479 字，`deepseek-chat` 5.3s / 2230 字；抽取阶段换 chat 约 3 倍速。
- 12 只股票、4 key × 4 worker、16 并发：约 71 秒完成 11 只（1 只因个别年报的目录锚定问题失败，修复后重跑成功）。

## 数据质量规则

- `名称` 必须是股票简称，不得是公司全称/英文标题/带代码前缀的标题。
- `报告期` 只能来自所选年报年份（`YYYY-12-31`），不能把“最新公告年份”当报告期。
- 年报选择：按年份分组，优先中文标准全文；排除摘要、英文版、图文版、已取消；同一年优先“更正/修订”版本；确保年份等于请求年份。
- PDF 文本过短、页均字符过少、业务章节仅含目录时，直接记为抓取失败，不调用模型、不污染业务字段。
- 推理模型（如 `deepseek-flash`）会先消耗 reasoning token；业务抽取默认 `extract_max_tokens=16000`，若仍被思考过程耗尽，会自动把文本块对半拆小后重试。
- 所有 LLM 请求共用一个进程内并发上限 `llm.max_concurrent`（默认 16）；`batch.py` 启动时按配置设置。多个 batch 进程会各自持有一个上限。
- 模型返回“无法提炼/未提供/仅含目录”等拒绝话术时，记为抽取失败，只写 `runs.jsonl`。
- 业务文本目标为 500~1500 字的详细描述；关键词 5~20 个，过滤“年报/主营业务/报告期内”等泛词。

## 当前升级状态（2026-10）

- 5471 条历史记录：索引/明细元数据一致；942 条明显错误名称已修复，当前“名称可疑”为 0。
- 已用新链路重跑问题记录，238 条更新；其中 220 条业务文本 ≥500 字（中位数约 2479 字，最长 9854 字）。
- 仍有 10 条年报 PDF 为扫描件/无文本层，无法提取正文（如 `000420`、`002083`、`300773`、`688981`）；这些记录保留旧内容，`audit` 会标记为“疑似拒绝话术”，需 OCR 或人工补录。
- 3 条报告期仍为 2024-12-31（`002731`、`600958`、`601107`）：巨潮当前只有 2024 年全文、2025 年仅有摘要，属“最新可用全文”，不是选源错误。
- 46 条名称与巨潮当前简称不同，多为 ST/更名/退市整理，默认不自动覆盖；可用 `repair-names --mode all` 人工确认后同步。
- 其余约 5233 条仍是旧 Prompt（≤300 字）生成的摘要。要达到 SPEC v2 的详细度，需要对全量代码再跑一次：
  ```bash
  python3 batch.py --spec 全A股 --force --workers-per-key 2
  ```
  这会消耗较多 API 额度与时间（推理模型每个分块还需要额外 reasoning token），建议先小批量试跑并评估。

## 供 AI / 程序调用

```python
from tools import db

pool = ["600519", "000001", "300750"]
todo = db.filter_unanalyzed(pool)

db.save_stock(
    code="300750",
    name="宁德时代",
    business="（详细在营业务，覆盖业务板块/产品/客户/收入构成）",
    report_period="2025-12-31",
    keywords=["动力电池", "储能", "锂电池"],
)

hits = db.search(["点读笔", "学习机", "早教机"])
```

```python
from tools.search_flow import search
res = search("点读笔")          # 关联词 -> 本地召回 -> AI 重排（失败自动回退本地）
for r in res["results"]:
    print(r["code"], r["name"], r.get("score"), r.get("ranked_by"))
```

```python
from tools.analyze_flow import analyze
res = analyze("科创50", workers=4)
for r in res:
    print(r["code"], r["status"], r.get("stage", ""), r.get("error", ""))
```

### 组件

| 组件 | 抽象类 | 职责 | 实现 |
|------|--------|------|------|
| 股票池 | `UniverseProvider` | 范围 -> 股票列表 | `AkshareUniverse`（全A股 + 指数成分）/ `StubUniverse` |
| 财报源 | `Fetcher` | 代码 -> 财报原文 | `CninfoFetcher`（巨潮年报，按年份选源+质量门槛）/ `FileFetcher` |
| 模型 | `LLMClient` | 财报 -> 业务提取/检索重排 | `OpenAICompatClient` / `StubLLM` |

`CninfoFetcher` 返回 `title/source_url/announcement_time/pdf_pages/quality/warnings`，分析链路会写入 `db/runs.jsonl`，便于追溯。

`resolve_stocks` 支持 `"600519"`、`"600519 贵州茅台"`、`"全A股"`、`"科创50"`、`"指数:沪深300"`；
板块接口仍是占位（依赖后续接入）。

## 测试

```bash
python3 -m pytest
```

覆盖：并发写入不丢记录、首次分析日期保留、名称 `|` 清洗、索引重建/格式错误、年报年份选择、图谱版/摘要过滤、目录跳过、拒绝话术、检索候选上限、LLM 失败回退。

## 约定

- 一切读写都通过 `tools/db.py`，保证总揽与明细一致；索引可由明细重建。
- 代码统一为 6 位补零字符串，如 `000001`。
- `config.json` 含密钥，权限建议 600；不要提交到 git。
- 不要手工编辑 `db/index.txt`；需要修复时用 `rebuild-index`/`repair-names`。
