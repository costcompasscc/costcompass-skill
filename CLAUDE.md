# CostCompass — CLI + Claude Code plugin

This repo is **both** things at once, and that is the point:

- the standalone `costcompass` Python CLI (`src/costcompass/`), and
- the `costcompass` Claude Code plugin that ships it (`.claude-plugin/`,
  `skills/`, `commands/`, `bin/`).

They share one root because a plugin is installed by copying its root into a
versioned cache — so the plugin cannot reach a CLI living anywhere else and
must carry its own copy. Making the plugin root *be* the CLI root means there
is exactly one copy instead of a vendored duplicate to keep in sync.

This repo is public. The CostCompass monorepo is private and is assumed to be
checked out **alongside** this one as `../costcompass`; it symlinks this repo
back in at `client/plugin`. Cross-repo paths in this tree are written relative to
that layout.

## Stack

- Python 3.11+, **uv** package manager, **Typer** CLI, **httpx** client,
  **cryptography** for the vault JWE. `jwcrypto` is a dev-only dependency used
  purely for the cross-implementation vault vector test.

## Layout

```
.claude-plugin/  plugin.json + marketplace.json (this repo is its own marketplace)
skills/spend/    the /costcompass:spend skill
commands/        thin shims: mtd, refresh, auth-status — they delegate to the skill
bin/costcompass  bootstraps an isolated venv, then runs the CLI (see below)
scripts/         gen-build-files.sh — regenerates requirements.txt + VERSION
src/costcompass/
  main.py        Typer app (mtd + auth); arg parsing in pure plan_action()
  config.py      secret + URL resolution (see "Where secrets live"), 0600 file
  secrets.py     OS credential store (keyring): Keychain / Cred Manager / Secret Service
  api.py         httpx client for /api/v1 (injectable for tests)
  services.py    dynamic service-name -> provider-id resolution
  render.py      plain-text output (no rich)
  vault.py       GET + decrypt JWE (PBES2-HS256+A128KW / A256GCM), entry lookup, write-back
  refresh/
    orchestrator.py  fetch run end to end (flat + program paths)
    broker.py        POST /broker/v1/forward client (API-key auth, forward cap)
    program.py       generic FetchProgram interpreter (bindings, poll loop)
    signers.py       qc_hmac_v1 and the auth-header overlay
    oauth.py         short-lived access_token mint via the oauth-broker
tests/           pytest; HTTP mocked via injected httpx.MockTransport (no live stack)
```

**`requirements.txt` and `VERSION` are generated — never hand-edit them.** They
are derived from `pyproject.toml` + `uv.lock`; `bin/costcompass` needs a flat
hash-pinned list because there is no uv and no lock resolution at install time.

```bash
scripts/gen-build-files.sh          # regenerate
scripts/gen-build-files.sh --check  # fails if stale; runs in ./run-tests.sh
```

## Releasing

This repo is its own marketplace, so **pushing to `main` publishes**. What
decides whether an *already-installed* plugin updates is
`.claude-plugin/plugin.json`'s version — it names the install cache directory.
Merge code without moving that number and existing users keep running what they
already have, silently.

```bash
scripts/release.sh 1.1.0   # bump, relock, regenerate, test, then confirm before publishing
```

That is the only supported way to change the version — **never hand-edit
`pyproject.toml`'s `version`**. The script authors it there, reruns `uv lock`
and `gen-build-files.sh` so the derived files follow, runs the full suite, and
only then commits, tags `v<version>`, and pushes. It refuses a dirty tree, a
non-`main` branch, or a version that is unchanged or already tagged.

Which number: any change to shipped behavior is a minor bump; a release that is
only fixes is a patch bump.

## Conventions

- **Mirror the browser, don't reinvent.** The wire shapes (`FetchPlan`,
  `FetchProgram`, `RawResponse`) come from
  `../costcompass/backend/app/schemas/fetch.py`; the relay/interpreter logic
  comes from `../costcompass/frontend/src/lib/refresh/`. This is one of
  **three** lockstep relay implementations (browser reference, this CLI, and
  the macOS app in `../costcompass/client/macos/`) — the canonical
  file-correspondence table lives in that repo's root CLAUDE.md under "Three
  relay implementations (lockstep)", and each file carries a `LOCKSTEP:`
  header naming its siblings. A semantic change here must land in the other
  two. Because the set now spans two repos, a bare `git grep LOCKSTEP` no
  longer sees all of it — run `make lockstep` from `../costcompass`, which
  greps both.
- **No plugin-id branching, and no provider tables.** Like
  `../costcompass/frontend/src/lib/`, this code branches on plan/credential
  *shape* (flat vs `program`, direct vault entry vs minted), never on a
  provider id literal. **Credential routing is server-authored**: each
  fetch-run entry carries a `credential` object
  (`{kind: "vault_key" | "oauth_mint" | "oauth_installation_grant",
  sentinel_key?, mint_path?}`) the App Server builds from the plugin's
  `credential_routing()`. The CLI executes the kinds it implements (direct
  vault entry first, then `oauth_mint`) and skips any other kind with a
  `no_credentials` synthetic. Adding an OAuth provider — or a new credential
  variant — never touches this tree.
