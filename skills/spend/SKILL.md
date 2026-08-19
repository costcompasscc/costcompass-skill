---
name: spend
description: Report CostCompass month-to-date spend by driving the bundled `costcompass` CLI. Use when asked "what's my spend", "how much have I spent", "my MTD", "month-to-date", "AI/cloud spend", "costcompass mtd", spend for one provider (e.g. "how much on claude / openai / google this month"), a per-model cost breakdown, a spend forecast / burn rate, or to "refresh my usage" before reporting.
user-invocable: true
allowed-tools: Bash
---

# CostCompass spend from the terminal

Answer the user's spend question by running the **bundled `costcompass` CLI** and
presenting a concise summary — not raw JSON. Always invoke it by full path:

```
${CLAUDE_PLUGIN_ROOT}/bin/costcompass
```

Never call a bare `costcompass` — that picks up whatever is on PATH, which may
be a different version than this skill was written against.

## Hot path — check auth and read spend in one call

For a bare "what's my spend", run **one** Bash call that checks readiness and
reads the total together — one round-trip, not two:

```bash
CC="${CLAUDE_PLUGIN_ROOT}/bin/costcompass"
"$CC" auth status --json || true
echo '--- mtd ---'
"$CC" mtd --json || true
```

The `|| true` on **both** lines is not cargo-cult. `auth status` deliberately
exits 1 when `ready.spend` is false, and `mtd` exits 1 when there's no usable
key; without the guard the harness renders a red `Error: Exit code 1` above your
reply, telling the user their setup is broken when in fact the command answered
what it was asked. Errors go to stderr and are noise for you; the two JSON
objects on stdout (split by the `--- mtd ---` line) are what you read.

First run also builds the CLI's virtualenv and prints a one-time setup line to
stderr — expected, needs network, a few seconds.

**Read `ready` out of the first JSON — don't re-derive it from the parts:**

```json
{
  "server": "…",
  "identity": "user@example.com",
  "api_key": { "source": "credential-store|env|null", "valid": true },
  "vault": { "source": "credential-store|env|config-file|null", "unlocks": true },
  "ready": { "spend": true, "refresh": true }
}
```

- **`ready.spend` true** → summarize the `mtd` JSON (see Presentation).
- **`ready.spend` false** → you cannot answer any spend question. **Ignore the
  `mtd` output entirely** (it only errored), then load the reference file and
  follow its "Fix: API key" section:

  ```bash
  cat "${CLAUDE_PLUGIN_ROOT}/skills/spend/reference.md"
  ```

- **`ready.refresh` false** → spend still works; only matters if the user asked
  to refresh. If they did, load the reference file and follow "Fix: vault
  password".

## When to load the reference file

`cat "${CLAUDE_PLUGIN_ROOT}/skills/spend/reference.md"` and follow it whenever
the request is **not** a bare total: `ready.spend`/`ready.refresh` false, a
refresh, a single service, a breakdown, a per-model view, or standalone
subscriptions. It carries the setup fixes (with their secret-handling rules),
the drill-down commands, service-name resolution, refresh, and the full field
meanings. For a service or breakdown, chain the matching `mtd …` command after
`auth status --json || true` the same way as above — still one round-trip.

## Presentation

- Summarize; show raw JSON only if the user explicitly asks for it.
- Lead with the headline `mtd_usd`. Add `forecast_usd` (projected end-of-month)
  and `burn_rate_7day` (7-day daily burn, includes subscriptions) when relevant.
- Report the **all-in card total** by default; only break out "subscription vs
  usage" when the user explicitly asks. (Full field meanings in the reference.)
- Format money plainly (`$131.01`). Note "data as of …" when `newest_fetched_at` is set.
- Never expose the API key, the vault password, or internal card UUIDs.
