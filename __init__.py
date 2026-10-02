"""pass (password-store) secret source plugin for Hermes.

Resolves secrets from a local GPG-encrypted password-store into environment
variables at process startup. Works alongside or as a replacement for
Bitwarden Secrets Manager.

Config in config.yaml:
    secrets:
      sources: [pass, bitwarden]   # pass primary, BWS fallback
      pass:
        enabled: true
        store_path: ~/.password-store   # default: ~/.password-store
        subdirs: [shared, phoenix]       # which subdirectories to pull

The plugin walks each configured subdirectory, runs `pass show <path>` for
each entry, and returns the merged {ENV_VAR: value} dict. The orchestrator
handles precedence, conflict detection, and os.environ writes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

try:
    from agent.secret_sources.base import (
        ErrorKind,
        FetchResult,
        SecretSource,
        is_valid_env_name,
        run_secret_cli,
    )
except ImportError:
    # Allow the package to be imported outside Hermes (tests, dev, static
    # analysis). Inside Hermes the agent package is on sys.path and the
    # primary import succeeds.
    from abc import ABC, abstractmethod
    from dataclasses import dataclass, field
    from enum import Enum
    from pathlib import Path
    from typing import Dict, List, Optional

    class ErrorKind(Enum):
        BINARY_MISSING = "binary_missing"
        NOT_CONFIGURED = "not_configured"
        AUTH_FAILED = "auth_failed"
        TIMEOUT = "timeout"
        UNKNOWN = "unknown"

    @dataclass
    class FetchResult:
        secrets: Dict[str, str] = field(default_factory=dict)
        warnings: List[str] = field(default_factory=list)
        error: Optional[str] = None
        error_kind: Optional[ErrorKind] = None
        binary_path: Optional[Path] = None

    class SecretSource(ABC):
        name: str = ""
        label: str = ""
        shape: str = ""
        scheme: str = ""
        protected_env_vars: List[str] = []

        @abstractmethod
        def fetch(self, cfg: dict, home_path: Path) -> FetchResult: ...
        @abstractmethod
        def config_schema(self) -> dict: ...
        @abstractmethod
        def fetch_timeout_seconds(self, cfg: dict) -> float: ...
        def override_existing(self, cfg: dict) -> bool:
            return False

    def is_valid_env_name(name: str) -> bool:
        return name.isidentifier() and name.isupper()

    import subprocess
    def run_secret_cli(cmd, allow_env=None, timeout=30):
        env = {k: v for k, v in os.environ.items()
               if allow_env and k in allow_env}
        return subprocess.run(cmd, capture_output=True, text=True,
                            timeout=timeout, env={**os.environ, **env})


def _pass_install_hint() -> str:
    """Platform-aware `pass` install command for error messages."""
    import platform
    system = platform.system()
    if system == "Darwin":
        return "brew install pass"
    if system == "Linux":
        import shutil as _sh
        if _sh.which("pacman"):
            return "sudo pacman -S pass"
        if _sh.which("dnf"):
            return "sudo dnf install pass"
        if _sh.which("apt"):
            return "sudo apt install pass"
        return "install pass via your package manager (apt/brew/pacman/dnf)"
    return "install pass from https://www.passwordstore.org/"


class PassSource(SecretSource):
    """pass (password-store) as a registered secret source.

    Bulk source: injects every secret in the configured subdirectories.
    The leaf name of each pass entry becomes the env var name.
    """

    name = "pass"
    label = "pass (password-store)"
    shape = "bulk"
    scheme = "pass"
    # No protected_env_vars — pass uses GPG agent, not an env-var bootstrap token.
    # The GPG key passphrase is managed by gpg-agent, not by Hermes.

    def override_existing(self, cfg: dict) -> bool:
        # Default True (matches BWS behavior): the point of a central store
        # is centralized rotation. If .env had the final say, rotating a key
        # in pass wouldn't take effect until the stale .env line was deleted.
        return bool(isinstance(cfg, dict) and cfg.get("override_existing", True))

    def config_schema(self) -> dict:
        return {
            "enabled": {"description": "Master switch", "default": False},
            "store_path": {
                "description": "Path to the password-store directory",
                "default": "~/.password-store",
            },
            "subdirs": {
                "description": "Subdirectories to pull secrets from (e.g. [shared, phoenix])",
                "default": ["shared"],
            },
            "override_existing": {
                "description": "pass values overwrite .env/shell values",
                "default": True,
            },
            "timeout_seconds": {
                "description": "Wall-clock budget for fetch()",
                "default": 30,
            },
        }

    def fetch_timeout_seconds(self, cfg: dict) -> float:
        try:
            val = float((cfg or {}).get("timeout_seconds", 30))
        except (TypeError, ValueError):
            return 30.0
        return val if val > 0 else 30.0

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        """Resolve secrets from pass. MUST NOT raise. MUST NOT prompt."""
        result = FetchResult()
        cfg = cfg if isinstance(cfg, dict) else {}

        # Find the pass binary
        pass_bin = shutil.which("pass")
        if not pass_bin:
            result.error = (
                f"secrets.pass.enabled is true but `pass` binary is not on PATH. "
                f"Install with: {_pass_install_hint()} && pass init <GPG_KEY_ID>"
            )
            result.error_kind = ErrorKind.BINARY_MISSING
            return result

        result.binary_path = Path(pass_bin)

        # Resolve store path
        store_path_str = str(cfg.get("store_path", "~/.password-store"))
        store_path = Path(os.path.expanduser(store_path_str))
        if not store_path.exists():
            result.error = (
                f"pass store not found at {store_path}. "
                f"Initialize with: pass init <GPG_KEY_ID>"
            )
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result

        # Get subdirs to pull from
        subdirs = cfg.get("subdirs", ["shared"])
        if not isinstance(subdirs, list) or not subdirs:
            subdirs = ["shared"]

        secrets: Dict[str, str] = {}
        warnings: List[str] = []

        for subdir in subdirs:
            subdir = str(subdir).strip("/")
            if not subdir:
                continue

            # List all .gpg files in this subdirectory
            subdir_path = store_path / subdir
            if not subdir_path.exists():
                warnings.append(f"subdirectory '{subdir}' not found in pass store")
                continue

            # Find all .gpg files recursively
            gpg_files = sorted(subdir_path.rglob("*.gpg"))

            for gpg_file in gpg_files:
                # The pass path is the relative path without .gpg extension
                rel = gpg_file.relative_to(store_path)
                pass_path = str(rel.with_suffix(""))  # remove .gpg

                # The env var name is the leaf filename
                env_name = gpg_file.stem

                # Validate env var name
                if not is_valid_env_name(env_name):
                    warnings.append(
                        f"Skipping secret '{pass_path}': '{env_name}' is not a "
                        "valid env-var name (use UPPER_SNAKE_CASE, no hyphens)"
                    )
                    continue

                # Retrieve the secret value via `pass show`
                try:
                    proc = run_secret_cli(
                        [pass_bin, "show", pass_path],
                        allow_env=["GNUPGHOME", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS"],
                        timeout=10,
                    )
                except RuntimeError as exc:
                    warnings.append(f"Failed to retrieve '{pass_path}': {exc}")
                    continue

                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or "").strip()[:200]
                    warnings.append(f"pass show '{pass_path}' failed: {err}")
                    continue

                value = (proc.stdout or "").strip()
                if not value:
                    warnings.append(f"pass show '{pass_path}' returned empty value")
                    continue

                # First claim wins within the same source
                if env_name not in secrets:
                    secrets[env_name] = value
                else:
                    warnings.append(
                        f"Duplicate env var '{env_name}' from '{pass_path}' — "
                        f"already set by another entry in the configured subdirs"
                    )

        result.secrets = secrets
        result.warnings.extend(warnings)
        return result


def register(ctx):
    """Plugin registration entry point.

    Registers the pass secret source. The framework re-pulls enabled plugin
    secret sources immediately after discovery (reset_secret_source_cache +
    load_hermes_dotenv), so our secrets will be applied by the orchestrator
    with proper precedence, conflict detection, and provenance.

    Per the SecretSource plugin contract: "You fetch; the orchestrator applies."
    """
    ctx.register_secret_source(PassSource())
    ctx.register_system_prompt_section(
        "pass-secrets-resolution",
        _SECRETS_PROMPT_NOTE,
    )


# Static, ~600 chars of the 4000 cap. Frozen into each new session prompt.
_SECRETS_PROMPT_NOTE = """## Fleet secrets: pass store, NOT raw env probes

Secrets (API keys, tokens) resolve from the GPG-encrypted pass store (~/.password-store) through Hermes' secret sources — they are NOT in os.environ or .env directly. Names listed in this profile's `terminal.env_passthrough` (config.yaml) are injected into terminal/execute_code child processes automatically: use them as plain env vars (`os.environ.get("NAME")`, `$NAME`). Do NOT run `pass show` in terminal — the declared names are already there.

- Which names: `grep -A30 env_passthrough <profile>/config.yaml` (per-profile list)
- A name missing from env = not in this profile's passthrough list (add it to terminal.env_passthrough), not a missing secret
- Provider credentials (OLLAMA/OPENROUTER/TAVILY/etc.) are blocked from passthrough by upstream design (GHSA-rhgp-j443-p4rf): they resolve inside the Hermes runtime only — an empty probe for those is expected"""