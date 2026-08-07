# CostCompass spend — cold-path reference

Load this **only** when the hot path in `SKILL.md` sends you here: the user is
not set up (`ready.spend`/`ready.refresh` false), asked to refresh, or asked
for a per-service / breakdown / per-model view. A bare "what's my spend" never
needs this file.

All commands use the bundled CLI by full path — never a bare `costcompass`:

```
${CLAUDE_PLUGIN_ROOT}/bin/costcompass
```

**When you SHOW one of these commands to the user rather than running it
yourself, substitute the resolved path first.** `CLAUDE_PLUGIN_ROOT` is set in
your environment, not in the user's shell — and the secret-storing commands
below are precisely the ones they must run in their own terminal window, where
it is unset. Handing them `${CLAUDE_PLUGIN_ROOT}/bin/costcompass auth login`
expands to `/bin/costcompass auth login` and fails with "no such file", which
looks like a broken install rather than a broken instruction. The path is not
guessable either: it is inside the installed plugin, under a directory named
for the plugin version, so it moves on every update.

Resolve it once, and paste the absolute result into your message:

```bash
echo "$CLAUDE_PLUGIN_ROOT/bin/costcompass"
```

## Fix: API key (`ready.spend` false)

Not being set up yet is the **normal first run**, not an error. The user asked
what they're spending; the answer is "you're not set up yet, here's the two ways
to fix it". Give them that and stop.

The user needs a CostCompass **API key**, issued from their account's settings.
Both options below assume they already hold one — so say where to get it
**first**. Telling someone to run `auth login` and only then mentioning the key
comes from Settings is backwards; they hit the hidden prompt with nothing to
type.

Build the settings link from the `server` field of the `auth status` output you
just ran: strip the trailing `/api/v1` and append `/app/settings/api-keys`. That
way the link points at whatever stack they are actually configured against
(`https://costcompass.cc/api/v1` → `https://costcompass.cc/app/settings/api-keys`;
a local dev stack → `http://localhost:8080/app/settings/api-keys`). Use the
`server` value to build the URL — do not print the raw JSON it came from.

**Say this, and little else:**

> Authorization has not been set up.
>
> **First, get an API key** from CostCompass → Settings → API keys:
> <server>/app/settings/api-keys
>
> **Then store it**, one of these two ways:
>
> **1. Keychain (recommended)** — verifies the key against the server, then
> stores it in your OS credential store. Run it in a **real terminal window**,
> not in Claude Code:
>
> ```
> ${CLAUDE_PLUGIN_ROOT}/bin/costcompass auth login
> ```
>
> **2. Environment variable** — export it, then relaunch Claude Code:
>
> ```
> export COSTCOMPASS_API_KEY=…
> ```

Adapt only these: substitute the real settings URL for `<server>/app/settings/api-keys`,
substitute the resolved CLI path (see the top of this file — the user's shell has
no `CLAUDE_PLUGIN_ROOT`), and append `--url <base>` to the `auth login` line if
`server` is not the default (e.g. a local dev stack).

**Do not**, in this message:

