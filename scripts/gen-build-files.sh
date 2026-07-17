#!/usr/bin/env bash
#
# Generate the plugin's derived build files from the CLI's package metadata.
#
# `requirements.txt` and `VERSION` are build artifacts that happen to be
# committed: the plugin is installed by copying this repo's root into a
# versioned cache, with no uv and no lockfile resolution at install time, so
# `bin/costcompass` needs a flat hash-pinned dependency list sitting next to it.
# `plugin.json`'s version is the same number wearing a marketplace hat.
# `pyproject.toml` is the one place a version is authored. Edit it (or
# `uv.lock`) and re-run this script. Never hand-edit `requirements.txt`,
# `VERSION`, or `plugin.json`'s version field.
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

# Version stamps, both tracking pyproject.toml:
#
#   VERSION      — what the wrapper reports while bootstrapping. Its venv is
#                  keyed on a digest of the actual code, not on this string, so
#                  a stale VERSION is cosmetic rather than a correctness bug —
#                  but it is still the number a user sees.
#   plugin.json  — what the marketplace installs under, so a stale one is worse
#                  than cosmetic: it names the cache directory, and a bump that
#                  missed it would ship new code at the old version.
#
# plugin.json is hand-authored apart from this one field, so substitute in place
# rather than re-serializing — a json round-trip would reformat the whole file
# and turn every future hand edit into a diff against this script's taste.
uv run --quiet --project "$ROOT" python -c "
import pathlib, re, tomllib
root = pathlib.Path('$ROOT')
stage = pathlib.Path('$STAGE')
version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']

stage.joinpath('VERSION').write_text(version + '\n')

manifest = (root / '.claude-plugin/plugin.json').read_text()
stamped, n = re.subn(r'(\"version\"\s*:\s*)\"[^\"]*\"', r'\g<1>\"%s\"' % version, manifest, count=1)
if n != 1:
    raise SystemExit('gen-build-files: no \"version\" field found in .claude-plugin/plugin.json')
stage.joinpath('plugin.json').write_text(stamped)
"

# Staged name -> path in the repo.
GENERATED=(
    "requirements.txt:requirements.txt"
    "VERSION:VERSION"
    "plugin.json:.claude-plugin/plugin.json"
)

if [[ $CHECK -eq 1 ]]; then
    status=0
    for entry in "${GENERATED[@]}"; do
        diff -q "$STAGE/${entry%%:*}" "$ROOT/${entry#*:}" >/dev/null 2>&1 || status=1
    done
    if [[ $status -ne 0 ]]; then
        echo "gen-build-files: derived files are stale — they have drifted from pyproject.toml/uv.lock." >&2
        echo "                 Run: scripts/gen-build-files.sh" >&2
        exit 1
    fi
    echo "gen-build-files: requirements.txt, VERSION and plugin.json are in sync"
    exit 0
fi

for entry in "${GENERATED[@]}"; do
    cp "$STAGE/${entry%%:*}" "$ROOT/${entry#*:}"
done

echo "gen-build-files: regenerated $(tr -d '\n' < "$ROOT/VERSION")"
