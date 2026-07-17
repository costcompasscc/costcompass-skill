"""OS credential-store access for the two secrets this CLI needs.

Mirrors ``SecretsStore`` in the macOS app (``../costcompass/client/macos/
CostCompassKit/Sources/CostCompassKit/Storage/SecretsStore.swift``)
so both clients describe the same two items the same way — but the two use
**separate services**, so each stores its own copy and neither can silently
change the other's credentials.

The backend is whatever ``keyring`` resolves for the platform: Keychain on
macOS, Credential Manager on Windows, Secret Service on Linux. Linux has many
implementations; ``keyring``'s entry-point backend system is how alternatives
(KWallet, keyrings.alt, …) plug in without changes here.

A machine with no usable backend (a headless Linux box, say) is not an error:
the store simply reports every secret as absent, and the caller falls back to
the environment or the config file.

Never log a secret value. Read failures carry the backend's message only.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

# Deliberately not the menu-bar app's "cc.costcompass.menubar": the CLI owns
# its own credentials, so rotating or clearing one client never disturbs the
# other.
SERVICE = "cc.costcompass.cli"


class SecretItem(Enum):
    """A secret this CLI persists outside its config file."""

    API_KEY = "api-key"
    VAULT_PASSWORD = "vault-password"


class SecretsStore(Protocol):
    """Storage for secrets that must never reach the config file."""

    def read(self, item: SecretItem) -> str | None: ...

    def write(self, item: SecretItem, value: str) -> None: ...

    def delete(self, item: SecretItem) -> None: ...

    @property
    def available(self) -> bool: ...


class NullStore:
    """Stand-in when the platform exposes no usable credential store."""

    available = False

    def read(self, item: SecretItem) -> str | None:
        return None

    def write(self, item: SecretItem, value: str) -> None:
        raise SecretsUnavailable("No OS credential store is available on this machine.")

    def delete(self, item: SecretItem) -> None:
        return None


class SecretsUnavailable(Exception):
    """Raised when a write is attempted with no usable backend."""


class KeyringStore:
    """`keyring`-backed store — the real implementation on all platforms."""

    available = True

    def __init__(self, backend=None) -> None:
        # Injectable so tests exercise this class rather than a stub of it.
        if backend is None:
            import keyring

            backend = keyring
        self._kr = backend

    def read(self, item: SecretItem) -> str | None:
        try:
            return self._kr.get_password(SERVICE, item.value)
        except Exception:
            # A locked, broken, or absent backend means "no secret here", not a
            # crash — the caller still has the env var and config file to try.
            return None

    def write(self, item: SecretItem, value: str) -> None:
        try:
            self._kr.set_password(SERVICE, item.value, value)
        except Exception as exc:
            raise SecretsUnavailable(
                f"Could not write to the credential store: {exc}"
            ) from exc

    def delete(self, item: SecretItem) -> None:
        try:
            self._kr.delete_password(SERVICE, item.value)
        except Exception:
            return None


def store() -> SecretsStore:
    """The credential store for this machine, or a NullStore if there is none."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except Exception:
        return NullStore()
    try:
        if isinstance(keyring.get_keyring(), FailKeyring):
            return NullStore()
    except Exception:
        return NullStore()
    return KeyringStore(keyring)
