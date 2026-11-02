#!/usr/bin/env python3
"""Self-check for pass-secrets — runs from a checkout, no Hermes install needed.

    python3 tests/selftest.py

Covers registration wiring, the source contract against the local pass store
(skipped when no store is present), and the !secret chat lane's parsing rules.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location(
    "ps_self", HERE / "__init__.py", submodule_search_locations=[str(HERE)]
)
assert spec is not None and spec.loader is not None
m = importlib.util.module_from_spec(spec)
m.__package__ = "ps_self"
m.__path__ = [str(HERE)]
sys.modules["ps_self"] = m
spec.loader.exec_module(m)
chat = m.chat


def check_registration() -> None:
    """register() wires the source, the prompt note and the dispatch hook."""
    calls = []

    class Ctx:
        def register_secret_source(self, source):
            calls.append(("source", source.name))

        def register_system_prompt_section(self, index, fn):
            calls.append(("prompt", index))

        def register_hook(self, name, cb, **_kwargs):
            calls.append(("hook", name, cb.__name__))

        def get_config(self, key, default=None):
            return default

    m.register(Ctx())
    assert calls == [
        ("source", "pass"),
        ("prompt", "pass-secrets-resolution"),
        ("hook", "pre_gateway_dispatch", "on_dispatch"),
    ], calls
    print("  PASS  registration")


def check_source() -> None:
    """The source returns {ENV_VAR: value} and never raises."""
    if not shutil.which("pass"):
        print("  SKIP  source: no pass binary on PATH")
        return
    store = pathlib.Path("~/.password-store").expanduser()
    if not store.is_dir():
        print("  SKIP  source: no pass store at ~/.password-store")
        return
    result = m.PassSource().fetch(
        {"store_path": str(store), "subdirs": ["shared"]}, pathlib.Path.home()
    )
    assert result.error is None, result.error
    assert isinstance(result.secrets, dict)
    assert all(k.isupper() for k in result.secrets), "non UPPER_SNAKE key leaked through"
    print(f"  PASS  source: {len(result.secrets)} entries under shared/")


def check_usage() -> None:
    """Usage text is generic and teaches both scopes."""
    text = chat._run("!secret")
    assert "!secret <name> <value>" in text, text
    assert "!secret shared/MY_API_KEY" in text, text
    assert "!secret agentname/MY_API_KEY" in text, text
    assert "`agentname/` = this agent only" in text, text
    print("  PASS  usage text")


def check_name_rules() -> None:
    """A name must be a path; bare words and traversal are refused."""
    assert chat._valid("shared/KEY")
    assert chat._valid("work/NESTED/KEY")
    for bad in ("get", "", "../etc/passwd", "shared/../x", "breakglass/K", "shared/", "/leading"):
        assert not chat._valid(bad), bad
    assert not chat._is_secret_command("/suggest")
    assert not chat._is_secret_command("!secrets leak")  # prefix must not swallow a longer word
    assert not chat._is_secret_command("!secretx")
    assert chat._is_secret_command("!secret x")
    assert chat._is_secret_command("!secret")
    assert chat._is_secret_command("/secret x")   # alias, where the platform passes it through
    assert not chat._is_secret_command("!secrets leak")
    assert not chat._is_secret_command("!secretx")
    print("  PASS  name rules")


def check_injection_hint() -> None:
    """Non UPPER_SNAKE leaves warn that the value will not become an env var."""
    assert "will not be injected" in chat._run("!secret shared/sc_lower v")
    assert "will not be injected" not in chat._run("!secret shared/SC_UPPER v")
    for name in ("shared/sc_lower", "shared/SC_UPPER"):
        subprocess.run(["pass", "rm", "-f", name], capture_output=True)
    print("  PASS  injection hint")


def _dispatch(user_id, allowed):
    """Run on_dispatch for one !secret message; returns (hook result, reply sent)."""

    class Src:
        platform = "telegram"
        chat_id = "100"
        profile = ""

    Src.user_id = user_id

    class Ev:
        source = Src()
        text = "!secret shared/SELFTEST_ANY v"

    class Adapter:
        sent = None

        async def send(self, _chat_id, text):
            self.sent = text

    adapter = Adapter()

    class Gw:
        adapters = {"telegram": adapter}

    original = (chat._run, chat._get_setting)
    chat._run = lambda _text: "STORED"
    chat._get_setting = lambda key, default=None: allowed if key == "chat_allowed_users" else default
    try:
        result = asyncio.run(chat.on_dispatch(event=Ev(), gateway=Gw()))
    finally:
        chat._run, chat._get_setting = original
    return result, adapter.sent


def check_sender_gate() -> None:
    """The hook runs before Hermes' auth, so only a listed sender may store."""
    result, sent = _dispatch("999", [])
    assert result["action"] == "skip" and sent is None, (result, sent)  # off by default
    result, sent = _dispatch("999", ["telegram:123"])
    assert result["action"] == "skip" and sent is None, (result, sent)  # stranger
    result, sent = _dispatch("123", ["telegram:123"])
    assert result["action"] == "skip" and sent == "STORED", (result, sent)
    print("  PASS  sender gate")


def check_reply_routing() -> None:
    """The reply goes through the router when there is one.

    gateway.adapters only holds the primary profile's adapters; a multiplex
    profile must reply through _delivery_adapter_for or the reply is dropped.
    """
    from types import SimpleNamespace

    primary, mine = object(), object()

    class Routed:
        adapters = {"telegram": primary}

        def _delivery_adapter_for(self, source):
            return mine

    class Plain:
        adapters = {"telegram": primary}

    src = SimpleNamespace(platform="telegram", chat_id="c", user_id="1")
    assert chat._reply_adapter(Routed(), src) is mine
    assert chat._reply_adapter(Plain(), src) is primary
    print("  PASS  reply routing")


def main() -> int:
    check_registration()
    check_source()
    check_usage()
    check_name_rules()
    check_injection_hint()
    check_sender_gate()
    check_reply_routing()
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
