#!/usr/bin/env bash
#
# Cut a release of the plugin.
#
# This repo is its own marketplace, so a push to `main` publishes. What decides
# whether an *already-installed* plugin updates is `.claude-plugin/plugin.json`'s
# version — it names the install cache directory. Ship code without moving it and
# existing users keep running what they already have, silently and indefinitely.
#
# That is not a hypothetical: 13 commits landed at 1.0.0 before this script
# existed. Bumping was an unenforced habit, so this replaces the habit with one
# command that cannot skip the bump, the lockfile, the derived files, or the
# tests.
#
#   release.sh 1.1.0         bump, regenerate, test, then confirm before publishing
#   release.sh 1.1.0 --yes   same, without the confirmation prompt
#
# The version is authored in `pyproject.toml` and nowhere else; everything
# downstream is derived by gen-build-files.sh. Never hand-edit it — go through
# this script so the lockfile, the derived files and the tag move together.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

die() { echo "release: $*" >&2; exit 1; }

# Files this script rewrites, directly or through gen-build-files.sh. Named once
# so the commit, the diff preview and the rollback hint cannot disagree.
TOUCHED=(
    pyproject.toml
    uv.lock
    VERSION
    requirements.txt
    .claude-plugin/plugin.json
)

# ---------------------------------------------------------------- arguments

VERSION=""
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --yes) ASSUME_YES=1 ;;
        -*)    die "unknown option: $arg" ;;
        *)     [[ -n "$VERSION" ]] && die "unexpected extra argument: $arg"
               VERSION="$arg" ;;
    esac
done

[[ -n "$VERSION" ]] || die "usage: release.sh <version> [--yes]   (e.g. release.sh 1.1.0)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "version must be MAJOR.MINOR.PATCH, got: $VERSION"

command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"

CURRENT="$(uv run --quiet python -c \
    "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")"

[[ "$VERSION" != "$CURRENT" ]] || die "already at $CURRENT — nothing to release"

TAG="v$VERSION"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"

# ------------------------------------------------------------ preconditions
#
# A release must be reproducible from its tag, so it is cut from a clean tree on
# main that is not behind the remote. Each check names what is wrong rather than
# reporting a generic refusal.

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$BRANCH" == "main" ]] || die "releases are cut from main; you are on $BRANCH"

[[ -z "$(git status --porcelain)" ]] \
    || die "working tree is dirty — commit or stash first (git status)"

echo "release: fetching origin…"
git fetch --quiet origin main

BEHIND="$(git rev-list --count HEAD..origin/main)"
[[ "$BEHIND" -eq 0 ]] || die "local main is $BEHIND commit(s) behind origin/main — pull first"

AHEAD="$(git rev-list --count origin/main..HEAD)"

# ------------------------------------------------------------------- bump
#
# Substitute the version line in place rather than re-serializing the TOML: a
# round-trip would reformat a hand-authored file and turn every future edit into
# a diff against this script's taste. Same reasoning as gen-build-files.sh's
# treatment of plugin.json.

echo "release: $CURRENT -> $VERSION"

uv run --quiet python - "$VERSION" <<'PY'
import pathlib, re, sys

version = sys.argv[1]
path = pathlib.Path("pyproject.toml")
text = path.read_text()
# Anchored to the line start so a dependency's own version pin can never match.
stamped, n = re.subn(
    r'(?m)^(version\s*=\s*)"[^"]*"', r'\g<1>"%s"' % version, text, count=1
)
if n != 1:
    raise SystemExit("release: no top-level version field found in pyproject.toml")
path.write_text(stamped)
PY

rollback_hint() {
    echo >&2
    echo "release: restore the tree with:" >&2
    echo "         git checkout -- ${TOUCHED[*]}" >&2
}
trap 'rollback_hint' ERR

# uv.lock records the project's own version, and gen-build-files.sh exports
# requirements.txt from it — a stale lock would fail the --check gate below.
echo "release: relocking…"
uv lock --quiet

# Derives VERSION, requirements.txt, and plugin.json's version from the above.
scripts/gen-build-files.sh

echo "release: running tests…"
./run-tests.sh

trap - ERR

# ----------------------------------------------------------------- confirm

echo
git --no-pager diff --stat -- "${TOUCHED[@]}"
echo
echo "release: $TAG  ($AHEAD unreleased commit(s) will publish with it)"

if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "Commit, tag $TAG, and push? [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        echo "release: aborted; the tree still holds the bump."
        rollback_hint
        exit 1
    fi
fi

# ----------------------------------------------------------------- publish

git add -- "${TOUCHED[@]}"
git commit --quiet -m "Release $TAG"
git tag -a "$TAG" -m "$TAG"
git push --quiet origin main
git push --quiet origin "$TAG"

echo "release: published $TAG"
