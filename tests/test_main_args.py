from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

import costcompass
from costcompass import config, main
from costcompass.refresh import orchestrator

runner = CliRunner()


def _now_iso() -> str:
    """A just-now fetch stamp, in the format the server sends."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stale_iso(days: int = 12) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- version ---------------------------------------------------------------


def test_version_flag_reports_distribution_and_build():
    result = runner.invoke(main.app, ["--version"])
    assert result.exit_code == 0
    line = result.stdout.strip()
    assert line.startswith("costcompass-cli ")
    assert costcompass.package_version() in line


def test_version_command_matches_flag():
    """The subcommand is a second door onto one string, not a second string."""
    flag = runner.invoke(main.app, ["--version"]).stdout.strip()
    command = runner.invoke(main.app, ["version"])
    assert command.exit_code == 0
    assert command.stdout.strip() == flag


def test_version_short_flag():
    assert (
        runner.invoke(main.app, ["-V"]).stdout.strip()
        == runner.invoke(main.app, ["--version"]).stdout.strip()
    )


def test_bare_invocation_shows_help_and_succeeds():
    """A root callback is what makes --version possible, and pairing one with
    Typer's no_args_is_help turns a bare `costcompass` into exit 2. Callers read
    that status, so the help-with-exit-0 behaviour is pinned here."""
    result = runner.invoke(main.app, [])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_build_id_includes_git_sha(monkeypatch):
    monkeypatch.setattr(costcompass, "GIT_SHA", "abc123def456")
    assert costcompass.build_id().endswith(" (git abc123def456)")


def test_build_id_omits_empty_git_sha(monkeypatch):
    """A build generated outside a git tree prints no hollow suffix."""
    monkeypatch.setattr(costcompass, "GIT_SHA", "")
    assert costcompass.build_id() == costcompass.package_version()
    assert "git" not in costcompass.user_agent()


# --- pure arg interpretation ---------------------------------------------


def test_plan_total():
    assert main.plan_action(None, None, False) == main.Action("total", None)


def test_plan_service():
    assert main.plan_action("claude", None, False) == main.Action("service", "claude")


def test_plan_details():
    assert main.plan_action("claude", "details", False) == main.Action(
        "details", "claude"
    )


def test_plan_refresh_all():
    assert main.plan_action("refresh", None, True) == main.Action("refresh", None)


def test_plan_refresh_one():
    assert main.plan_action("claude", "refresh", True) == main.Action(
        "refresh", "claude"
    )


def test_vault_required_for_refresh():
    with pytest.raises(ValueError, match="requires --vault"):
        main.plan_action("refresh", None, False)


def test_vault_rejected_without_refresh():
    with pytest.raises(ValueError, match="only valid with a refresh"):
        main.plan_action("claude", None, True)


def test_unknown_action():
    with pytest.raises(ValueError, match="Unknown action"):
        main.plan_action("claude", "frobnicate", False)


def test_refresh_with_extra_arg():
    with pytest.raises(ValueError, match="no extra argument"):
        main.plan_action("refresh", "details", False)


def test_plan_breakdown():
    assert main.plan_action("breakdown", None, False) == main.Action("breakdown", None)


def test_plan_breakdown_rejects_extra_arg():
    with pytest.raises(ValueError, match="no extra argument"):
        main.plan_action("breakdown", "details", False)


# --- CLI wiring ----------------------------------------------------------


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    monkeypatch.delenv(config.ENV_API_URL, raising=False)
    return tmp_path


def test_vault_without_refresh_errors(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    result = runner.invoke(main.app, ["mtd", "claude", "--vault"])
    assert result.exit_code != 0


def test_missing_key_message(clean_env):
    result = runner.invoke(main.app, ["mtd"])
    assert result.exit_code != 0
    assert "No API key configured" in result.output


def test_refresh_quiet_flag_accepted_and_suppresses_progress(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main, "_read_vault_password", lambda: "pw")
    seen = {}

    def fake_run(cfg, key, service, password, *, progress=False, **kw):
        seen["progress"] = progress
        return []

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    result = runner.invoke(main.app, ["mtd", "refresh", "--vault", "--quiet"])
    assert result.exit_code == 0
    assert seen["progress"] is False  # --quiet suppresses the ticker


def test_total_happy_path(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def summary(self, provider=None):
            return {"mtd_usd": 7.0}

    monkeypatch.setattr(main.api, "Client", FakeClient)
    result = runner.invoke(main.app, ["mtd"])
    assert result.exit_code == 0
    assert "$7.00" in result.output


def test_total_json(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def summary(self, provider=None):
            return {"mtd_usd": 7.0, "forecast_usd": 9.0}

    monkeypatch.setattr(main.api, "Client", FakeClient)
    result = runner.invoke(main.app, ["mtd", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {"mtd_usd": 7.0, "forecast_usd": 9.0}


def test_details_json(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def summary(self, provider=None):
            return {"mtd_usd": 5.0}

        def providers(self):
            return [
                {
                    "id": "anthropic",
                    "short_name": "Claude",
                    "display_name": "Anthropic (Claude)",
                    "enabled": True,
                }
            ]

        def breakdown(self):
            return [
                {
                    "provider_id": "anthropic",
                    "model_breakdown": [{"model": "x", "cost_usd": 1.0}],
                }
            ]

    monkeypatch.setattr(main.api, "Client", FakeClient)
    result = runner.invoke(main.app, ["mtd", "claude", "details", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["provider_id"] == "anthropic"
    assert data["display_name"] == "Anthropic (Claude)"
    assert data["summary"]["mtd_usd"] == 5.0
    assert data["models"][0]["model"] == "x"


class _FakeCardsClient:
    """Serves a fixed providers list + breakdown (one provider, one sub)."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def summary(self, provider=None):
        # The breakdown payload has nowhere to carry scope-level facts, so the
        # human path reads freshness from here. Fresh, so these tests assert on
        # the card output alone.
        return {"enabled_provider_count": 1, "newest_fetched_at": _now_iso()}

    def providers(self):
        return [
            {
                "id": "anthropic",
                "short_name": "Claude",
                "display_name": "Anthropic",
                "enabled": True,
            }
        ]

    def breakdown(self):
        return [
            {
                "provider_id": "anthropic",
                "display_name": "Anthropic",
                "kind": "provider",
                "cost_usd": 96.71,
            },
            {
                "provider_id": "u1",
                "display_name": "Higgsfield",
                "kind": "subscription",
                "cost_usd": 14.5,
            },
        ]


