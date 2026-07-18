---
description: Refresh CostCompass usage from providers, then report the new spend.
argument-hint: "[service]"
allowed-tools: Bash
---

Use the `costcompass:spend` skill to refresh provider usage, then report the
refreshed month-to-date spend.

If `$ARGUMENTS` names a service, refresh only that service; otherwise refresh
everything.

Refresh needs the vault password, so follow the skill's refresh guidance to the
letter (the skill loads it from its reference file) — in particular, never pass
the password as an argument and never ask the user to paste it into the chat.
