"""Central Configuration for Boom Agent Harness."""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def boom_home() -> Path:
    """Return the Boom home directory: $BOOM_HOME or ~/.boom."""
    return Path(os.environ.get("BOOM_HOME", str(Path.home() / ".boom")))


def default_config_path() -> Path:
    """Return the default config file path: $BOOM_HOME/config.json."""
    return boom_home() / "config.json"


class ChannelConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    webhook_url: Optional[str] = None
    bridge_port: int = 8787

class GatewayConfig(BaseModel):
    host: str = "127.0.1"
    port: int = 51236
    telegram: ChannelConfig = Field(default_factory=ChannelConfig)
    zalo: ChannelConfig = Field(default_factory=ChannelConfig)
    discord: ChannelConfig = Field(default_factory=ChannelConfig)

class ProviderConfig(BaseModel):
    name: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    timeout: float = 60.0

class BoomConfig(BaseModel):
    profile: str = "default"
    yolo: bool = False
    workdir: str = "."
    primary_provider: ProviderConfig = Field(default_factory=ProviderConfig)
    fallback_providers: List[ProviderConfig] = Field(default_factory=list)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    memory_char_limit: int = 2200
    user_char_limit: int = 1375

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "BoomConfig":
        """Load configuration with the following precedence (highest first):

        1. Environment variables (OPENAI_API_KEY, etc.)
        2. Config file ($BOOM_HOME/config.json or ~/.boom/config.json)
        3. Built-in defaults
        """
        conf = cls()

        # 1. Load from config file if it exists
        cfg_file = Path(config_path) if config_path else default_config_path()
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                conf = cls._from_dict(data)
            except Exception:
                # Corrupt config file — fall back to defaults + env vars
                # rather than crashing at startup.
                pass

        # 2. Environment variables override config file (secrets stay in env)
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        if openrouter_key:
            conf.primary_provider = ProviderConfig(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key=openrouter_key,
                model=os.getenv("BOOM_MODEL", conf.primary_provider.model or "anthropic/claude-3.7-sonnet")
            )
        elif openai_key:
            conf.primary_provider = ProviderConfig(
                name="openai",
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key=openai_key,
                model=os.getenv("BOOM_MODEL", conf.primary_provider.model or "gpt-4o")
            )
        elif anthropic_key:
            conf.primary_provider = ProviderConfig(
                name="anthropic",
                base_url="https://api.anthropic.com/v1",
                api_key=anthropic_key,
                model=os.getenv("BOOM_MODEL", conf.primary_provider.model or "claude-3-7-sonnet-20250219")
            )

        # Telegram
        tg_token = os.getenv("BOOM_TELEGRAM_BOT_TOKEN", "")
        if tg_token:
            conf.gateway.telegram.enabled = True
            conf.gateway.telegram.token = tg_token

        # YOLO mode
        if os.getenv("BOOM_YOLO", "").lower() in ("1", "true", "yes"):
            conf.yolo = True

        return conf

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "BoomConfig":
        """Build a BoomConfig from a plain dict (e.g. parsed config.json)."""
        conf = cls()

        pp = data.get("primary_provider", {})
        if pp:
            conf.primary_provider = ProviderConfig(
                name=pp.get("name", "openai"),
                base_url=pp.get("base_url", "https://api.openai.com/v1"),
                api_key=pp.get("api_key", ""),
                model=pp.get("model", "gpt-4o"),
                timeout=float(pp.get("timeout", 60.0)),
            )

        for fp in data.get("fallback_providers", []):
            conf.fallback_providers.append(ProviderConfig(
                name=fp.get("name", ""),
                base_url=fp.get("base_url", ""),
                api_key=fp.get("api_key", ""),
                model=fp.get("model", ""),
                timeout=float(fp.get("timeout", 60.0)),
            ))

        gw = data.get("gateway", {})
        if gw:
            conf.gateway.host = gw.get("host", "127.0.0.1")
            conf.gateway.port = int(gw.get("port", 51236))
            for ch_name in ("telegram", "zalo", "discord"):
                ch = gw.get(ch_name, {})
                if ch:
                    cfg = getattr(conf.gateway, ch_name)
                    cfg.enabled = bool(ch.get("enabled", False))
                    cfg.token = ch.get("token", "")
                    cfg.webhook_url = ch.get("webhook_url")
                    cfg.bridge_port = int(ch.get("bridge_port", 8787))

        conf.profile = data.get("profile", "default")
        conf.yolo = bool(data.get("yolo", False))
        conf.workdir = data.get("workdir", ".")
        conf.memory_char_limit = int(data.get("memory_char_limit", 2200))
        conf.user_char_limit = int(data.get("user_char_limit", 1375))

        return conf
