---
name: spend
description: Report CostCompass month-to-date spend by driving the bundled `costcompass` CLI. Use when asked "what's my spend", "how much have I spent", "my MTD", "month-to-date", "AI/cloud spend", "costcompass mtd", spend for one provider (e.g. "how much on claude / openai / google this month"), a per-model cost breakdown, a spend forecast / burn rate, or to "refresh my usage" before reporting.
user-invocable: true
allowed-tools: Bash
---

# CostCompass spend from the terminal

Answer the user's spend question by running the **bundled `costcompass` CLI** and
presenting a concise summary — not raw JSON. The CLI ships with this plugin;
always invoke it by full path:

```
${CLAUDE_PLUGIN_ROOT}/bin/costcompass
```

Never call a bare `costcompass` — that picks up whatever is on PATH, which may
be a different version than this skill was written against.

## Step 1 — check you can actually do the job

**Run this first, before any spend command — including the `|| true`:**

```bash
${CLAUDE_PLUGIN_ROOT}/bin/costcompass auth status --json || true
```

The `|| true` is not cargo-cult. `auth status` deliberately exits 1 when
`ready.spend` is false, so that shell users can write
`costcompass auth status && costcompass mtd`. You are not a shell user — you read
`ready` out of the JSON — and without the guard the harness renders a red
`Error: Exit code 1` above your reply, which tells the user their setup is broken
when in fact nothing failed: the command answered the question it was asked. Keep
the CLI's exit code as it is and swallow it here.

It reports where each secret came from and whether it *works* (the vault check
really decrypts the vault; the key check really calls the server):

```json
{"server": "…", "identity": "user@example.com",
 "api_key": {"source": "credential-store|env|null", "valid": true},
 "vault":   {"source": "credential-store|env|config-file|null", "unlocks": true},
 "ready":   {"spend": true, "refresh": true}}
```

Use `ready` — don't re-derive it from the parts:

- **`ready.spend` false** → you cannot answer any spend question. Stop and give
  the "API key" fix below. Don't run `mtd`; it will only fail.
- **`ready.refresh` false** → spend questions still work. Only raise the "vault
  password" fix if the user actually asked to refresh.

First run also builds the CLI's virtualenv and prints a one-time setup line to
stderr — expected, needs network, a few seconds.

### Fix: API key (`ready.spend` false)

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
and append `--url <base>` to the `auth login` line if `server` is not the default
(e.g. a local dev stack).

**Do not**, in this message:

- **Tell the user to run `auth login` with the `!` prefix.** It cannot work.
  `!` gives the command no TTY, so `getpass` cannot suppress echo and the CLI
  aborts rather than print the key in the clear — correct behaviour, useless
  advice. A hidden prompt needs a real terminal window. This applies to every
  secret-accepting command (`auth login`, `auth vault`).
- Show or quote the `auth status --json` output. `source: null`,
  `valid: false`, and `ready.spend` are diagnostics for *you* — to the user they
  are noise that buries the two things they can actually do.
- Lead with what's broken, a status table, or a field-by-field readout.
- Explain the resolution order, or why the credential store beats the
  environment. They didn't ask.
- Volunteer the vault password — it is a *separate* secret and irrelevant to a
  spend question. Only raise it if they asked to refresh.

Never ask the user to paste the key into the chat, and never echo one back.

### Fix: vault password (`ready.refresh` false)

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

A third option exists — `vault_password = "…"` in
`~/.config/costcompass/config.toml` (mode 0600) — but it is **plaintext on
disk**. Offer it only if they ask for it, and say that plainly when you do.

**Never** ask the user to paste the vault password into the chat, and never
echo, log, or store it yourself. Never pass it as a command-line argument —
`--vault <pw>` is rejected by design, since argv leaks via shell history and
`ps`.

If `auth status` shows `vault.source` as `config-file`, you may mention once
that moving it to the credential store (`auth vault`) is safer. Don't nag.

## Step 2 — read the spend

Always use `--json`, then summarize in plain language.

```bash
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd --json                    # total month-to-date (all services)
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd breakdown --json          # EVERY card ranked (providers + subscriptions); total matches `mtd`
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd <service> --json          # one service (e.g. claude, openai, google)
${CLAUDE_PLUGIN_ROOT}/bin/costcompass mtd <service> details --json  # that service's totals + per-model breakout
```

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

Lead with the headline number, then forecast/burn if relevant. Keep it tight.

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

## Presentation

- Summarize; show raw JSON only if the user explicitly asks for it.
- Format money plainly (`$131.01`). Note the "data as of" time when you have it.
- Don't expose the API key, the vault password, or internal card UUIDs.
