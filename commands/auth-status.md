---
description: Check that CostCompass is fully authenticated and can read your vault.
allowed-tools: Bash
---

Use the `costcompass:spend` skill's readiness check to report whether
CostCompass is set up.

Run its `auth status --json` step and tell the user, plainly:

- which server they're pointed at, and who they're authenticated as
- where each secret is coming from (credential store, environment variable, or
  plaintext config file) — this is the answer to "which one am I actually
  using?"
- whether the vault password actually unlocks the vault, not merely that one is
  configured
- what works right now: reading spend, refreshing, or neither

If anything is missing or rejected, give the matching fix from the skill. Never
print the API key or the vault password.
