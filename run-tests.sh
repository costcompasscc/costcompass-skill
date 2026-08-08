#!/usr/bin/env bash
#
# Test entry point for the CostCompass plugin repo.
#
# Hermetic: httpx MockTransport, no DB, no network, no live stack. Run in the
# repo's own uv-managed venv; `uv run` auto-syncs deps on first use.
#
# The CostCompass monorepo calls this script through its `client/plugin` symlink,
# so the two stay one implementation rather than two lists of test commands.
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv not found on PATH — install uv (https://docs.astral.sh/uv/)" >&2
    exit 2
fi

# requirements.txt, VERSION and githash.py are derived from pyproject.toml/uv.lock
# and the current commit. This is what keeps a dependency bump from shipping a
# plugin whose pinned install list no longer matches the code it was tested
# against.
#
# It runs before pytest rather than after so that a missing generated file is
# reported as what it is. `import costcompass` reads githash.py, so a tree
# without it fails collection with a ModuleNotFoundError that says nothing about
# how to fix it — while this check names the file and prints the command.
scripts/gen-build-files.sh --check

uv run --quiet pytest -q
