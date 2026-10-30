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
    subdirs: [shared, work]          # pass directories to pull
    override_existing: true          # pass wins over .env/shell
    timeout_seconds: 30
```

Resolution flow:

1. `register(ctx)` runs at plugin discovery → `ctx.register_secret_source(PassSource())`
2. The orchestrator re-pulls enabled secret sources at startup (`reset_secret_source_cache` + dotenv load)
3. `fetch()` walks each configured subdirectory, runs `pass show <path>` per `.gpg` entry (10s timeout, GPG agent env preserved)
4. Merged `{ENV_VAR: value}` returns to the orchestrator, which handles precedence, conflict detection, and provenance

## Storing a secret from chat — `/s`

The same plugin can write to the store without a shell, from any Hermes chat
channel:

```
/s <name> <value>
```

Examples:

```
/s shared/MY_API_KEY sk-abc123      # every agent/profile
/s agentname/MY_API_KEY sk-abc123   # one profile's directory only
```

The name is a pass path, so the leaf becomes the environment-variable name
(`UPPER_SNAKE` enforced for injection — anything else is stored but warned
about). Everything after the first space is the value, verbatim.

The command registers as a `pre_gateway_dispatch` hook, which fires at ingress
**before** Hermes' sender-authorisation check and before any session exists.
The plugin applies no sender policy of its own: who may reach the bot at all is
Hermes' channel/pairing management's concern. The practical consequence is that
any message that does arrive can write to the store — so treat channel access
as the boundary, and remember that a `shared/` entry becomes an environment
variable inside every agent process.

Values are passed to `pass insert` on stdin and are never logged, never written
to conversation state, and never echoed back.

## What you'll see in session prompts

The plugin also registers a conditional system-prompt note teaching agents where secrets resolve from (declared names are injected into child process env; provider credentials are runtime-only by upstream design). It appears **only when `secrets.pass.enabled` is true** and the `pass` binary is resolvable — a disabled plugin contributes nothing to the prompt. The note is ~900 characters of the 4000-char prompt budget, no dynamic content.

## Disclosures

- **Reads your GPG-encrypted pass store** by shelling out to the `pass` binary per entry, with `GNUPGHOME` / `GPG_AGENT_INFO` preserved. If your GPG key prompts a passphrase, `gpg-agent` handles it — the plugin never prompts.
- **Bulk injection:** everything in the configured subdirs becomes env vars. Configure narrowly (`subdirs:`) — don't point it at a store subdirectory containing material you wouldn't put in a process environment.
- **No network. Ever.** Pure local: subprocess + filesystem only.
- **No protected env vars:** pass uses GPG agent, not an env-var bootstrap token.

## Requirements

- `pass` on PATH (GPG-based password store initialized: `pass init <GPG_KEY_ID>`)
- Hermes Agent `>=0.21.4` (SecretSource plugin API with `register_secret_source`; `/s` needs the `pre_gateway_dispatch` hook)
- No Python dependencies beyond stdlib

## Missing/broken secrets fail cleanly

- `pass` not on PATH → typed `BINARY_MISSING` error with platform-specific install hint
- Store path missing → `NOT_CONFIGURED` with `pass init` hint
- Individual entry fails → warning collected, remaining entries still load

## License

MIT — see [LICENSE](LICENSE).