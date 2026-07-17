#!/usr/bin/env bash
#
# Test entry point for the CostCompass plugin repo.
#
# Hermetic: httpx MockTransport, no DB, no network, no live stack. Run in the
# repo's own uv-managed venv; `uv run` auto-syncs deps on first use.
#
# The CostCompass monorepo calls this script through its `cli/plugin` symlink,
# so the two stay one implementation rather than two lists of test commands.
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv not found on PATH — install uv (https://docs.astral.sh/uv/)" >&2
    exit 2
fi

uv run --quiet pytest -q

# requirements.txt and VERSION are derived from pyproject.toml/uv.lock. This is
# what keeps a dependency bump from shipping a plugin whose pinned install list
# no longer matches the code it was tested against.
scripts/gen-build-files.sh --check
