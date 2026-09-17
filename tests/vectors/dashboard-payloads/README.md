# Dashboard payload goldens (`test-vectors/dashboard-payloads/`)

The "before" picture of a **single-currency** account, captured ahead of the
multi-currency change so that change can be reviewed against something rather
than against memory — plus, now that the per-currency shape has landed, **one
two-currency scenario** that pins the new contract (design §7.0, §7.2).

Read [`doc/design/multi-currency.md`](../../doc/design/multi-currency.md) §1 and
§1.1 first. §1 promises that a user with one currency sees today's UI unchanged.
This corpus is what that promise is checked against. Every scenario except
`two-currency` is a single-currency account and is part of that evidence;
`two-currency` is the one scenario that exists to be different, so it carries no
§1 claim — do not cite its render files as a single-currency user's screen.

## What this proves, and what it does not

**It is not a byte-identity claim on the wire.** §5 renames `mtd_usd` → `mtd`
for *every* user, single-currency ones included, so the JSON here cannot survive
that change and is not meant to. What the JSON is for is making the rename a
**reviewable diff**: the fixtures record exactly what the API serves today, so
when the shape moves, the move is visible field by field instead of disappearing
into a green suite.

**The render files are where "unchanged" means something.** `web-render.txt`,
`macos-render.txt`, `cli-render.txt` and `report.html` are what a human actually sees. A change to
any of those is a change to what a single-currency user's screen says, and §1
says that must not happen.

Deliberately outside the corpus:

- **Balances.** Sequenced for §11 step 5.
- **CLI JSON and refresh-flow output.** This corpus covers the CLI's text
  renderer only: `format_amount`, `format_breakdown`, `format_details`,
  `format_subscription`, and their separately emitted freshness notes.
  Its separately shaped JSON output and refresh-flow line remain outside this
  compatibility boundary.

Known gaps inside the corpus, named here so they are not mistaken for coverage:

- **WeasyPrint output is not pinned.** `report.html` pins the PDF's markup; the
  PDF bytes get a smoke assertion only (non-empty, starts `%PDF-`). Font
  substitution, pagination and raster layout are unproven, because WeasyPrint
  output is not stable across library versions or installed font sets and a
  golden that fails for those reasons is a golden that gets deleted.
- **`web-render.txt` covers three components in their initial state, not the
  whole dashboard.** It mounts `MtdHero`, the portfolio `Breakdown`, and the
  `Report` page — each non-interactive — and extracts only `.tabular`. It
  therefore excludes `ServiceRail`'s per-card money and balances (the rail
  renders provider-card metadata this corpus does not model), `BreakdownPanel`'s
  selected-provider rows (raw `tabular-nums`, outside the contract by
  construction), `PricingButton`'s rates, and `TrendChart`'s hover-only readout.
  Those are visual and interaction coverage and belong to the visual-baselines
  follow-up; a text fixture rendering them in a state no user sees would read as
  coverage while proving nothing.
- **There are no unmasked visual baselines yet.** The pixel half of the render
  proof is a separate follow-up. `frontend/e2e/responsive/app.spec.ts`'s
  existing baselines mask `.tabular` — every money element — so they prove
  layout and say nothing about the values this corpus is about. They are
  deliberately untouched: they are themselves part of the "before".

## Layout

```
clock.json                <- authored: the one frozen instant every scenario uses
coverage.json             <- authored: the scenarios × surfaces that MUST exist
<scenario>/
  scenario.json           <- authored: what is seeded, and the request parameters
  summary.json            <- captured: GET /api/v1/dashboard/summary
  breakdown.json          <- captured: GET /api/v1/dashboard/breakdown
  trend.json              <- captured: GET /api/v1/dashboard/trend?days=30
  report.json             <- captured: GET /api/v1/report
  report.html             <- captured: report_pdf.render_html()
  web-render.txt          <- captured: the web components' rendered values
  macos-render.txt        <- captured: MenuBarTitle over the decoded summary
  cli-render.txt          <- captured: CLI text renderer over the decoded payloads
```

**Authored vs captured is the distinction the whole policy rests on.** An
authored file is written by a person and says what the scenario *is*. A captured
file is what the code produced when that scenario was run, and is the evidence.
The two are never edited by the same act.

## Scenarios

| Scenario | State | From |
| --- | --- | --- |
| `empty` | No rows at all — the degenerate path | §1.1 |
| `metered-usage` | Two providers, several models, current-month and prior-month/7-day data | added |
| `subscriptions-only` | Money with no usage events | §1.1 |
| `stale-cards` | `stale_cards` populated, `newest_fetched_at` old | §1.1 |
| `incomplete-cards` | `incomplete_card_count` non-zero | §1.1 |
| `provider-filtered` | `?provider=…` — a different code path from the portfolio view | §1.1 |
| `two-currency` | Two currencies, one card holding both, `primary_currency` not the larger spend | §7.0, §7.2 |

