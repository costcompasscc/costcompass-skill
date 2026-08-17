# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/vault/entries.ts and
# ../costcompass/client/macos/CostCompassKit/Sources/
#   CostCompassKit/Vault/Vault.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""Entry lookup over an already-decrypted vault document.

Resolving ``(provider, instance_key)`` to an entry is one decided rule with
three ports, held equal by the hand-written corpus
``test-vectors/relay/vault-entry-lookup.json`` rather than by any relay acting
as the others' oracle. The decrypt/encrypt underneath this is a separate
concern with its own ports — see `crypto.py`.

Pure functions over a parsed JSON tree: no crypto, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _entry_matches(entry: Any, provider: str, instance_key: str) -> bool:
    """The single (provider, instance_key) matching predicate.

    LOCKSTEP INVARIANT 14 — every site that decides "is this the entry?" goes
    through here, in all three relays. See invariant 14 in
    ``frontend/src/lib/refresh/CLAUDE.md``; pinned by
    ``test-vectors/relay/vault-entry-lookup.json``.

    ``instance_key`` arrives normalized: an absent, ``None`` or ``""`` selector
    is the provider-DEFAULT lookup, spelled ``""`` here.

    On the entry side, a ``metadata.instance_key`` that is absent or ``None``
    means the default slot, so it compares equal to ``""``. A non-``str``,
    non-``None`` value (number, bool, list, dict) is a foreign value the format
    permits but no writer produces: that entry is neither the default card nor
    any named card, so no lookup ever returns it. It is unreachable, not an
    error — the same way any missing entry falls through.
    """
    if not isinstance(entry, dict) or entry.get("provider") != provider:
        return False
    meta = entry.get("metadata")
    ek = meta.get("instance_key") if isinstance(meta, dict) else None
    if ek is None:
        return instance_key == ""
    if not isinstance(ek, str):
        return False
    return ek == instance_key


@dataclass
class Vault:
    """A decrypted vault document plus the params needed to write it back."""

    doc: dict[str, Any]
    p2s: bytes
    p2c: int
    revision: int

    def entry_for(
        self, provider: str, instance_key: str | None = None
    ) -> dict[str, Any] | None:
        """Find the entry for (provider, instance_key); mirrors findEntry."""
        key = instance_key or ""
        for entry in self.doc.get("entries", []):
            if _entry_matches(entry, provider, key):
                return entry
        return None