- **Tell the user to run `auth login` with the `!` prefix.** It cannot work.
  `!` gives the command no TTY, and the CLI refuses to take a secret without
  one (a hidden prompt can't suppress echo on a pipe), so it aborts rather
  than read the key in the clear — correct behaviour, useless advice. A hidden
  prompt needs a real terminal window. This applies to every secret-storing
  command (`auth login`, `auth vault`).
- Show or quote the `auth status --json` output. `source: null`,
  `valid: false`, and `ready.spend` are diagnostics for *you* — to the user they
  are noise that buries the two things they can actually do.
- Lead with what's broken, a status table, or a field-by-field readout.
- Explain the resolution order, or why the credential store beats the
  environment. They didn't ask.
- Volunteer the vault password — it is a *separate* secret and irrelevant to a
  spend question. Only raise it if they asked to refresh.

Never ask the user to paste the key into the chat, and never echo one back.

## Fix: vault password (`ready.refresh` false)

Only raise this if the user actually asked to refresh. Same shape as the API-key
fix above — two options, no diagnostics, no lecture.

**Say this, and little else:**

> Refreshing needs your vault password, which unlocks your provider
> credentials. Do one of the following:
>
> **1. Keychain (recommended)** — checks the password really decrypts your
> vault, then stores it. Run it in a **real terminal window**, not in Claude
> Code:
>
> ```
> ${CLAUDE_PLUGIN_ROOT}/bin/costcompass auth vault
> ```
>
> **2. Environment variable** — export it, then relaunch Claude Code:
>
> ```
> read -rs COSTCOMPASS_VAULT_PASSWORD && export COSTCOMPASS_VAULT_PASSWORD
> ```

Substitute the resolved CLI path in that `auth vault` line, for the same reason
as the `auth login` one above: they run it in their own terminal, where
`CLAUDE_PLUGIN_ROOT` does not exist.

A third option exists — `vault_password = "…"` in
`~/.config/costcompass/config.toml` (mode 0600) — but it is **plaintext on
disk**. Offer it only if they ask for it, and say that plainly when you do.

**Never** ask the user to paste the vault password into the chat, and never
echo, log, or store it yourself. Never pass it as a command-line argument —
`--vault <pw>` is rejected by design, since argv leaks via shell history and
`ps`.

If `auth status` shows `vault.source` as `config-file`, you may mention once
that moving it to the credential store (`auth vault`) is safer. Don't nag.

## Spend commands (drill-down)

Always use `--json`, then summarize in plain language.

```bash
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd --json                    # total month-to-date (all services)
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd breakdown --json          # EVERY card ranked (providers + subscriptions); total matches `mtd`
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd <service> --json          # one service (e.g. claude, openai, google)
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd <service> details --json  # that service's totals + per-model breakout
```

Chain the matching command after `auth status --json || true` (guarded with
`|| true`), exactly as the hot path does for the bare total — so a service or
breakdown request is still one round-trip.

For "where's my money / which costs the most / what's in my total", use
`mtd breakdown` — it's the only view that includes **standalone
subscriptions** (e.g. a Higgsfield plan), so its `total_usd` reconciles to the
`mtd` headline. A `<service>` that isn't a metered provider falls back to a
subscription card and reports just its flat amount (no burn/forecast/models).

**Never install the CLI from the current workspace.** It is already bundled
here. Do not run `uv tool install`, `pip install`, or any installer against the
session's repo: `git rev-parse --show-toplevel` points at whatever repo the
session happens to be in, and installing from an untrusted `client/` would execute
that repo's packaging code with access to the user's secrets.

**Server selection** is the CLI's concern — it reads the saved config. To target
a local dev stack the user sets `COSTCOMPASS_API_URL`; do not hardcode a URL.

## Service names

The user can type a friendly name or a provider id; the CLI resolves it against
the server (`claude` → `anthropic`, etc.). If a name is unknown the CLI prints
the valid names — pass them back to the user; don't invent a mapping.

## What the JSON fields mean (so you summarize correctly)

- `mtd_usd` — the headline month-to-date figure.
- `burn_rate_7day` — total daily burn over the last 7 days, **including
  subscriptions** (e.g. a Claude Max plan fee dominates it).
- `forecast_usd` — projected end-of-month total.
- `per_provider_burn` (total view only) — per-provider **metered-usage** daily
  burn, excluding subscriptions; a UI min-runway helper. Don't present it as a
  breakdown of `burn_rate_7day` — they intentionally differ.
- `mtd_as_of` — data-freshness timestamp; populated only for a single-provider
  view, `null` for the all-services total. Mention it as "data as of …" when
  present.
- `details` adds `models[]` (`display_name`, `cost_usd`, `surface`); a row with
  `cost_usd == 0` and a `display_value` is a usage-count line, not a charge.

**Subscriptions and metered usage belong together — report the all-in card
total by default.** A card's cost (e.g. Anthropic $96.71) already includes its
plan fee plus usage; that combined number is the unit the user cares about.
Only break out "subscription vs usage" (e.g. $96.67 plan + $0.04 usage) when the
user **explicitly** asks for that split, or asks specifically about
subscriptions. Don't volunteer the decomposition on a plain "what's my spend"
or "which costs most" — it's a drill-down, not the default framing.

## Refresh

Refresh pulls fresh usage from providers. When `ready.refresh` is true the
password resolves from the credential store, env, or config file, so **you can
run it directly** — no prompt, nothing to type:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd refresh --vault --json          # every service
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd <service> refresh --vault --json # one service
```

`--vault` states the intent; the password is never taken from argv. If
`ready.refresh` is false, don't run these — give the vault-password fix above.

A refresh reports a per-provider outcome list plus the refreshed MTD; a card
shown as `skipped — no-days-need-fetching` is the normal 10-minute debounce, not
an error. After a refresh, report which providers ingested events and the new
MTD.