## Request scope is per surface, not per scenario

Each `scenario.json` carries a `request` map keyed by surface file. A surface
named there is called with those parameters; a surface absent is called with
none. The capture code reads that map rather than applying one request to
everything, and it **refuses** a scope on a surface whose route cannot take one.

This exists because the scope genuinely differs by surface, and a directory name
implies otherwise. `provider-filtered` is the case:

| Surface | Scope | Why |
| --- | --- | --- |
| `summary.json` | `?provider=anthropic&instance_key=` | The route accepts the filter |
| `trend.json` | `?provider=anthropic&instance_key=` | The route accepts the filter |
| `breakdown.json` | **portfolio** | `/dashboard/breakdown` takes no filter |
| `report.json`, `report.html` | **portfolio** | `/report` takes no filter |
| `web-render.txt` | mixed, and correctly so | A filtered hero, a portfolio breakdown, and the report — three separately rendered surfaces, which is what a user with a provider selected actually sees |

So **`provider-filtered/report.json` is not evidence of filtered behaviour**, and
neither is its breakdown. Do not cite them as such. The scenario is still worth
having: `per_provider_burn` is `null` under a filter and populated without one,
and that difference is what §1.1 means by "a different code path".

`metered-usage` is not in §1.1's list. It was added because that list contains no
ordinary path exercising summary, per-provider burn, trend, model breakdown, card
ordering and report reconciliation *together*, so a regression in any of them
could pass every state §1.1 names.

`stale-cards` and `incomplete-cards` are separate because `stale_cards` and
`incomplete_card_count` are computed independently and rendered independently; a
combined scenario can hide a regression in either.

`two-currency` is the one scenario that is not a single-currency account. It
exists because the per-currency report shape (design §7.0) and the per-currency
card figures (§7.2) are contracts a USD-only corpus cannot pin: every
single-currency scenario is satisfied by a renderer that ignores currency
altogether. Its three discriminating choices are authored in its
`scenario.json` and restated here so they are not mistaken for arbitrary seeds:

- **USD is `primary_currency` while LKR is the larger spend.** §4.2's served
  order is therefore `["USD", "LKR"]`, which is neither alphabetical nor
  size-ranked (`LKR` leads under both). A regression to either ordering is a
  visible diff rather than a plausible-looking one.
- **Anthropic holds money in both currencies** and OpenAI holds LKR only. The
  first exercises a card's two per-currency header lines (§7.2); the second
  gives the LKR ring two services, so its percentages are a real 73.6/26.4 split
  instead of the degenerate 100% a one-service ring yields under any denominator.
- **No balance is seeded**, so `balance_total` is `null` throughout — balances
  are sequenced for §11 step 5 and this scenario does not imply otherwise.

What a golden can assert about it that a byte-equality check cannot — and what
`backend/tests/unit/test_two_currency_golden.py` therefore does — is the order
above, each ring's percentages being shares of *that ring*, and the absence of a
figure summed across the two.

`coverage.json` lists these, and the surfaces each must carry. A scenario
directory that goes missing fails a test rather than reading as "we did not need
that one".

## Determinism

Everything is pinned to one instant, `clock.json`'s `2026-04-15T12:00:00Z` —
mid-month, so `days_remaining`, the trailing-7-day burn window and the
previous-month window are all non-degenerate.

Only the clocks need pinning, and there are two of them.

Not the locale, despite what §1.1's "clock, locale and user settings frozen"
suggests. `report_pdf.month_title` formats the report's month with
`strftime('%B')`, which really is locale-sensitive — but CPython starts with
`LC_TIME=C` regardless of `LANG`/`LC_ALL` and only ever leaves it if something
calls `locale.setlocale`, which nothing in this repository does. Re-capturing
under `LC_ALL=fr_FR.UTF-8` produces byte-identical output, verified.

**That is a property of the environment, not of the code, so it is asserted
rather than claimed here**:
`test_report_month_name_does_not_follow_the_process_locale` fails the day
anything puts the process into a non-`C` `LC_TIME`, and says what to do about it
— decide the report's language contract, do not re-capture the goldens.

Pinning the locale inside the capture was tried and removed. It would have kept
the corpus English while production drifted, which hides the regression it
appears to guard against.

- **Server.** The tests monkeypatch the module-bound `_utc` in
  `app.services.query_service` and `app.services.report_service`, then drive the
  real routes through `TestClient`. The service functions already take a `now`;
  the routes do not pass one, which is why the patch is at the module symbol
  rather than at the call.
