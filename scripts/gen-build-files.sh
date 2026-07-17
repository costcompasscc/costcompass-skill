#!/usr/bin/env bash
#
# Generate the plugin's derived build files from the CLI's package metadata.
#
# `requirements.txt` and `VERSION` are build artifacts that happen to be
# committed: the plugin is installed by copying this repo's root into a
# versioned cache, with no uv and no lockfile resolution at install time, so
# `bin/costcompass` needs a flat hash-pinned dependency list sitting next to it.
# Edit `pyproject.toml` / `uv.lock` and re-run this script. Never hand-edit
# `requirements.txt` or `VERSION`.
#
#   gen-build-files.sh            regenerate
#   gen-build-files.sh --check    fail if stale (used by run-tests.sh)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

command -v uv >/dev/null || { echo "gen-build-files: uv is required" >&2; exit 1; }

# Build into a staging tree, then either diff it or move it into place. Staging
# keeps --check honest: it compares against a freshly generated copy rather than
# trusting whatever is already committed.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Dependencies, hash-pinned from the lockfile the CLI is tested against. The
# project itself is excluded; the wrapper installs it from the repo root.
#
# Redirect rather than `-o`: uv records its own argv in the file header, so an
# output path would embed this run's temp directory and the --check diff would
# report drift on every invocation.
( cd "$ROOT" && uv export \
    --format requirements-txt \
    --no-dev \
    --no-emit-project \
    --quiet \
    > "$STAGE/requirements.txt" )

# Version stamp: the wrapper reports this while bootstrapping. Its venv is keyed
# on a digest of the actual code, not on this string, so a stale VERSION is
# cosmetic rather than a correctness bug — but it is still the number a user
# sees, so it tracks pyproject.toml.
uv run --quiet --project "$ROOT" python -c "
import pathlib, tomllib
p = tomllib.loads(pathlib.Path('$ROOT/pyproject.toml').read_text())
pathlib.Path('$STAGE/VERSION').write_text(p['project']['version'] + '\n')
"

if [[ $CHECK -eq 1 ]]; then
    status=0
    for f in requirements.txt VERSION; do
        diff -q "$STAGE/$f" "$ROOT/$f" >/dev/null 2>&1 || status=1
    done
    if [[ $status -ne 0 ]]; then
        echo "gen-build-files: derived files are stale — they have drifted from pyproject.toml/uv.lock." >&2
        echo "                 Run: scripts/gen-build-files.sh" >&2
        exit 1
    fi
    echo "gen-build-files: requirements.txt and VERSION are in sync"
    exit 0
fi

for f in requirements.txt VERSION; do
    cp "$STAGE/$f" "$ROOT/$f"
done

echo "gen-build-files: regenerated $(tr -d '\n' < "$ROOT/VERSION")"
