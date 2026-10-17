"""配置加载：文件 + 环境变量覆盖。"""

import json
import os
import tempfile
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "api_keys": [],
        "model": "deepseek-chat",
        "timeout": 120,
        "temperature": 0.1,
        "retries": 3,
        "max_tokens": 4096,
        "max_candidates": 60,
        "extract_max_tokens": 16000,
        "max_concurrent": 16,
    }
}

_ENV_MAP = {
    "STOCK_API_BASE": "base_url",
    "STOCK_API_KEY": "api_key",
    "STOCK_API_MODEL": "model",
}

PLACEHOLDER_MARKERS = ("请在此填入", "批量分析key", "sk-xxx", "your-api-key")


def is_placeholder_key(key):
    s = str(key or "").strip()
    if not s:
        return True
    return any(marker in s for marker in PLACEHOLDER_MARKERS)


def load_config(path=None):
    """加载配置：文件 -> 默认值兜底 -> 环境变量覆盖。"""
    path = Path(path) if path else CONFIG_PATH
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"配置文件不是合法 JSON: {path} ({exc})") from exc
        if not isinstance(data, dict):
            raise ValueError(f"配置文件根节点必须是对象: {path}")
        for k, v in data.items():
            if k in cfg and isinstance(cfg[k], dict) and isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    for env, field in _ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            cfg["llm"][field] = val
    env_keys = os.environ.get("STOCK_API_KEYS")
    if env_keys:
        cfg["llm"]["api_keys"] = [k.strip() for k in env_keys.split(",") if k.strip()]
    return cfg


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_config(cfg, path=None):
    path = Path(path) if path else CONFIG_PATH
    _atomic_write(path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def load_keys(cfg=None):
    """读取多个 API key：优先 llm.api_keys，为空则回退 [llm.api_key]。"""
    cfg = cfg or load_config()
    llm = cfg.get("llm", {})
    keys = llm.get("api_keys") or []
    if isinstance(keys, str):
        keys = [k for k in keys.replace("\n", ",").split(",")]
    out = []
    seen = set()
    for key in keys:
        s = str(key).strip()
        if not s or is_placeholder_key(s) or s in seen:
            continue
        seen.add(s)
        out.append(s)
    if not out and llm.get("api_key") and not is_placeholder_key(llm["api_key"]):
        out = [str(llm["api_key"]).strip()]
    return out