- **Browser.** `frontend/src/lib/format.ts` reads the clock twice —
  `monthLabel(now = new Date())`, called argument-less from `MtdHero`, and
  `formatRelativeTime`'s `Date.now()`, reached from `ServiceRail`. Neither is
  reachable from the server freeze. The web suites use `vi.setSystemTime()` and
  the Playwright scenarios use `page.clock.install()`, both at the same instant.

A server-side recapture check cannot see a browser clock leak. If you are
changing how time enters either side, that is the failure mode to look for.

## The extraction contract for `web-render.txt`

> The `textContent` of every element carrying the `.tabular` class, in document
> order, one per line, each prefixed with `<surface>/<ordinal>`.

`.tabular` is the class the existing responsive baseline masks
(`frontend/e2e/responsive/app.spec.ts`), so unmasking for the value scenarios and
extracting for this file are one mechanism, not two. The document-order ordinal
means a changed line identifies its **location** as well as its string — a value
that moved between two elements does not read as unchanged.

`macos-render.txt` follows the same one-value-per-line, ordinal-prefixed shape so
the two files diff alike.

`cli-render.txt` follows that shape too. It contains portfolio headline and
breakdown commands, their separately emitted freshness notes, a details command
only where the scenario captures a provider-scoped summary, and standalone
subscription output through its distinct renderer. The canonical backend suite
captures it and the sibling CLI suite asserts its vendored copy.

## Updating this corpus

**There is deliberately no one-command regeneration, and adding one would defeat
the corpus.**

`test-vectors/subscription-proration/` has `UPDATE_VECTORS=1` and should: it pins
an *algorithm*, its expectations are derived, and regenerating them is how you
adopt a corrected calculation. This corpus is the opposite kind of thing. It is a
historical compatibility boundary, and its captured outputs *are* the artefact.
A flag that rewrites all of them in one go destroys the only evidence the
boundary has — which is precisely the failure §1.1 anticipates when it says
snapshot tests get deleted the first time they block someone.

So:

**A failing test here is a finding, not a chore.** Read the diff before doing
anything else. In every case it is telling you that a payload or a rendered value
changed, and the question is whether that change was intended.

**Changing an input** — a new scenario, a different seeded amount — is an edit to
`scenario.json` and `coverage.json`, which are authored. Re-capture that
scenario's files, and the diff you get is the answer to "what does this input
now produce".

**One capture invocation updates one named scenario.** Each suite has its own
variable and each takes exactly one name, validated against `coverage.json`:

```bash
cd backend  && CAPTURE_API_GOLDENS=metered-usage   .venv/bin/pytest tests/integration/test_dashboard_payload_goldens.py
CAPTURE_CLI_GOLDENS=metered-usage ./run-tests.sh --integration-only --test tests/integration/test_cli_render_goldens.py
cd frontend && CAPTURE_WEB_GOLDENS=metered-usage   npx vitest run src/test/payload-goldens.test.tsx
cd client/macos/CostCompassKit && CAPTURE_MACOS_GOLDENS=metered-usage swift test --filter DashboardPayloadGoldensTests
```

A list is refused, and so is a name the manifest does not carry. A loop over six
invocations is of course still possible, and that is fine — it is six deliberate
capture operations, and each one had to be named. What it is not is a single flag
that turns one confusing failure into a bulk rebaseline, which is the shape this
policy exists to prevent. Three separate variables for the same reason: one
shared flag would rewrite every surface of a scenario in a single run.

**Changing an output on purpose** — you meant to change the wire shape or the
rendering — is a deliberate act that needs a deliberate record. Re-capture the
affected scenarios only, and state in the commit message:

1. the product decision that made the change correct;
2. which scenarios and which surfaces moved;
3. **why §1 still holds** — i.e. what a single-currency user now sees, and why
   that is not the regression §1 forbids.

Point 3 is not ceremony. It is the whole reason the corpus exists, and a
regeneration whose commit message cannot answer it is a regeneration that should
not have happened.

**Never re-capture the whole corpus to make a suite green.** If several
scenarios fail at once, that is more likely one real change than several, and
recapturing all of them converts the evidence into agreement with whatever the
code now does.

`--update-snapshots` on the responsive suite regenerates the **PNG** baselines
and is normal after an intentional layout change. It is capture tooling, not
authorization to rebaseline the values in this directory, and it does not touch
anything here. It is also the reason the text goldens are the primary proof
rather than the pixels: one flag rebaselines every screenshot, and there is
deliberately no equivalent for these files.

When the rule is unclear, design §1.1 is what this corpus answers to.
