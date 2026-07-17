# CostCompass for Claude Code

Ask Claude Code what you're spending on AI and cloud providers, and get an
answer from your own [CostCompass](https://costcompass.cc) account.

```
/plugin marketplace add costcompasscc/costcompass-skill
/plugin install costcompass@costcompass-skill
```

Then just ask — "what's my spend this month?", "how much on claude?", "which
provider costs the most?" — or use the commands directly:

| | |
|---|---|
| `/costcompass:spend` | Ask about spend in your own words |
| `/costcompass:mtd [service]` | Month-to-date total, or one service |
| `/costcompass:refresh [service]` | Pull fresh usage from providers, then report |
| `/costcompass:auth-status` | Check you're authenticated and the vault is readable |

## Setup

The plugin bundles the `costcompass` CLI, so you don't install it separately.
On first use it builds an isolated virtualenv under
`~/.local/share/costcompass-plugin/` — this needs network and takes a few
seconds; after that it's instant. It never touches a `costcompass` you may
already have on your PATH.

**Requirements: [uv](https://docs.astral.sh/uv/getting-started/installation/),
or Python 3.11+.** uv is the easy path — it supplies its own Python, so nothing
else is needed. Without uv, the plugin uses your own `python3`, which must be
3.11 or newer (note that macOS still ships 3.9, so on a stock Mac install uv).
If neither is available, the plugin tells you so on first run rather than
failing obscurely.

## Authenticating

You need an API key from CostCompass (Settings → API keys). Both prompts are
hidden, so run these in a **real terminal window** — not inside Claude Code,
whose `!` prefix gives the command no TTY, so the prompt cannot hide your
keystrokes and the CLI refuses to run rather than echo a secret:

```
costcompass auth login     # your API key
costcompass auth vault     # your vault password, needed only for refreshing
```

Each one **verifies before it stores**: the key against the server, the password
against your actual vault. Neither is ever accepted as a command-line argument,
and both go into your OS credential store — Keychain on macOS, Credential
Manager on Windows, Secret Service on Linux.

Prefer environment variables? Export `COSTCOMPASS_API_KEY` and
`COSTCOMPASS_VAULT_PASSWORD` before launching Claude Code and skip the above.
The stored value wins if both exist; `/costcompass:auth-status` always tells you
which is in use.

## Refreshing

Once your vault password is stored, refreshing just works — ask Claude to
refresh, or run `/costcompass:refresh`. There's nothing to type, because the
password resolves from your credential store.

Your vault password unlocks every provider credential, so: never paste it into
the chat, and never put it in `.zshrc` or `.env`.

## Privacy

Your provider API keys stay in your browser-side encrypted vault. The
CostCompass server never sees them in plaintext, and neither does this plugin
outside the moment it uses them to fetch your usage.

---

# Using the CLI directly

You don't need Claude Code — the same `costcompass` command runs standalone,
authenticating with a programmatic API key you generate in the app
(Settings → API keys).

```bash
uv tool install .        # installs the `costcompass` command
# or, for local development:
uv sync && uv run costcompass --help
```

## Authentication

```bash
costcompass auth login           # verify an API key, then store it (hidden prompt)
costcompass auth login --url http://localhost:8080/api/v1
costcompass auth vault           # verify the vault password, then store it
costcompass auth status          # where each secret comes from, and whether it works
costcompass auth status --json   # same, machine-readable
```

Both `login` and `vault` **verify before they store** — the key against the
server, the password against your actual vault. A secret that doesn't work is a
secret that was mistyped, and storing it would only hide the mistake until
later.

### Where the secrets come from

Resolution is **most-hardened first**. `auth status` always reports which source
won, so "which one am I actually using?" is never a guess.

| Value          | 1st                   | 2nd                            | 3rd                              |
|----------------|-----------------------|--------------------------------|----------------------------------|
| API key        | OS credential store   | `COSTCOMPASS_API_KEY`          | — (never the config file)        |
| Vault password | OS credential store   | `COSTCOMPASS_VAULT_PASSWORD`   | `vault_password` in the config file |
| Base URL       | `COSTCOMPASS_API_URL` | `api_url` in the config file   | `https://costcompass.cc/api/v1`  |

The **OS credential store** is your platform's own: Keychain on macOS,
Credential Manager on Windows, Secret Service on Linux (KWallet and other
backends plug in through `keyring`). A machine with no usable backend isn't an
error — the CLI just falls through to the next source.

Because the store wins, an exported `COSTCOMPASS_API_KEY` will **not** override
a stored key. That's deliberate. Use `auth status` to see which is in play, or
`auth login` to replace the stored one.

The config file lives at `$XDG_CONFIG_HOME/costcompass/config.toml` (falling
back to `~/.config/...`) and is written `0600`. It holds the non-secret
`api_url`, and optionally `vault_password`. **The API key is never written to
it.**

`vault_password` in the config file is a supported last resort for machines
with no credential store — but it is **plaintext at rest**: readable by anything
running as you, and it reaches Time Machine and any file-sync tool. Prefer
`costcompass auth vault`. Never put either secret in `.zshrc` or `.env`, where
it also leaks into shell history and version control.

## Usage

```bash
costcompass mtd                  # total MTD across all services (the big number)
costcompass mtd claude           # MTD for one service
costcompass mtd claude details   # that service's totals + per-model breakout
costcompass mtd breakdown        # every card (providers + subscriptions) ranked, reconciling to the total
costcompass mtd higgsfield       # a standalone subscription, addressed by name
```

Service names resolve dynamically against the server (`claude` →
`anthropic`, etc.), so you can use the friendly name or the provider id.
A name that isn't a metered provider falls back to a standalone
**subscription** card (e.g. a Higgsfield plan); since a subscription is a
flat fee it shows just its amount, with no burn/forecast/per-model detail.
`mtd breakdown` is the unified view — it lists providers *and* subscriptions
and its total matches `mtd`.

### JSON output

Add `--json` to any command for machine-readable output (it implies
`--quiet`, and errors still go to stderr with a non-zero exit):

```bash
costcompass mtd --json                   # the full summary object
costcompass mtd claude --json            # one service's summary
costcompass mtd claude details --json    # { provider_id, display_name, summary, models }
costcompass mtd breakdown --json         # { total_usd, cards: [ every provider + subscription ] }
costcompass mtd refresh --vault --json   # { mtd_usd, providers: [ per-card outcomes ] }
```

### Refresh

Refresh pulls fresh usage from your providers. It needs your **vault
password** to unlock the provider credentials:

```bash
costcompass mtd refresh --vault          # refresh every service (prompts for the password)
costcompass mtd claude refresh --vault   # refresh only one service
costcompass mtd refresh --vault --quiet  # no progress ticker
```

The vault password is **never** taken as a command-line argument (it
would leak via shell history). `--vault` prompts interactively; for
non-interactive use, pipe it on stdin or set
`COSTCOMPASS_VAULT_PASSWORD`.

### Non-interactive refresh (automation)

For a scripted/cron refresh the password never needs a prompt. `--vault` states
the intent; the password itself resolves from the credential store, the
environment, or the config file (see [Authentication](#authentication)), and is
**never** read from argv.

Simplest — store it once, then refresh forever:

```bash
costcompass auth vault                   # verifies, then stores it
costcompass mtd refresh --vault --json   # no prompt, nothing to type
```

Or keep it in the environment only, for a machine where you'd rather persist
nothing:

```bash
read -rs COSTCOMPASS_VAULT_PASSWORD && export COSTCOMPASS_VAULT_PASSWORD
costcompass mtd refresh --vault --json
unset COSTCOMPASS_VAULT_PASSWORD         # drop it when done
```

`--vault` also falls back to stdin when not a TTY:

```bash
printf '%s' "$pw" | costcompass mtd refresh --vault --json
```

Your vault password decrypts *every* provider credential, so it deserves the
most protection of anything here. Prefer the credential store; if you use the
environment, export it in the shell *before* launching the tool that needs it
(e.g. Claude Code) so it stays in process memory. **Never** put it in `.zshrc`
or `.env` — that is plaintext plus shell history, and committed configs leak it
to git.

A refresh can take several seconds, so it prints a `.` per second while
it works (only on an interactive terminal — never when piped to a file).
Pass `--quiet` to suppress it.

---

## For maintainers

This repo is both the CLI and the plugin that ships it. They share one root
because a plugin is installed by copying its root into a versioned cache, so it
cannot reach a CLI stored anywhere else — one root means one copy of the code
rather than a vendored duplicate that drifts.

```bash
./run-tests.sh    # pytest + the generated-file staleness check
```

**`requirements.txt` and `VERSION` are generated — never hand-edit them.** They
derive from `pyproject.toml` + `uv.lock`, because `bin/costcompass` installs
with `--require-hashes` and has no lockfile resolver at install time:

```bash
scripts/gen-build-files.sh          # regenerate
scripts/gen-build-files.sh --check  # fail if stale (runs in ./run-tests.sh)
```

The refresh path here is one of three lockstep relay implementations (the
browser is the reference; the macOS menu-bar app is the third). The other two
live in the CostCompass monorepo, assumed checked out as `../costcompass`. See
[`CLAUDE.md`](CLAUDE.md) for the full rules.
