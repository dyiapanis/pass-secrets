#!/usr/bin/env python3
"""Self-check for pass-secrets — runs from a checkout, no Hermes install needed.

    python3 selftest.py

Covers registration wiring, the source contract against the local pass store
(skipped when no store is present), and the /s chat lane's parsing rules.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
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
    text = chat._run("/s")
    assert "/s <name> <value>" in text, text
    assert "/s shared/MY_API_KEY" in text, text
    assert "/s agentname/MY_API_KEY" in text, text
    assert "`agentname/` = this agent only" in text, text
    print("  PASS  usage text")


def check_name_rules() -> None:
    """A name must be a path; bare words and traversal are refused."""
    assert chat._valid("shared/KEY")
    assert chat._valid("work/NESTED/KEY")
    for bad in ("get", "", "../etc/passwd", "shared/../x", "breakglass/K", "shared/", "/leading"):
        assert not chat._valid(bad), bad
    assert not chat._is_secret_command("/suggest")
    assert chat._is_secret_command("/s x")
    print("  PASS  name rules")


def check_injection_hint() -> None:
    """Non UPPER_SNAKE leaves warn that the value will not become an env var."""
    assert "will not be injected" in chat._run("/s shared/sc_lower v")
    assert "will not be injected" not in chat._run("/s shared/SC_UPPER v")
    for name in ("shared/sc_lower", "shared/SC_UPPER"):
        subprocess.run(["pass", "rm", "-f", name], capture_output=True)
    print("  PASS  injection hint")


def check_no_sender_gate() -> None:
    """The hook applies no sender policy: a stranger's /s is still handled."""
    assert not hasattr(chat, "_authorized") and not hasattr(chat, "_allowlist")

    class Src:
        user_id = "@stranger:example.org"
        chat_id = "!room"
        profile = ""

    class Ev:
        source = Src()
        text = "/s shared/SELFTEST_ANY v"

    class Gw:
        sent = None

        def _delivery_adapter_for(self, _source):
            return self

        async def send(self, _chat_id, text):
            self.sent = text

    gw = Gw()
    original = chat._run
    chat._run = lambda _text: "STORED"
    try:
        asyncio.run(chat.on_dispatch(event=Ev(), gateway=gw))
    finally:
        chat._run = original
    assert gw.sent == "STORED", f"unexpected reply: {gw.sent!r}"
    print("  PASS  no sender gate")


def main() -> int:
    check_registration()
    check_source()
    check_usage()
    check_name_rules()
    check_injection_hint()
    check_no_sender_gate()
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
