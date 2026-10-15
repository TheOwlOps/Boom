"""Interactive `boom setup` wizard — stdlib only, no extra dependencies.

Creates ~/.boom/config.json with LLM provider credentials and channel tokens.
Run with:  boom setup   (or:  python -m agent.src.cli setup)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def boom_home() -> Path:
    """Return the Boom home directory: $BOOM_HOME or ~/.boom."""
    return Path(os.environ.get("BOOM_HOME", str(Path.home() / ".boom")))


def config_path() -> Path:
    return boom_home() / "config.json"


# Provider presets: (display name, base_url, env var hint)
PROVIDERS = {
    "1": ("OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "2": ("Anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "3": ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "4": ("Custom (OpenAI-compatible)", "", ""),
}


def _prompt(text: str, default: str = "", secret: bool = False) -> str:
    """Prompt for input. Returns default if empty."""
    suffix = f" [{default}]" if default else ""
    prompt_text = f"{text}{suffix}: "
    if secret:
        import getpass
        try:
            value = getpass.getpass(prompt_text)
        except Exception:
            # getpass fails when stdin is not a tty — fall back to input
            value = input(prompt_text)
    else:
        value = input(prompt_text)
    value = value.strip()
    return value if value else default


def _yes_no(text: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{text} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def test_api_key(provider_name: str, base_url: str, api_key: str,
                 model: str) -> tuple[bool, str]:
    """Validate an LLM API key with a minimal request.

    Returns (ok, message). Uses httpx if available, else urllib (stdlib).
    Never logs or returns the key itself.
    """
    # Minimal chat completion payload
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Anthropic uses a different endpoint and auth scheme
    url = base_url.rstrip("/") + "/chat/completions"
    if provider_name.lower() == "anthropic":
        url = base_url.rstrip("/") + "/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }

    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return True, "API key is valid."
        if resp.status_code in (401, 403):
            return False, f"API key rejected (HTTP {resp.status_code}). Check the key and try again."
        return False, f"Unexpected response (HTTP {resp.status_code}): {resp.text[:200]}"
    except ImportError:
        # Fallback to stdlib urllib if httpx is not installed
        import urllib.request
        import urllib.error
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True, "API key is valid."
                return False, f"Unexpected response (HTTP {resp.status})."
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, f"API key rejected (HTTP {e.code}). Check the key and try again."
            return False, f"Unexpected response (HTTP {e.code})."
        except Exception as e:
            return False, f"Connection failed: {e}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def run_setup(argv: list[str] | None = None) -> int:
    """Run the interactive setup wizard. Returns exit code."""
    print("=" * 50)
    print("  Boom Setup")
    print("=" * 50)
    print()
    print("This wizard creates ~/.boom/config.json with your")
    print("LLM provider credentials and channel tokens.")
    print()

    home = boom_home()
    cfg_file = config_path()

    if cfg_file.exists():
        print(f"Found existing config at {cfg_file}")
        if not _yes_no("Overwrite it?", default=False):
            print("Setup cancelled. Existing config kept.")
            return 0
        print()

    # --- LLM Provider ---
    print("--- LLM Provider ---")
    for key, (name, _, _) in PROVIDERS.items():
        print(f"  {key}. {name}")
    print()
    choice = _prompt("Choose provider", default="1")
    while choice not in PROVIDERS:
        print("Invalid choice. Pick 1-4.")
        choice = _prompt("Choose provider", default="1")

    provider_name, default_base_url, _ = PROVIDERS[choice]

    if choice == "4":
        provider_name = _prompt("Provider name", default="custom")
        base_url = _prompt("Base URL (OpenAI-compatible)")
        while not base_url:
            print("Base URL is required for a custom provider.")
            base_url = _prompt("Base URL (OpenAI-compatible)")
    else:
        base_url = _prompt("Base URL", default=default_base_url)

    default_models = {
        "OpenAI": "gpt-4o",
        "Anthropic": "claude-3-7-sonnet-20250219",
        "OpenRouter": "anthropic/claude-3.7-sonnet",
    }
    model = _prompt("Model", default=default_models.get(provider_name, ""))

    api_key = _prompt("API key", secret=True)
    while not api_key:
        print("API key is required.")
        api_key = _prompt("API key", secret=True)

    # Validate the key
    print()
    print("Testing API key...", end=" ", flush=True)
    ok, msg = test_api_key(provider_name, base_url, api_key, model)
    print("OK" if ok else "FAILED")
    print(f"  {msg}")
    if not ok:
        if not _yes_no("Continue anyway?", default=False):
            print("Setup cancelled.")
            return 1
    print()

    # --- Channels (optional) ---
    print("--- Channels (optional, press Enter to skip) ---")
    print()

    telegram_token = _prompt("Telegram bot token")
    discord_token = _prompt("Discord bot token")
    # Zalo and Messenger use webhook verification rather than simple tokens
    zalo_oa_token = _prompt("Zalo OA access token")

    # --- Write config ---
    config = {
        "version": 1,
        "primary_provider": {
            "name": provider_name.lower().replace(" ", "-"),
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "timeout": 60.0,
        },
        "fallback_providers": [],
        "gateway": {
            "host": "127.0.0.1",
            "port": 51236,
            "telegram": {
                "enabled": bool(telegram_token),
                "token": telegram_token,
            },
            "discord": {
                "enabled": bool(discord_token),
                "token": discord_token,
            },
            "zalo": {
                "enabled": bool(zalo_oa_token),
                "token": zalo_oa_token,
            },
        },
    }

    home.mkdir(parents=True, exist_ok=True)
    # Restrict permissions on the config file (contains secrets)
    cfg_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(cfg_file, 0o600)
    except OSError:
        pass  # Windows doesn't support chmod the same way

    print()
    print("=" * 50)
    print(f"  Config written to {cfg_file}")
    print("=" * 50)
    print()
    print("Next steps:")
    print("  boom --gateway    Start the multi-channel gateway daemon")
    print("  boom              Start an interactive REPL session")
    print()
    print("Tip: you can also set credentials via environment variables")
    print("  (OPENAI_API_KEY, ANTHROPIC_API_KEY, BOOM_TELEGRAM_BOT_TOKEN).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(run_setup(sys.argv[1:]))
