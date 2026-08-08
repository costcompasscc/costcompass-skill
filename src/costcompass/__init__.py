"""CostCompass command-line client."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _package_version

try:
    from .githash import GIT_SHA
except ImportError:  # pragma: no cover - the file is generated and committed
    # githash.py is a build artifact, so a tree can be missing it — and the
    # whole package importing is too much to lose over a diagnostic string.
    # Degrading here means `--version` omits the commit rather than the CLI
    # refusing to start; `gen-build-files.sh --check`, which runs first in
    # run-tests.sh, is what reports the missing file in terms a reader can act
    # on.
    GIT_SHA = ""

DISTRIBUTION = "costcompass-cli"


def package_version() -> str:
    """The installed distribution's version, or ``"unknown"``.

    Read from package metadata rather than a second string in the source: the
    version is authored once, in ``pyproject.toml``. A source tree that was
    never installed has no metadata to read, and ``--version`` failing is a
    worse outcome than ``--version`` admitting it does not know.
    """
    try:
        return _package_version(DISTRIBUTION)
    except PackageNotFoundError:
        return "unknown"


def build_id() -> str:
    """Version plus the commit it was generated from, e.g. ``1.1.0 (git abc123)``.

    The version alone does not identify a build. ``bin/costcompass`` keys its
    venv on a digest of the code precisely because the version need not move
    when the code does, so two different builds can both call themselves
    1.1.0 — which is the case worth telling apart when a relay looks stale.

    ``GIT_SHA`` is empty when the build was generated outside a git tree; the
    suffix is then omitted rather than printed hollow.
    """
    if not GIT_SHA:
        return package_version()
    return f"{package_version()} (git {GIT_SHA})"


def user_agent() -> str:
    """``User-Agent`` for every request the CLI makes to CostCompass services.

    Only for the CLI -> App Server / broker / oauth-broker hop. The broker sets
    its own agent on the provider hop and rejects a caller-supplied one in a
    forwarded request, so this must never reach the forwarded headers.

    ``build_id`` already renders the sha as a parenthesised suffix, which is
    exactly RFC 9110's ``product/version (comment)``.
    """
    return f"{DISTRIBUTION}/{build_id()}"