def test_breakdown_human(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _FakeCardsClient)
    result = runner.invoke(main.app, ["mtd", "breakdown"])
    assert result.exit_code == 0
    assert "Anthropic" in result.output
    assert "Higgsfield" in result.output
    assert "Total" in result.output


def test_breakdown_json(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _FakeCardsClient)
    result = runner.invoke(main.app, ["mtd", "breakdown", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total_usd"] == round(96.71 + 14.5, 4)
    assert data["cards"][0]["display_name"] == "Anthropic"  # ranked by cost
    assert any(c["kind"] == "subscription" for c in data["cards"])


def test_subscription_resolves_via_breakdown(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _FakeCardsClient)
    result = runner.invoke(main.app, ["mtd", "higgsfield"])
    assert result.exit_code == 0
    assert "$14.50" in result.output


def test_subscription_json(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _FakeCardsClient)
    result = runner.invoke(main.app, ["mtd", "higgsfield", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["kind"] == "subscription"
    assert data["display_name"] == "Higgsfield"
    assert data["cost_usd"] == 14.5


def test_unknown_service_lists_subscriptions_too(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _FakeCardsClient)
    result = runner.invoke(main.app, ["mtd", "zzznope"])
    assert result.exit_code != 0
    assert "Higgsfield" in result.output  # the error lists subscriptions


def test_refresh_json_forces_quiet_and_emits_payload(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main, "_read_vault_password", lambda: "pw")
    seen = {}

    def fake_run(cfg, key, service, password, *, echo=print, progress=False, **kw):
        seen["progress"] = progress
        return orchestrator.RefreshResult(
            outcomes=[
                orchestrator.EntryOutcome("anthropic", "", "success", events_ingested=3)
            ],
            mtd_usd=131.01,
        )

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    result = runner.invoke(main.app, ["mtd", "refresh", "--vault", "--json"])
    assert result.exit_code == 0
    assert seen["progress"] is False  # --json implies --quiet → no ticker
    data = json.loads(result.output)
    assert data["mtd_usd"] == 131.01
    assert data["providers"][0] == {
        "provider_id": "anthropic",
        "instance_key": "",
        "instance_label": None,
        "state": "success",
        "events_ingested": 3,
        "error_message": None,
    }


@pytest.mark.parametrize("extra", [[], ["--json"]])
def test_refresh_deadline_message_reaches_the_user_in_every_mode(
    clean_env, monkeypatch, extra
):
    """A run that gives up on its budget reports a failed cancelling finalize by
    appending it to the deadline message, precisely so it survives ``--json``:
    the ``echo`` seam the orchestrator prints its per-card lines through is a
    no-op there, and a stranded run row is least likely to be noticed in exactly
    that scripted use. This is the only test that covers the last hop — the
    orchestrator suite stops at the exception it raises."""
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main, "_read_vault_password", lambda: "pw")

    def fake_run(*a, **kw):
        raise orchestrator.RefreshDeadlineExceeded(
            "Refresh took too long and was stopped. Some providers may not "
            "have been updated — try again. The server was not told this run "
            "was cancelled (HTTP 500), so it may show as still running for a "
            "while."
        )

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    result = runner.invoke(main.app, ["mtd", "refresh", "--vault", *extra])

    assert result.exit_code != 0
    assert "was not told this run was cancelled" in result.output
    assert "HTTP 500" in result.output
    # The actionable sentence is not displaced by the diagnostic one.
    assert "Refresh took too long" in result.output


# --- auth: verify before storing -----------------------------------------
# A mistyped secret (notably the vault password, which unlocks every provider
# credential) must never be persisted. Storing first and validating later put a
# plaintext vault password on disk while reporting success.

from costcompass import secrets


@pytest.fixture
def tty_stdin(monkeypatch):
    """Secret-storing prompts refuse without a TTY; fake one so CliRunner's
    piped ``input=`` reaches the hidden prompt."""
    monkeypatch.setattr(main, "_stdin_isatty", lambda: True)


def _client(*, key_valid=True):
    class FakeClient:
        def __init__(self, base_url, api_key, *a, **k):
            self.base_url = base_url

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def me(self):
            if not key_valid:
                raise main.api.ApiError("Invalid or expired API key.")
            return {"email": "user@example.com"}

    return FakeClient


def test_auth_login_rejects_invalid_key_and_stores_nothing(
    tty_stdin, clean_env, fake_store, monkeypatch
):
    monkeypatch.setattr(main.api, "Client", _client(key_valid=False))
    result = runner.invoke(main.app, ["auth", "login"], input="not-a-real-key\n")

    assert result.exit_code != 0
    assert fake_store.read(secrets.SecretItem.API_KEY) is None, (
        "unverified key was stored"
    )
    assert not config.config_path().exists(), "a secret reached the config file"


def test_auth_login_does_not_echo_the_rejected_secret(
    tty_stdin, clean_env, monkeypatch
):
    monkeypatch.setattr(main.api, "Client", _client(key_valid=False))
    secret = "hunter2-vault-pw"
    result = runner.invoke(main.app, ["auth", "login"], input=f"{secret}\n")
    assert secret not in result.output


def test_auth_login_stores_a_valid_key_in_the_credential_store(
    tty_stdin, clean_env, fake_store, monkeypatch
):
    monkeypatch.setattr(main.api, "Client", _client())
    result = runner.invoke(main.app, ["auth", "login"], input="sk-good\n")

    assert result.exit_code == 0
    assert fake_store.read(secrets.SecretItem.API_KEY) == "sk-good"
    # The API key must never be written to the file, whatever else happens.
    body = config.config_path().read_text() if config.config_path().exists() else ""
    assert "sk-good" not in body


def test_auth_login_verifies_against_the_url_flag(tty_stdin, clean_env, monkeypatch):
    seen = {}

    class FakeClient(_client()):
        def __init__(self, base_url, api_key, *a, **k):
            seen["base_url"] = base_url

    monkeypatch.setattr(main.api, "Client", FakeClient)
    result = runner.invoke(
        main.app,
        ["auth", "login", "--url", "http://localhost:8080/api/v1"],
        input="sk-good\n",
    )
    assert result.exit_code == 0
    # Checked against the server it will be used against, not the default.
    assert seen["base_url"] == "http://localhost:8080/api/v1"


def test_auth_vault_rejects_a_password_that_does_not_decrypt(
    tty_stdin, clean_env, fake_store, monkeypatch
):
    fake_store.write(secrets.SecretItem.API_KEY, "sk-good")
    monkeypatch.setattr(main.api, "Client", _client())

    def boom(client, password):
        raise main.vault_mod.VaultError("wrong vault password")

    monkeypatch.setattr(main.vault_mod, "fetch_and_decrypt", boom)
    result = runner.invoke(main.app, ["auth", "vault"], input="wrong-pw\n")

    assert result.exit_code != 0
    assert fake_store.read(secrets.SecretItem.VAULT_PASSWORD) is None


def test_auth_vault_stores_a_password_that_decrypts(
    tty_stdin, clean_env, fake_store, monkeypatch
):
    fake_store.write(secrets.SecretItem.API_KEY, "sk-good")
    monkeypatch.setattr(main.api, "Client", _client())
    monkeypatch.setattr(main.vault_mod, "fetch_and_decrypt", lambda c, p: object())
    result = runner.invoke(main.app, ["auth", "vault"], input="right-pw\n")

    assert result.exit_code == 0
    assert fake_store.read(secrets.SecretItem.VAULT_PASSWORD) == "right-pw"


# Without a TTY the hidden prompt would fall back to reading stdin with echo
# (getpass's pipe behaviour) — a secret-storing command must refuse instead.
# No tty_stdin fixture here: CliRunner's fake stdin is genuinely not a TTY.


def test_auth_login_refuses_without_a_tty(clean_env, fake_store, monkeypatch):
    monkeypatch.setattr(main.api, "Client", _client())
    result = runner.invoke(main.app, ["auth", "login"], input="sk-good\n")
    assert result.exit_code != 0
    assert "terminal" in result.output
    assert fake_store.read(secrets.SecretItem.API_KEY) is None


def test_auth_vault_refuses_without_a_tty(clean_env, fake_store, monkeypatch):
    fake_store.write(secrets.SecretItem.API_KEY, "sk-good")
    monkeypatch.setattr(main.api, "Client", _client())
    result = runner.invoke(main.app, ["auth", "vault"], input="right-pw\n")
    assert result.exit_code != 0
    assert fake_store.read(secrets.SecretItem.VAULT_PASSWORD) is None


def test_auth_status_json_reports_sources_and_readiness(
    clean_env, fake_store, monkeypatch
):
    fake_store.write(secrets.SecretItem.API_KEY, "sk-good")
    fake_store.write(secrets.SecretItem.VAULT_PASSWORD, "pw")
    monkeypatch.setattr(main.api, "Client", _client())
    monkeypatch.setattr(main.vault_mod, "fetch_and_decrypt", lambda c, p: object())

    result = runner.invoke(main.app, ["auth", "status", "--json"])
    report = json.loads(result.output)

    assert result.exit_code == 0
    assert report["api_key"] == {"source": config.SOURCE_STORE, "valid": True}
    assert report["vault"] == {"source": config.SOURCE_STORE, "unlocks": True}
    assert report["ready"] == {"spend": True, "refresh": True}


def test_auth_status_json_when_nothing_configured(clean_env, monkeypatch):
    result = runner.invoke(main.app, ["auth", "status", "--json"])
    report = json.loads(result.output)

    assert result.exit_code != 0
    assert report["api_key"] == {"source": None, "valid": False}
    assert report["vault"] == {"source": None, "unlocks": None}
    assert report["ready"] == {"spend": False, "refresh": False}


def test_auth_status_refresh_not_ready_without_vault(
    clean_env, fake_store, monkeypatch
):
    fake_store.write(secrets.SecretItem.API_KEY, "sk-good")
    monkeypatch.setattr(main.api, "Client", _client())

    result = runner.invoke(main.app, ["auth", "status", "--json"])
    report = json.loads(result.output)

    # Spend works, refresh does not — the distinction the skill's preflight needs.
    assert result.exit_code == 0
    assert report["ready"] == {"spend": True, "refresh": False}


def test_auth_status_reports_a_plaintext_file_source(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-good")
    config._write_text('vault_password = "pw"\n')
    monkeypatch.setattr(main.api, "Client", _client())
    monkeypatch.setattr(main.vault_mod, "fetch_and_decrypt", lambda c, p: object())

    result = runner.invoke(main.app, ["auth", "status", "--json"])
    report = json.loads(result.output)

    assert report["api_key"]["source"] == config.SOURCE_ENV
    assert report["vault"]["source"] == config.SOURCE_FILE


# --- Stale-data reminder wiring -------------------------------------------


class _StaleClient:
    """Every read path served, with a fetch old enough to warrant the note."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def summary(self, provider=None):
        return {
            "mtd_usd": 7.0,
            "enabled_provider_count": 1,
            "newest_fetched_at": _stale_iso(12),
            # The server decides which cards are behind and by how much; this
            # fixture states that answer rather than a timestamp to be aged.
            "stale_cards": [
                {
                    "provider_id": "anthropic",
                    "instance_key": "",
                    "display_name": "Claude",
                    "days": 12,
                    "blocked": False,
                }
            ],
        }

    def providers(self):
        return [
            {
                "id": "anthropic",
                "short_name": "Claude",
                "display_name": "Anthropic (Claude)",
                "enabled": True,
            }
        ]

    def breakdown(self):
        return [
            {
                "provider_id": "anthropic",
                "display_name": "Anthropic",
                "kind": "provider",
                "cost_usd": 7.0,
            }
        ]


STALE_NOTE = "Not updated: Claude (12 days)"


@pytest.mark.parametrize(
    "argv",
    [
        ["mtd"],
        ["mtd", "claude"],
        ["mtd", "claude", "details"],
        ["mtd", "breakdown"],
    ],
)
def test_every_spend_path_warns_on_stale_data(clean_env, monkeypatch, argv):
    """The ticket's whole point: no way to read a figure without being told how
    old it is. Each of these is a separate branch in `mtd`, so parametrize
    rather than trust one of them to stand for the rest."""
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _StaleClient)
    result = runner.invoke(main.app, argv)
    assert result.exit_code == 0
    assert STALE_NOTE in result.stderr
    assert "costcompass mtd refresh --vault" in result.stderr


def test_stale_note_stays_off_stdout(clean_env, monkeypatch):
    """`costcompass mtd | pbcopy` must still yield just the figure."""
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _StaleClient)
    result = runner.invoke(main.app, ["mtd"])
    assert result.stdout.strip() == "$7.00"


@pytest.mark.parametrize(
    "argv",
    [
        ["mtd", "--json"],
        ["mtd", "claude", "--json"],
        ["mtd", "claude", "details", "--json"],
        ["mtd", "breakdown", "--json"],
    ],
)
def test_json_output_carries_no_stale_note(clean_env, monkeypatch, argv):
    """A hint on either stream would contaminate output something is parsing."""
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main.api, "Client", _StaleClient)
    result = runner.invoke(main.app, argv)
    assert result.exit_code == 0
    assert STALE_NOTE not in result.stderr
    assert STALE_NOTE not in result.stdout
    json.loads(result.stdout)


def test_refresh_never_warns_about_stale_data(clean_env, monkeypatch):
    """Refresh just updated the data, so a reminder to refresh would be absurd.
    Today that holds because the refresh branch returns before the print path —
    control flow, which a refactor can undo silently. Pin it."""
    monkeypatch.setenv(config.ENV_API_KEY, "sk-x")
    monkeypatch.setattr(main, "_read_vault_password", lambda: "pw")
    monkeypatch.setattr(main.api, "Client", _StaleClient)
    monkeypatch.setattr(main.orchestrator, "run", lambda *a, **k: [])
    result = runner.invoke(main.app, ["mtd", "refresh", "--vault"])
    assert result.exit_code == 0
    assert "Not updated in" not in result.stderr
    assert "Not updated in" not in result.stdout
