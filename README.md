# 股票业务数据库

用于保存 AI 分析财报后提取的「在营业务」信息，支持保存、更新、关键词检索与查重，
并提供「关键词 → 关联词 → 筛选 → 排序」的检索链路。
格式规范见 [SPEC.md](SPEC.md)。

## 目录

```
app.py                 # 桌面窗口界面（Tkinter，含 API 设置）
main.py                # 统一入口（analyze/search/list/check/config）
config.example.json    # API 配置模板（复制为 config.json 后填写）
db/
├── index.txt          # 总揽：已分析股票清单（查重用）
└── stocks/            # 每股一个纯文本文件
tools/
├── db.py              # 数据层：读写/更新/检索/查重
├── config.py          # 配置加载（文件 + 环境变量覆盖）
├── llm.py             # LLM：抽象接口 + OpenAICompatClient + StubLLM
├── universe.py        # 股票池解析（全A股/指数/单一，可注入板块源）
├── fetcher.py         # 财报获取（CninfoFetcher 巨潮年报全文 + FileFetcher）
├── search_flow.py     # 检索链路编排
└── analyze_flow.py    # 分析入库链路编排
```

## 快速上手

```bash
# 桌面窗口（含 API 设置、分析、检索、已分析列表）
python3 app.py

# 命令行等价操作
python3 main.py config set --base-url https://api.deepseek.com/v1 \
  --api-key sk-xxx --model deepseek-chat
python3 main.py analyze "600519 贵州茅台"   # 单一股票
python3 main.py analyze 科创50               # 指数成分股
python3 main.py analyze 全A股                # 全部 A 股
python3 main.py search 点读笔                 # 检索
python3 main.py list / check 600519          # 清单 / 查重
```

API 密钥也可用环境变量注入：`STOCK_API_BASE` / `STOCK_API_KEY` / `STOCK_API_MODEL`。

## 供 AI / 程序调用

### 数据层

```python
from tools import db

# 1) 收到分析命令后，先查重：返回尚未分析的股票池
pool = ["600519", "000001", "300750"]
todo = db.filter_unanalyzed(pool)   # ['300750']

# 2) 分析完成后保存
db.save_stock(
    code="300750",
    name="宁德时代",
    business="（AI 提取的一长段不概括业务文字）",
    report_period="2025-12-31",
    keywords=["动力电池", "储能", "锂电池"],
)

# 3) 数据层检索（给定关联词，返回含 business 全文）
hits = db.search(["点读笔", "学习机", "早教机"])
```

### 检索链路（含 LLM 抽象）

```python
from tools.search_flow import search

res = search("点读笔")   # 内部：生成关联词 -> 筛选 -> 排序
print(res["related"])     # ['点读笔', ...]
for r in res["results"]:
    print(r["code"], r["name"], r.get("score"))
```

### 接入真实大模型

```python
from tools.llm import LLMClient

class MyLLM(LLMClient):
    def expand_keywords(self, query):
        return ["学习机", "早教机"]          # 调用模型生成关联词

    def rank_results(self, query, results):
        # 调用模型按关联度排序，附加 score/rank 后返回
        return results

    def extract_business(self, report_text):
        # 调用模型提取在营业务，返回 {"business": ..., "keywords": [...]}
        return {"business": "...", "keywords": [...]}

res = search("点读笔", llm=MyLLM())
```

当前默认使用 `StubLLM`：不调用模型、不扩展关联词、按命中数排序，仅保证链路可跑通。
接入具体模型时，实现 `LLMClient` 的三个方法并传入即可。

### 接入任意兼容 API 的模型

`OpenAICompatClient` 通过标准 `/chat/completions` 协议调用任意 OpenAI 兼容服务，
配置好 `config.json` 后自动启用：

```json
{
  "llm": {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-xxx",
    "model": "deepseek-chat"
  }
}
```

无需写代码，`main.py` 会自动装配 `OpenAICompatClient`；未配置 `api_key` 时回退 `StubLLM`。

### 分析入库链路

```python
from tools.analyze_flow import analyze

res = analyze("科创50")   # 依赖 UniverseProvider + Fetcher + LLMClient
for r in res:
    print(r["code"], r["status"])   # saved / skipped / error
```

`analyze` 的三个可注入组件，均含占位实现，接入真实源时替换：

| 组件 | 抽象类 | 职责 | 实现 |
|------|--------|------|------|
| 股票池 | `UniverseProvider` | 范围 -> 股票列表 | `AkshareUniverse`（全A股 + 指数成分）/ `StubUniverse` |
| 财报源 | `Fetcher` | 代码 -> 财报原文 | `CninfoFetcher`（巨潮年报全文）/ `FileFetcher`（本地文件） |
| 模型 | `LLMClient` | 财报 -> 业务提取 | `OpenAICompatClient` / `StubLLM` |

`AkshareUniverse`：`all_a_shares()` 全 A 股（akshare）、`index_constituents()` 中证指数成分。
`resolve_stocks` 支持 `"600519"`、`"600519 贵州茅台"`、`"全A股"`、`"科创50"`、
`"指数:沪深300"`、`"板块:xxx"`（板块当前占位）等写法。

`CninfoFetcher`：从巨潮资讯网抓取最新年度报告全文（PDF），用 PyMuPDF 提取文本，
并定位「管理层讨论与分析」章节作为业务分析输入；`latest_annual_period(code)` 返回最新年报期。
依赖 `requests` + `PyMuPDF`。`FileFetcher` 约定：财报放 `reports/{代码}.txt`。

## 约定

- 一切读写都通过 `tools/db.py`，保证总揽与明细文件一致。
- 代码统一为 6 位补零字符串，如 `000001`。
