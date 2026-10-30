"""chat half: /s stores a secret from chat, bypassing the session store.

Registers ``pre_gateway_dispatch``, which fires at ingress BEFORE auth and
before any session exists. A handled message returns ``{"action": "skip"}``,
so the text never reaches the agent loop and is never written to state.db.
The reply goes out through the platform adapter (still E2EE).

    /s <name> <value>

The name is ``shared/<KEY>`` (all agents) or ``<agent>/<KEY>`` (one profile).
Everything after the first space is the value. Values are never logged.

NO SENDER CHECK HERE: who may reach the bot is Hermes' channel/pairing
management's decision, applied outside this hook. Anything that does arrive may
store a secret; a shared/ entry becomes an environment variable for every agent.
"""

from __future__ import annotations

import re
import subprocess

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
PREFIX = "/s"

USAGE = (
    "**/s** — store a secret\n"
    "\n"
    "`/s <name> <value>`\n"
    "\n"
    "Examples:\n"
    "`/s shared/MY_API_KEY sk-abc123`   ← every agent\n"
    "`/s agentname/MY_API_KEY sk-abc123`   ← this agent only\n"
    "\n"
    "`shared/` = every agent · `agentname/` = this agent only\n"
    "`MY_API_KEY` = the key name — you choose it\n"
    "`sk-abc123` = the secret, everything after the first space"
)


def _err(msg: str) -> str:
    """Every mistake answers with the reason AND the syntax."""
    return f"{msg}\n\n{USAGE}"


def _valid(name: str) -> bool:
    # must be a path: `<dir>/<KEY>`. A bare word is rejected, so a mistyped
    # command ("/s get shared/X") can never be stored as an entry called "get".
    return (
        bool(name)
        and "/" in name
        and ".." not in name
        and not name.startswith(("breakglass", "."))
        and bool(NAME_RE.match(name))
    )


def _insert(name: str, value: str) -> bool:
    p = subprocess.run(["pass", "insert", "-m", "-f", name], input=value + "\n",
                       capture_output=True, text=True, timeout=20)
    return p.returncode == 0


def _run(text: str) -> str:
    """Handle one /s message; returns the reply text."""
    parts = text.split(None, 2)
    if len(parts) == 1:
        return USAGE
    if len(parts) < 3:
        return _err("missing value")
    name, value = parts[1], parts[2]
    if not _valid(name):
        return _err(f"bad name `{name}` — use shared/<KEY> or <agent>/<KEY>, no '..'")
    if not value.strip():
        return _err("empty value")
    if not _insert(name, value):
        return f"failed to store {name}"
    leaf = name.rsplit("/", 1)[-1]
    hint = "" if leaf.isupper() else f" — note: `{leaf}` is not UPPER_SNAKE, so it will not be injected as an env var"
    return f"stored `{name}`{hint}"


def _is_secret_command(text: str) -> bool:
    return text == PREFIX or text.startswith(PREFIX + " ")


async def on_dispatch(*, event=None, gateway=None, session_store=None, **_kwargs):
    """pre_gateway_dispatch. Fail closed: any error still skips the message."""
    try:
        source = getattr(event, "source", None)
        text = (getattr(event, "text", "") or "").strip()
        if not source or not text or not _is_secret_command(text):
            return None

        await _send(gateway, source, _run(text))
        return {"action": "skip", "reason": "pass-secrets: handled"}
    except Exception:  # noqa: BLE001 — never let a failure hand the text to the agent
        return {"action": "skip", "reason": "pass-secrets: error"}


async def _send(gateway, source, text: str) -> None:
    try:
        adapter = gateway._delivery_adapter_for(source)
        if adapter:
            await adapter.send(source.chat_id, text)
    except Exception:  # noqa: BLE001
        pass

