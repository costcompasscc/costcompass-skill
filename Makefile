.PHONY: help install test fmt fmt-check fmt-python fmt-python-check fmt-web fmt-web-check

.DEFAULT_GOAL := help

help:  ## Show this list of targets
	@echo "CostCompass CLI (plugin repo) — development Makefile"
	@echo ""
	@echo "Usage: make <target>   (this list is generated from the Makefile itself)"
	@awk 'BEGIN {FS = ":.*##"} \
	     /^##@/ { printf "\n%s:\n", substr($$0, 5); next } \
	     /^[a-zA-Z0-9_-]+:.*##/ { printf "  make %-22s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

##@ Setup
# Two package managers because the tree holds two languages: uv owns the Python
# CLI (the shipped artifact), npm owns prettier and nothing else. No application
# JavaScript exists here and none should — if node_modules/ ever holds something
# the CLI imports, that is the bug, not this target.
install:  ## Install Python (uv) and formatter (npm) dependencies
	@echo "→ installing Python deps (uv)"
	@uv sync --quiet
	@echo "→ installing formatter deps (prettier)"
	@npm ci
	@echo "✓ install complete"

##@ Quality
# No test target beyond this passthrough on purpose: ./run-tests.sh carries the
# generated-file staleness check that has to run before pytest, and the monorepo
# invokes that same script through its client/plugin symlink. A make target that
# shelled straight to pytest would be a second, quieter definition of "the tests"
# that skips the check and drifts from what the monorepo actually runs.
test:  ## Run the test suite (./run-tests.sh)
	@./run-tests.sh

# Formatting mirrors the monorepo's targets of the same names, because this tree
# is the CLI relay implementation and its files sit alongside the browser and
# macOS ones in review. Two formatter families, split the same way: prettier owns
# Markdown and JSON, ruff owns Python. `fmt` rewrites in place; `fmt-check` is the
# read-only variant to run before a commit.
#
# The monorepo's .prettierignore excludes client/plugin/ (this repo, symlinked)
# with the note "formatted by their own repo's tooling" — these targets are that
# tooling. Nothing formats this tree from over there.
fmt: fmt-python fmt-web  ## Format everything (ruff, prettier)

fmt-check: fmt-python-check fmt-web-check  ## Verify formatting without rewriting

# Run ruff through `uv run` rather than a bare `ruff` on PATH. A developer's
# global ruff is whatever they installed last, and `ruff format` output changes
# between releases — so the two can disagree about whether this tree is formatted
# and the gate's verdict stops being reproducible. `uv run` resolves the version
# pinned in pyproject.toml/uv.lock, and installs it on first use rather than
# dying with "command not found" on a fresh checkout.
#
# Ruff runs on its defaults: pyproject.toml carries no [tool.ruff] section, so
# the pinned version is the only thing deciding the output. Adding a section is
# what would make the settings ours; until then, the pin is the whole contract.
fmt-python:  ## Format Python only (ruff format)
	@uv run --quiet ruff format .

fmt-python-check:  ## Check Python formatting only (ruff format --check)
	@uv run --quiet ruff format --check .

# Run the locally installed prettier directly rather than through `npx`. With no
# node_modules, `npx prettier` does not fail loudly — it silently fetches an
# unpinned prettier from the registry, so the formatter version deciding the gate
# is whatever the registry served that minute. The guard names the real cause and
# the fix instead.
PRETTIER := node_modules/.bin/prettier
REQUIRE_PRETTIER = test -x $(PRETTIER) || { \
	  echo "✗ $(PRETTIER) not found — run: make install"; exit 1; }

# No --ignore-path flag, deliberately: passing one REPLACES prettier's implicit
# set (.gitignore and .prettierignore, both tracked) rather than adding to it, so
# an untracked local ignore file would become a live input to the gate and make
# it answer differently per machine. Anything that must escape the formatter
# earns a line in one of those two tracked files.
fmt-web:  ## Format Markdown/JSON only (prettier)
	@$(REQUIRE_PRETTIER)
	@$(PRETTIER) --write .

fmt-web-check:  ## Check Markdown/JSON formatting only (prettier)
	@$(REQUIRE_PRETTIER)
	@$(PRETTIER) --check .
