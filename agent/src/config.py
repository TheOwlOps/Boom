"""Central Configuration for Boom Agent Harness."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class ChannelConfig(BaseModel):
    enabled: bool = False
    token: Optional[str] = None
    webhook_url: Optional[str] = None
    bridge_port: int = 8787

class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 51236
    telegram: ChannelConfig = Field(default_factory=ChannelConfig)
    zalo: ChannelConfig = Field(default_factory=ChannelConfig)
    discord: ChannelConfig = Field(default_factory=ChannelConfig)

class ProviderConfig(BaseModel):
    name: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: Optional[str] = None
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
        """Load configuration from config.yaml, environment variables, or defaults."""
        conf = cls()

        # Check environment variables for API keys and models
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        if openrouter_key:
            conf.primary_provider = ProviderConfig(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key=openrouter_key,
                model=os.getenv("BOOM_MODEL", "anthropic/claude-3.7-sonnet")
            )
        elif openai_key:
            conf.primary_provider = ProviderConfig(
                name="openai",
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key=openai_key,
                model=os.getenv("BOOM_MODEL", "gpt-4o")
            )
        elif anthropic_key:
            conf.primary_provider = ProviderConfig(
                name="anthropic",
                base_url="https://api.anthropic.com/v1",
                api_key=anthropic_key,
                model=os.getenv("BOOM_MODEL", "claude-3-7-sonnet-20250219")
            )

        # Telegram
        tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if tg_token:
            conf.gateway.telegram.enabled = True
            conf.gateway.telegram.token = tg_token

        # YOLO mode
        if os.getenv("BOOM_YOLO", "").lower() in ("1", "true", "yes"):
            conf.yolo = True

        return conf
