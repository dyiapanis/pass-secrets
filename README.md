# pass-secrets

**pass (password-store) secret source for [Hermes Agent](https://hermes-agent.nousresearch.com)** — resolves secrets from your local GPG-encrypted password store into environment variables at startup. Local filesystem, git-tracked, no network dependency, no token expiry.

## Why

Hermes ships Bitwarden Secrets Manager and 1Password sources. If your secrets already live in [`pass`](https://www.passwordstore.org/) — GPG-encrypted, git-backed, fully offline — this lets Hermes read them as a first-class secret source instead of migrating to a hosted vault.

## How it works

The plugin registers a `SecretSource` with the Hermes orchestrator. **You fetch; the orchestrator applies** — the plugin never writes `os.environ` itself, never prompts, never raises. Each pass entry's leaf name becomes the env var name (`UPPER_SNAKE_CASE` enforced).

```yaml
# profile config.yaml
secrets:
  sources: [pass, bitwarden]   # pass primary, BWS fallback
  pass:
    enabled: true
    store_path: ~/.password-store   # default
    subdirs: [shared, phoenix]       # pass directories to pull
    override_existing: true          # pass wins over .env/shell
    timeout_seconds: 30
```

Resolution flow:

1. `register(ctx)` runs at plugin discovery → `ctx.register_secret_source(PassSource())`
2. The orchestrator re-pulls enabled secret sources at startup (`reset_secret_source_cache` + dotenv load)
3. `fetch()` walks each configured subdirectory, runs `pass show <path>` per `.gpg` entry (10s timeout, GPG agent env preserved)
4. Merged `{ENV_VAR: value}` returns to the orchestrator, which handles precedence, conflict detection, and provenance

## What you'll see in session prompts

The plugin also registers a static system-prompt note teaching agents where secrets resolve from (they cannot see raw process env; provider credentials are runtime-only by upstream design). That's ~600 characters of the 4000-char prompt budget, no dynamic content.

## Disclosures

- **Reads your GPG-encrypted pass store** by shelling out to the `pass` binary per entry, with `GNUPGHOME` / `GPG_AGENT_INFO` preserved. If your GPG key prompts a passphrase, `gpg-agent` handles it — the plugin never prompts.
- **Bulk injection:** everything in the configured subdirs becomes env vars. Configure narrowly (`subdirs:`) — don't point it at a store subdirectory containing material you wouldn't put in a process environment.
- **No network. Ever.** Pure local: subprocess + filesystem only.
- **No protected env vars:** pass uses GPG agent, not an env-var bootstrap token.

## Requirements

- `pass` on PATH (GPG-based password store initialized: `pass init <GPG_KEY_ID>`)
- Hermes Agent `>=0.21.4` (SecretSource plugin API with `register_secret_source`)
- No Python dependencies beyond stdlib

## Missing/broken secrets fail cleanly

- `pass` not on PATH → typed `BINARY_MISSING` error with platform-specific install hint
- Store path missing → `NOT_CONFIGURED` with `pass init` hint
- Individual entry fails → warning collected, remaining entries still load

## License

MIT — see [LICENSE](LICENSE).