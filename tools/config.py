import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "timeout": 120,
        "temperature": 0.1,
    }
}

_ENV_MAP = {
    "STOCK_API_BASE": "base_url",
    "STOCK_API_KEY": "api_key",
    "STOCK_API_MODEL": "model",
}


def load_config(path=None):
    """加载配置：文件 -> 默认值兜底 -> 环境变量覆盖。"""
    path = Path(path) if path else CONFIG_PATH
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in data.items():
            if k in cfg and isinstance(cfg[k], dict) and isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    for env, field in _ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            cfg["llm"][field] = val
    return cfg


def save_config(cfg, path=None):
    path = Path(path) if path else CONFIG_PATH
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
