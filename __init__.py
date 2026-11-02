"""pass (password-store) plugin for Hermes — one plugin, two capabilities.

    source.py   resolves the GPG store into environment variables at process
                startup (the SecretSource contract: "you fetch; the orchestrator
                applies"). Nothing here ever sees chat traffic.

    chat.py     the `/secret` command: stores a secret from chat straight into the
                store via `pass insert`. The `pre_gateway_dispatch` hook fires at
                ingress, before auth and before any session exists, and returns
                "skip" — the value never reaches the agent loop or state.db.
                Only senders in `chat_allowed_users` may store (empty = off).

They share the store on disk and nothing else: the source reads, the chat lane
writes, and neither goes through the other.

Config in config.yaml:
    secrets:
      sources: [pass]
      pass:
        enabled: true
        store_path: ~/.password-store
        subdirs: [shared, work]
    plugins:
      entries:
        pass-secrets:
          settings:
            chat_allowed_users: ["telegram:123456789"]   # who may use /secret
"""

from __future__ import annotations

from . import chat
from .chat import on_dispatch
from .source import PassSource, _secrets_prompt_note


def register(ctx):
    """Wire both halves.

    The framework re-pulls enabled secret sources immediately after discovery,
    so the source applies through the orchestrator with proper precedence and
    provenance. Gateway hooks go live on registration (no restart).
    """
    ctx.register_secret_source(PassSource())
    ctx.register_system_prompt_section(
        "pass-secrets-resolution",
        _secrets_prompt_note,
    )
    chat._get_setting = ctx.get_config
    chat._store_path = _pass_store_path()
    ctx.register_hook("pre_gateway_dispatch", on_dispatch)


def _pass_store_path():
    """secrets.pass.store_path, so /secret writes to the store the source reads."""
    try:
        from hermes_cli.config import load_config
        cfg = (load_config() or {}).get("secrets", {}).get("pass", {}) or {}
        return cfg.get("store_path") if isinstance(cfg, dict) else None
    except Exception:  # noqa: BLE001 — fall back to pass' own default store
        return None