- **Security.** Never log the vault password, decrypted keys, minted tokens,
  or the API key. The decrypted vault stays in process memory only — never
  written to disk. Neither secret is ever accepted as an argv value.
- **Never store an unverified secret.** `auth login` proves the key against
  the server and `auth vault` proves the password actually decrypts the vault,
  each *before* writing anything. This is not politeness: the earlier
  `auth login` prompted, wrote, and printed "Saved credentials" without
  checking, so a vault password typed at the API-key prompt was persisted in
  plaintext, silently destroyed the real key (`O_TRUNC`), and reported
  success. Any new secret-accepting command verifies first.

## Where secrets live

Resolution is **most-hardened first**, and every resolver returns the source
alongside the value so `auth status` can report which one won:

| Value | 1st | 2nd | 3rd |
|---|---|---|---|
| API key | credential store | `COSTCOMPASS_API_KEY` | — never the file |
| Vault password | credential store | `COSTCOMPASS_VAULT_PASSWORD` | `vault_password` in the file |
| Base URL | `COSTCOMPASS_API_URL` | `api_url` in the file | built-in default |

Consequence to keep in mind: because the store wins, an exported
`COSTCOMPASS_API_KEY` does **not** override a stored key. That is intended;
`auth status` exists so it is discoverable rather than mysterious.

`secrets.py` mirrors the macOS app's `SecretsStore` protocol but uses its own
service (`cc.costcompass.cli`, not `cc.costcompass.menubar`) so the two
clients never disturb each other's credentials. No usable backend is a
*fallback*, not an error — `store()` returns a `NullStore` and resolution
continues to env/file. Linux's many backends plug in through `keyring`'s entry
points; don't hand-roll per-OS code here.

`vault_password` in the config file is a deliberate last resort for machines
with no credential store. It is plaintext at rest — say so whenever
documenting it, and never present it as the default.

**Tests must never touch the real credential store or `~/.config`.**
`tests/conftest.py` has autouse fixtures that redirect both. They are autouse
because a test that forgets writes the developer's own secrets into their
login keychain — which has already happened once.

- **Testability.** `api.Client` and `BrokerClient` take an injectable
  `httpx.Client`; tests use `httpx.MockTransport`. Arg parsing lives in the
  pure `plan_action()` so it is unit-tested without a network.

## Intentional scope limits

- **No `prefetch` / auto-discovery.** The browser runs a best-effort
  per-plugin `prefetch` before each fetch (e.g. Google billing-table
  auto-discovery). The CLI skips it and relies on already-configured cards — a
  google card set up in the app refreshes fine; a brand-new project the CLI has
  never seen won't be auto-discovered.
- **GitHub Organization-App (installation) rows are unsupported.** They need
  an App-Server-issued mint grant, not a vault refresh-token sentinel. The CLI
  doesn't special-case them — the App Server routes such rows to
  `credential.kind == "oauth_installation_grant"`, a kind the CLI doesn't
  implement, so the generic credential resolver **skips it cleanly** (a
  `no_credentials` synthetic → benign `skipped`) and prints a note to refresh
  that card from the app. PAT cards (direct vault entry) and User-App cards
  (`oauth_mint`) work.

## Tests

```bash
./run-tests.sh      # pytest + the generated-file staleness check
uv run pytest       # tests only
```

The vault decrypt is cross-checked against `jwcrypto` (an independent JOSE
implementation) so the JWE format stays portable. The monorepo's
`./run-tests.sh` calls this script through its `client/plugin` symlink as its
**client** phase (`--client-only` / `--no-client`), skipping it when this repo isn't
checked out — so there is one list of test commands, not two.

## The `bin/costcompass` wrapper

`bin/costcompass` runs the CLI from an isolated virtualenv under
`~/.local/share/costcompass-plugin/`, **content-addressed**: the venv is named
for a digest of the code installed into it (`src/**.py` + `requirements.txt` +
`pyproject.toml`). Do not "simplify" this to key on `VERSION` — that is a
shadow state, since the CLI's version need not change when its code does, and a
shipped fix would then leave every existing venv silently running the old code.
It ignores any `costcompass` on PATH by design, and deliberately does **not**
trust `CLAUDE_PLUGIN_DATA`: in a Bash-tool process that variable carries
whatever plugin context the harness holds, which has been observed pointing at
an unrelated plugin's directory.

Bootstrapping needs **uv, or a Python 3.11+ on PATH** — uv is preferred because
it supplies its own interpreter; the fallback uses the newest `python3.x` it
finds, since a stock macOS `python3` is 3.9 and the locked dependencies don't
resolve for it. Neither present → a clear error, not a pip stack trace.

Three constraints the wrapper holds; break one and the failure is subtle, so
re-check them by hand if you touch it (they have no automated test):

- Bootstrap output goes to **stderr** — callers parse stdout as JSON, and a
  stray pip notice on the first run would corrupt it.
- A `mkdir` lock serializes concurrent first-runs; the skill may issue several
  CLI calls in one turn, and two cold bootstraps would otherwise clobber each
  other's venv.
- `bin/costcompass` is created last (deps install first), so an interrupted
  build is retried rather than mistaken for a complete one.
