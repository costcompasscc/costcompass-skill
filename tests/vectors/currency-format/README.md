# Currency-format vectors (`test-vectors/currency-format/`)

The authored contract for how a money figure is spelled, asserted by **both**
renderers of the same report: the web page (`Intl.NumberFormat` via
`frontend/src/lib/format.ts`) and the PDF (`app/core/currency_format.py`).

Read [`doc/design/multi-currency.md`](../../doc/design/multi-currency.md) §6.1
and §6.2 first. §6.2 requires the page and the PDF of one month to look like one
document, because people keep PDFs.

## What is in here

```
cases.json   <- AUTHORED. {locale, currency, amount, expected, why} per case.
```

Every `expected` is written in `\uXXXX` escape form on purpose. U+00A0, U+202F,
U+2212 and the bidi marks (U+200E, U+200F, U+061C) are invisible in a diff, and
an invisible character in a golden file is a review hazard — the first person to
"tidy" one away would break the pin silently. `README.md` is the only other file
here.

## How the expectations were arrived at, stated plainly

They were **not** copied out of a passing run of either implementation. Each
case's value was read from the reference engine — `Intl.NumberFormat` on the
pinned Node/ICU — and reviewed by hand against §6.1's table before being
committed, which is what "authored" means here. The `why` field records what the
case is for; a case whose `why` cannot say what it pins does not belong here.

There is deliberately **no update flag**. A corpus with a regeneration switch is
a corpus that gets regenerated the first time it blocks someone, and then it
agrees with whatever the code now does instead of holding a contract. If a case
here fails, read the diff — see *When a case fails* below.

## Why the PDF does not use a CLDR library

The first plan was Babel — CLDR in Python — and it was measured before it was
built. Against the pinned runtime's ICU (Node 22.22 / ICU 78.2 / CLDR 48), Babel
2.17 and 2.18 disagreed on **16 of the 53 supported locales**:

| Class | Example | Cause |
| --- | --- | --- |
| Currency spacing absent | `ZAR2,900.00` instead of `ZAR\u00a02,900.00` | CLDR `currencySpacing` is not implemented in Babel at all |
| Grouping rule absent | `1.234,50` instead of `1234,50` (`es-ES`, `it-IT`, `pt-PT`, `pl-PL`, `hu-HU`, `fi-FI`, `nb-NO`, `sv-SE`) | `minimumGroupingDigits` is not in Babel's data or its formatter |
| Rounding mode | `¥1,234` instead of `¥1,235` | Babel rounds half-to-even, `Intl` round-half-expands |
| Digit shapes unreachable | Latin digits where `ar-SA`/`bn-BD` need `\u0661`/`\u09E7` | `numbering_system` switches separators, not glyphs |
| Bundled-CLDR drift | `de-CH`/`fr-CH` separators, `es-AR`/`es-CO`, `IDR`/`HUF`/`COP` fraction digits | Babel's CLDR version ≠ ICU 78's |

A wrapper for the first three cut the mismatch from 47% to 11% and then stalled:
the rest is missing rules and older data, not arguments. So the PDF's formatting
is **probed from ICU rather than re-derived from a specification** —
`scripts/lib/currency-format-table.mjs` → `backend/app/core/currency_format_data.json`
→ `backend/app/core/currency_format.py`, which decides nothing itself. Digit
shapes, separators, grouping sizes, symbol placement, the U+00A0 currency space,
the trailing minus in `de-CH` and the bidi marks in `ar-SA` all arrive as data,
so there is no second implementation of them to drift.

The corpus in this directory is what keeps that honest: same inputs, same
outputs, asserted on both sides.

## The table, and how it is kept current

`backend/app/core/currency_format_data.json` describes exactly one ICU. Its
`_provenance` records which.

```bash
make gen-currency-format-data            # deliberately re-probe and rewrite
cd frontend && npx vitest run src/test/currency-format-data.test.ts   # drift guard
```

The guard rebuilds the table in memory and compares it to the committed file,
and it runs inside the frontend suite, which `run-tests.sh` already executes. It
is deliberately *not* in `make drift-check`: that group is documented as
offline and stdlib-only, and this one needs Node and an ICU.

**A diff here is a finding, not a rebaseline.** It means the ICU moved. The
questions to answer before re-capturing are: which locales and currencies moved,
does the page really say something different now, and is that a change we want
in the PDF a reader downloads? The same discipline as
`test-vectors/dashboard-payloads/README.md`, for the same reason.

Only the locale **set** is checked on the Python side
(`backend/tests/unit/test_currency_format.py`), against
`SUPPORTED_DISPLAY_LOCALES` — that list is the authority on which locales exist,
and the Makefile reads it from there rather than keeping a second copy.

## When a case fails

| Failure | What it means |
| --- | --- |
| The frontend case fails | This runtime's ICU renders the case differently from the authored expectation. Either the ICU moved (a finding to review, per above) or the page's rule was changed. |
| The Python case fails | The PDF would print a different string than the page for the same figure — the bug this corpus exists for. Fix the formatter, not the expectation. |
| Both fail | The expectation itself is wrong, or the reference engine changed under both. Re-derive it from ICU, review against §6.1, and say in the commit message why the value moved. |

Changing a case is a change to what a reader sees on a page and in their
downloaded PDF. Treat it as a product decision with a record, never as a test
fixture.

## Named gaps

- **One reference ICU, not every browser.** The table is probed from the pinned
  Node/ICU. Chrome, Safari and Firefox ship different ICU versions, so a user on
  an older engine can see a different separator than the PDF prints. That is a
  property of browser-side `Intl` generally, not of this design; what this
  design guarantees is that the PDF matches the pinned reference exactly, and
  that a move in the reference is visible.
- **The vectors cover the currencies and locales the product serves**, plus the
  classes above. The table itself carries all 162 currencies ICU knows and every
  locale in `SUPPORTED_DISPLAY_LOCALES`, but a case is only added for a shape
  worth pinning — the guard is the table, the contract is these cases.
- **Amounts are finite floats.** Storage is binary double precision, so two
  implementations can in principle disagree on a value's shortest decimal
  representation. The half-way cases (`0.015`, `1.005`, `2.675`) are pinned
  precisely because that is where it would show.
- **A figure wider than the rounding context raises on the PDF side.**
  `format_money` quantizes with `Decimal(str(amount))`, which needs a 28-digit
  context: from `1e26` an amount raises `ValueError` in the PDF where the page
  prints the digits (measured; a zero-decimal currency such as JPY reaches it
  later). Pre-existing rather than a property of the probed table — the
  `format_usd` this replaced raised at the same value — and no provider reports
  money that large, but the parity claim stops there and a reader comparing the
  two surfaces should know where.
- **The CLI is wired; the macOS suite still hand-copies.** The CLI
  (`client/plugin`, the sibling repo) reads this same probed table
  (`costcompass/currency_format_data.json`, copied from
  `backend/app/core/`) and asserts every case here
  (`tests/test_currency_format.py`) against its vendored copy of
  `cases.json`, so it cannot drift. The macOS suite
  (`client/macos/…/FormattersTests.swift`) still hand-copies the USD/en-US
  values this corpus inherited from the deleted `money-display-cases.ts`: it
  formats through Foundation's ICU rather than this table, and Foundation's
  symbol set differs from the reference ICU this corpus was probed from (for a
  named gap, `ZAR` renders `R` and `JPY` a fullwidth `￥`), so wiring it
  mechanically is not available. A case changed here still has to be carried
  there by hand.
