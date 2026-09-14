import re
from abc import ABC, abstractmethod

_FULL_TO_HALF = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)


def _norm(s):
    return str(s).translate(_FULL_TO_HALF).replace(" ", "")


class UniverseProvider(ABC):
    """股票池来源抽象。"""

    @abstractmethod
    def resolve_sector(self, sector: str) -> list[dict]:
        """板块名 -> [{"code", "name"}, ...]"""

    @abstractmethod
    def all_a_shares(self) -> list[dict]:
        """全 A 股 -> [{"code", "name"}, ...]"""

    @abstractmethod
    def index_constituents(self, index_name: str) -> list[dict]:
        """指数名/代码 -> [{"code", "name"}, ...]"""

    @abstractmethod
    def resolve_by_name(self, name: str) -> list[dict]:
        """股票名称 -> [{"code", "name"}, ...]"""


class StubUniverse(UniverseProvider):
    """占位实现：未接入股票池数据源。"""

    def resolve_sector(self, sector: str) -> list[dict]:
        raise NotImplementedError("未接入板块数据源")

    def all_a_shares(self) -> list[dict]:
        raise NotImplementedError("未接入全 A 股列表数据源")

    def index_constituents(self, index_name: str) -> list[dict]:
        raise NotImplementedError("未接入指数成分数据源")

    def resolve_by_name(self, name: str) -> list[dict]:
        raise NotImplementedError("未接入股票名称解析数据源")


INDEX_CODE_MAP = {
    "科创50": "000688",
    "科创100": "000698",
    "上证50": "000016",
    "沪深300": "000300",
    "中证500": "000905",
    "中证800": "000906",
    "中证1000": "000852",
    "中证2000": "932000",
    "创业板指": "399006",
    "创业板50": "399673",
    "深证成指": "399001",
}


class AkshareUniverse(UniverseProvider):
    """基于 akshare 的股票池：全 A 股 + 中证指数成分。"""

    def __init__(self):
        self._all_cache = None

    def resolve_sector(self, sector: str) -> list[dict]:
        raise NotImplementedError("板块成分数据源在当前网络环境不可用，请使用全A股或指数")

    def all_a_shares(self) -> list[dict]:
        if self._all_cache is None:
            import akshare as ak

            df = ak.stock_info_a_code_name()
            self._all_cache = [
                {"code": str(r["code"]).strip().zfill(6), "name": str(r["name"]).strip()}
                for _, r in df.iterrows()
            ]
        return list(self._all_cache)

    def index_constituents(self, index_name: str) -> list[dict]:
        import akshare as ak

        code = INDEX_CODE_MAP.get(index_name) or index_name
        df = ak.index_stock_cons_csindex(symbol=code)
        return [
            {"code": str(r["成分券代码"]).strip().zfill(6), "name": str(r["成分券名称"]).strip()}
            for _, r in df.iterrows()
        ]

    def resolve_by_name(self, name: str) -> list[dict]:
        target = _norm(name)
        stocks = self.all_a_shares()
        exact = [s for s in stocks if _norm(s["name"]) == target]
        if exact:
            return exact
        fuzzy = [s for s in stocks if target and (target in _norm(s["name"]) or _norm(s["name"]) in target)]
        if len(fuzzy) == 1:
            return fuzzy
        if len(fuzzy) > 1:
            raise ValueError(f"名称「{name}」匹配到多只股票: {[s['name'] for s in fuzzy[:10]]}")
        raise ValueError(f"未找到股票: {name!r}")


def resolve_stocks(spec, provider=None):
    """解析范围描述为股票列表 [{"code","name"},...]。

    spec 支持：
      - 单一代码："600519"（name 留空）
      - 代码+名称："600519 贵州茅台"
      - 股票名称："科大讯飞"
      - 全 A 股："全A股" / "全部A股" / "all"
      - 指数："科创50" 或 "指数:科创50" / "指数:000688"
      - 板块："板块:xxx"（依赖 provider，当前占位）
    """
    spec = str(spec).strip()
    provider = provider or AkshareUniverse()
    if spec.startswith("板块:"):
        return provider.resolve_sector(spec[len("板块:"):].strip())
    if spec.startswith("指数:"):
        return provider.index_constituents(spec[len("指数:"):].strip())
    if spec in ("全A股", "全部A股", "全A", "全部", "all"):
        return provider.all_a_shares()
    if spec in INDEX_CODE_MAP:
        return provider.index_constituents(spec)
    m = re.match(r"(\d{6})(?:\s+(\S+))?", spec)
    if m:
        return [{"code": m.group(1), "name": m.group(2) or ""}]
    return provider.resolve_by_name(spec)
