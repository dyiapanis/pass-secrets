"""chat half: !secret stores a secret from chat, bypassing the session store.

Registers ``pre_gateway_dispatch``, which fires at ingress BEFORE auth and
before any session exists. Every ``!secret`` message returns ``{"action": "skip"}``,
so the text never reaches the agent loop and is never written to state.db.
The reply goes out through the platform adapter (still E2EE).

    !secret <name> <value>

The name is ``shared/<KEY>`` (all agents) or ``<agent>/<KEY>`` (one profile).
Everything after the first space is the value. Values are never logged.

SENDER CHECK HERE: on the messaging path this hook runs before Hermes'
sender authorization, so the hook itself decides who may store. Only senders
listed in the plugin setting ``chat_allowed_users`` (``<platform>:<user_id>``,
e.g. ``telegram:123456789``) can store; the list is empty by default, so ``!secret``
is off until you add yourself. Anyone else's ``!secret`` is dropped without a reply
(its text still never reaches the agent). A shared/ entry becomes an
environment variable for every agent, so list only people you would hand a
shell to.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
PREFIX = "!secret"
PREFIXES = (PREFIX, "/secret")  # `/secret` only works where unknown slash commands pass through

USAGE = (
    "**!secret** — store a secret\n"
    "\n"
    "`!secret <name> <value>`\n"
    "\n"
    "Examples:\n"
    "`!secret shared/MY_API_KEY sk-abc123`   ← every agent\n"
    "`!secret agentname/MY_API_KEY sk-abc123`   ← this agent only\n"
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
    # command ("!secret get shared/X") can never be stored as an entry called "get".
    return (
        bool(name)
        and "/" in name
        and ".." not in name
        and not name.startswith(("breakglass", "."))
        and bool(NAME_RE.match(name))
    )


def _insert(name: str, value: str) -> bool:
    env = dict(os.environ)
    if _store_path:
        # write to the store the source reads (secrets.pass.store_path)
        env["PASSWORD_STORE_DIR"] = os.path.expanduser(_store_path)
    p = subprocess.run(["pass", "insert", "-m", "-f", name], input=value + "\n",
                       capture_output=True, text=True, timeout=20, env=env)
    return p.returncode == 0


def _run(text: str) -> str:
    """Handle one !secret message; returns the reply text."""
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
    # `!secret` is the primary trigger because Mattermost (and any platform
    # with a slash-command registry) rejects an unregistered `/word` before
    # the message is posted — the hook never runs. `/secret` is kept for
    # platforms that pass unknown slash commands through as text.
    return any(text == p or text.startswith(p + " ") for p in PREFIXES)


# Set by register(): reads plugins.entries.pass-secrets.settings.<key>, and the
# store the source reads, so !secret writes where fetch() looks.
_get_setting = None
_store_path = None

# Senders already refused once; keeps an unlisted DM from writing a log line
# per message (the refusal itself is never suppressed).
_REFUSED_SENDERS: set = set()

# Failure kinds already logged (see _warn_once).
_WARNED: set = set()



def _sender_id(source) -> str:
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform) or ""
    user_id = getattr(source, "user_id", None)
    return f"{platform}:{user_id}" if platform and user_id else ""


def _allowed(source) -> bool:
    """Only an explicitly listed <platform>:<user_id> may store. Empty = off."""
    sender = _sender_id(source)
    if not sender or _get_setting is None:
        return False
    listed = _get_setting("chat_allowed_users", []) or []
    if not isinstance(listed, (list, tuple)):
        return False
    return sender in {str(item).strip() for item in listed}


async def on_dispatch(*, event=None, gateway=None, session_store=None, **_kwargs):
    """pre_gateway_dispatch. Fail closed: any error still skips the message."""
    try:
        source = getattr(event, "source", None)
        text = (getattr(event, "text", "") or "").strip()
        if not source or not text or not _is_secret_command(text):
            return None
        if not _allowed(source):
            # Runs before Hermes' auth: never reply to (or store for) an
            # unlisted sender, and never pass the value on to the agent.
            # Log once per sender: an unlisted DM is a probe, not a reason to
            # write a warning for every message.
            sender = _sender_id(source) or "unknown sender"
            if sender not in _REFUSED_SENDERS:
                _REFUSED_SENDERS.add(sender)
                logger.warning(
                    "pass-secrets: !secret from %s refused (not in chat_allowed_users; "
                    "further refusals from this sender are not logged)", sender)
            return {"action": "skip", "reason": "pass-secrets: sender not allowed"}

        await _send(gateway, source, _run(text))
        return {"action": "skip", "reason": "pass-secrets: handled"}
    except Exception:  # noqa: BLE001 — never let a failure hand the text to the agent
        return {"action": "skip", "reason": "pass-secrets: error"}


def _warn_once(key: str, msg: str, *args) -> None:
    """One log line per failure kind — a broken reply path must not be silent."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(msg, *args)


def _reply_adapter(gateway, source):
    """The adapter that ANSWERS this source.

    ``gateway.adapters`` only holds the primary/default profile's adapters; a
    multiplex profile (a served sibling) keeps its own in ``_profile_adapters``.
    ``_delivery_adapter_for`` is the router that knows the difference, so it goes
    first and the public map is the fallback.
    """
    router = getattr(gateway, "_delivery_adapter_for", None)
    if callable(router):
        try:
            adapter = router(source)
            if adapter is not None:
                return adapter
        except Exception as exc:  # noqa: BLE001
            _warn_once("router-failed", "pass-secrets: adapter routing failed: %s", exc)
    adapters = getattr(gateway, "adapters", None) or {}
    platform = getattr(source, "platform", None)
    return adapters.get(platform) or adapters.get(getattr(platform, "value", platform))


async def _send(gateway, source, text: str) -> None:
    adapter = _reply_adapter(gateway, source)
    if adapter is None:
        _warn_once("no-adapter", "pass-secrets: no reply adapter for %s — reply dropped",
                   getattr(source, "platform", "?"))
        return
    try:
        await adapter.send(source.chat_id, text)
    except Exception as exc:  # noqa: BLE001 — never let the reply break the skip
        _warn_once("send-failed", "pass-secrets: reply send failed: %s", exc)

