"""配置加载：config.yaml + .env 密钥 + settings 表热覆盖。"""
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or (ROOT / "config.yaml")
        with open(self.path, "r", encoding="utf-8") as f:
            self._raw: dict = yaml.safe_load(f)
        self._overrides: dict[str, Any] = {}  # settings 表的热覆盖, "a.b.c" -> value

        # 密钥从环境变量读
        self.tg_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
        self.binance_key: str = os.getenv("BINANCE_API_KEY", "")
        self.binance_secret: str = os.getenv("BINANCE_API_SECRET", "")

    def get(self, dotted: str, default: Any = None) -> Any:
        """按 'signal.vol_multiplier' 取值；settings 表覆盖优先。"""
        if dotted in self._overrides:
            return self._overrides[dotted]
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_override(self, dotted: str, value: Any) -> None:
        self._overrides[dotted] = value

    def load_overrides(self, items: dict[str, Any]) -> None:
        self._overrides.update(items)

    # 常用快捷属性
    @property
    def mode(self) -> str:
        return self.get("mode", "paper")

    @property
    def timeframes(self) -> list[str]:
        return self.get("timeframes", ["5m", "15m"])

    @property
    def db_path(self) -> Path:
        p = Path(self.get("data.db_path", "data/trade.db"))
        return p if p.is_absolute() else ROOT / p


_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
