"""CLI configuration: where the two secrets and the server URL come from.

**The API key is never stored in the config file.** It lives in the OS
credential store, or in ``COSTCOMPASS_API_KEY``. The config file holds the
non-secret ``api_url``, and — as a documented last resort — ``vault_password``.

Resolution order is most-hardened-first, so a value in the OS credential store
is preferred over one that is merely an environment variable or plaintext on
disk. A consequence worth knowing: exporting ``COSTCOMPASS_API_KEY`` does *not*
override a stored key. Every resolver therefore returns the source alongside
the value, and ``costcompass auth status`` reports it, so "which one am I
actually using?" is always answerable.

    API key         credential store -> COSTCOMPASS_API_KEY
    Vault password  credential store -> COSTCOMPASS_VAULT_PASSWORD -> config file

The config file lives at ``$XDG_CONFIG_HOME/costcompass/config.toml``
(falling back to ``~/.config``) and is written ``0600``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import secrets

DEFAULT_API_URL = "https://costcompass.cc/api/v1"
ENV_API_KEY = "COSTCOMPASS_API_KEY"
ENV_API_URL = "COSTCOMPASS_API_URL"
ENV_VAULT_PASSWORD = "COSTCOMPASS_VAULT_PASSWORD"

# Source labels, shared with `auth status --json` so the CLI and the skill name
# each origin identically.
SOURCE_STORE = "credential-store"
SOURCE_ENV = "env"
SOURCE_FILE = "config-file"


class ConfigError(Exception):
    """Raised when required configuration is missing."""


def config_path() -> Path:
    """Location of the config file, honoring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "costcompass" / "config.toml"


@dataclass
class Resolved:
    """A secret plus where it came from (``None`` when it was not found)."""

    value: str | None
    source: str | None

    def __bool__(self) -> bool:
        return bool(self.value)


@dataclass
class Config:
    api_key: str | None
    api_url: str
    # Where api_key came from; reporting metadata, not needed to use the Config.
    api_key_source: str | None = None

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "No API key configured. Run `costcompass auth login`, or set "
                f"{ENV_API_KEY}."
            )
        return self.api_key


def _read_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        # A corrupt or unreadable config must not take down a command whose
        # secrets resolve from the store or the environment anyway.
        return {}


def resolve_api_key(store: secrets.SecretsStore | None = None) -> Resolved:
    """Credential store first, then the environment. Never the config file."""
    store = store if store is not None else secrets.store()
    stored = store.read(secrets.SecretItem.API_KEY)
    if stored:
        return Resolved(stored, SOURCE_STORE)
    env = os.environ.get(ENV_API_KEY)
    if env:
        return Resolved(env, SOURCE_ENV)
    return Resolved(None, None)


def resolve_vault_password(store: secrets.SecretsStore | None = None) -> Resolved:
    """Credential store, then the environment, then the config file."""
    store = store if store is not None else secrets.store()
    stored = store.read(secrets.SecretItem.VAULT_PASSWORD)
    if stored:
        return Resolved(stored, SOURCE_STORE)
    env = os.environ.get(ENV_VAULT_PASSWORD)
    if env:
        return Resolved(env, SOURCE_ENV)
    from_file = _read_file(config_path()).get("vault_password")
    if from_file:
        return Resolved(str(from_file), SOURCE_FILE)
    return Resolved(None, None)


def resolve_api_url() -> str:
    """The server URL: environment, then config file, then the default.

    Separate from ``load`` so a caller that only needs the URL does not touch
    the credential store (which can prompt the user for access).
    """
    data = _read_file(config_path())
    url = os.environ.get(ENV_API_URL) or data.get("api_url") or DEFAULT_API_URL
    return str(url).rstrip("/")


def load(store: secrets.SecretsStore | None = None) -> Config:
    """Build a Config from the credential store, env, and file."""
    key = resolve_api_key(store)
    return Config(
        api_key=key.value,
        api_key_source=key.source,
        api_url=resolve_api_url(),
    )


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_api_url(api_url: str) -> Path:
    """Persist the (non-secret) server URL, preserving any other settings."""
    data = _read_file(config_path())
    data["api_url"] = api_url.rstrip("/")
    return _write(data)


def _write(data: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k} = {_toml_str(str(v))}\n" for k, v in sorted(data.items())]
    # O_CREAT with mode 0o600: the file may carry vault_password, so it must
    # never be even briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(lines))
    os.chmod(path, 0o600)
    return path
