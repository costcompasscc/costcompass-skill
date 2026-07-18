"""Typer entry point: `costcompass mtd ...` and `costcompass auth ...`."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Optional

import typer

from . import api, config, render, secrets, services
from . import vault as vault_mod
from .refresh import orchestrator

REFRESH = "refresh"
DETAILS = "details"
BREAKDOWN = "breakdown"

app = typer.Typer(
    help="CostCompass — month-to-date spend from your terminal.",
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(help="Manage the stored API key and server URL.")
app.add_typer(auth_app, name="auth")


@dataclass
class Action:
    kind: str  # "total" | "service" | "details" | "refresh"
    service: str | None  # raw service name (None = portfolio / refresh-all)


def plan_action(service: str | None, action: str | None, vault: bool) -> Action:
    """Pure arg interpretation; raises ValueError on misuse."""
    is_refresh = (service == REFRESH and action is None) or (action == REFRESH)
    is_breakdown = service == BREAKDOWN and action is None
    if action is not None and action not in (DETAILS, REFRESH):
        raise ValueError(f"Unknown action '{action}'. Use 'details' or 'refresh'.")
    if service == REFRESH and action is not None:
        raise ValueError("Usage: costcompass mtd refresh  (no extra argument).")
    if service == BREAKDOWN and action is not None:
        raise ValueError("Usage: costcompass mtd breakdown  (no extra argument).")
    if is_refresh and not vault:
        raise ValueError("refresh requires --vault to unlock provider credentials.")
    if vault and not is_refresh:
        raise ValueError("--vault is only valid with a refresh action.")
    if is_refresh:
        return Action("refresh", None if service == REFRESH else service)
    if is_breakdown:
        return Action("breakdown", None)
    if service is None:
        return Action("total", None)
    if action == DETAILS:
        return Action("details", service)
    return Action("service", service)


def _fail(message: str) -> None:
    # Single error-output sink: exception messages can embed server- or
    # provider-sourced text, so strip terminal control sequences before they
    # reach the user's terminal.
    typer.echo(render.safe_text(message), err=True)
    raise typer.Exit(1)


def _stdin_isatty() -> bool:
    """Seam for tests; CliRunner's fake stdin is never a TTY."""
    return sys.stdin.isatty()


def _prompt_secret(label: str) -> str:
    """Hidden prompt for a secret that is about to be *stored*.

    Refuses outright without a TTY: click's hidden prompt falls back to
    ``getpass``, which on a pipe reads stdin with echo (plus a warning) rather
    than aborting — and a secret-storing command must never take its input
    where it could be echoed or logged. The refresh path is different on
    purpose: ``_read_vault_password`` accepts piped stdin because it only
    *uses* the password, and piping is how a script supplies it.
    """
    if not _stdin_isatty():
        _fail(
            f"{label} must be typed at a hidden interactive prompt — run this "
            "command in a real terminal window. (To go env-var instead, see "
            "`costcompass auth status`.)"
        )
    return typer.prompt(label, hide_input=True)


def _read_vault_password() -> str:
    """Obtain the vault password without ever reading it from argv.

    Credential store, then the environment, then the config file (see
    ``config.resolve_vault_password``); an interactive terminal is asked last.
    """
    resolved = config.resolve_vault_password()
    if resolved.value:
        return resolved.value
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            _fail("No vault password supplied on stdin.")
        return line.rstrip("\n")
    return typer.prompt("Vault password", hide_input=True)


def _models_for(
    breakdown: list[dict[str, Any]], provider_id: str
) -> list[dict[str, Any]]:
    """Merge per-model rows across every instance of a provider."""
    models: list[dict[str, Any]] = []
    for entry in breakdown:
        if entry.get("provider_id") == provider_id:
            models.extend(entry.get("model_breakdown") or [])
    return models


def _emit(as_json: bool, data: Any, text: str) -> None:
    """Print machine JSON or the human text, depending on ``--json``."""
    typer.echo(json.dumps(data, indent=2) if as_json else text)


def _subscription_cards(breakdown: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Standalone (non-provider) cards from the breakdown, normalized so the
    resolver can match them — it keys on ``id``, breakdown rows carry
    ``provider_id``."""
    return [
        {**c, "id": c.get("provider_id")}
        for c in breakdown
        if (c.get("kind") or "provider") != "provider"
    ]


def _card_payload(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_id": card.get("provider_id"),
        "display_name": card.get("display_name"),
        "kind": card.get("kind") or "provider",
        "instance_key": card.get("instance_key", ""),
        "cost_usd": card.get("cost_usd") or 0.0,
    }


def _breakdown_payload(cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_usd": round(sum(c.get("cost_usd") or 0.0 for c in cards), 4),
        "cards": [
            _card_payload(c)
            for c in sorted(cards, key=lambda x: -(x.get("cost_usd") or 0.0))
        ],
    }


def _refresh_payload(result: orchestrator.RefreshResult) -> dict[str, Any]:
    """JSON shape for a refresh: the closing MTD plus a per-card outcome list."""
    return {
        "mtd_usd": result.mtd_usd,
        "providers": [
            {
                "provider_id": o.provider_id,
                "instance_key": o.instance_key,
                "instance_label": o.instance_label,
                "state": o.state,
                "events_ingested": o.events_ingested,
                "error_message": o.error_message,
            }
            for o in result.outcomes
        ],
    }


@app.command()
def mtd(
    service: Optional[str] = typer.Argument(
        None, help="Service name (e.g. claude) or 'refresh' to refresh all."
    ),
    action: Optional[str] = typer.Argument(
        None, help="'details' or 'refresh' for the given service."
    ),
    vault: bool = typer.Option(
        False,
        "--vault",
        help="Unlock the vault for a refresh (prompts; never pass the password on the command line).",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress the in-progress '.' ticker during a refresh.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON (implies --quiet).",
    ),
) -> None:
    """Show month-to-date spend, or refresh provider usage."""
    try:
        act = plan_action(service, action, vault)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    # JSON output must stay clean machine-readable, so it forces --quiet.
    quiet = quiet or as_json

    cfg = config.load()
    try:
        key = cfg.require_key()
    except config.ConfigError as exc:
        _fail(str(exc))

    if act.kind == "refresh":
        password = _read_vault_password()
        # Only tick on an interactive terminal — dots would pollute a pipe/file.
        progress = not quiet and sys.stdout.isatty()
        try:
            result = orchestrator.run(
                cfg,
                key,
                act.service,
                password,
                echo=(lambda *_: None) if as_json else typer.echo,
                progress=progress,
            )
        except (api.ApiError, services.ResolveError, orchestrator.RefreshError) as exc:
            _fail(str(exc))
        if as_json:
            typer.echo(json.dumps(_refresh_payload(result), indent=2))
        return

    try:
        with api.Client(cfg.api_url, key) as client:
            if act.kind == "breakdown":
                cards = client.breakdown()
                _emit(
                    as_json, _breakdown_payload(cards), render.format_breakdown(cards)
                )
                return
            if act.kind == "total":
                summary = client.summary()
                _emit(as_json, summary, render.format_amount(summary))
                return

            providers = client.providers()
            prov = services.match(act.service, providers)
            if prov is not None:
                provider_id = prov["id"]
                summary = client.summary(provider=provider_id)
                if act.kind == "service":
                    _emit(as_json, summary, render.format_amount(summary))
                else:
                    models = _models_for(client.breakdown(), provider_id)
                    label = prov.get("display_name") or provider_id
                    payload = {
                        "provider_id": provider_id,
                        "display_name": label,
                        "summary": summary,
                        "models": models,
                    }
                    _emit(
                        as_json, payload, render.format_details(label, summary, models)
                    )
                return

            # Not a metered provider — fall back to a standalone subscription
            # card (e.g. Higgsfield), which lives only in the breakdown.
            subs = _subscription_cards(client.breakdown())
            sub = services.match(act.service, subs)
            if sub is None:
                raise services.ResolveError(
                    f"Unknown service '{act.service}'. "
                    f"Available: {services.available_names(providers, subs)}"
                )
            cost = sub.get("cost_usd") or 0.0
            label = sub.get("display_name") or act.service
            text = (
                render.money(cost)
                if act.kind == "service"
                else render.format_subscription(label, cost)
            )
            _emit(as_json, _card_payload(sub), text)
    except (api.ApiError, services.ResolveError) as exc:
        _fail(str(exc))


def _verify_key(api_url: str, key: str) -> dict:
    """Check a key against the server, returning its identity payload.

    Exits with the server's rejection message if the key is not accepted.
    """
    try:
        with api.Client(api_url, key) as client:
            return client.me()
    except api.ApiError as exc:
        _fail(str(exc))
        raise  # unreachable; _fail raises. Keeps the return type honest.


def _identity(me: dict) -> str:
    return str(me.get("email") or me.get("id") or "(unknown)")


def _store_secret(item: secrets.SecretItem, value: str) -> None:
    """Put a *already verified* secret into the OS credential store."""
    store = secrets.store()
    if not store.available:
        _fail(
            "No OS credential store is available on this machine. Set "
            f"{config.ENV_API_KEY} / {config.ENV_VAULT_PASSWORD} instead — see "
            "`costcompass auth status`."
        )
    try:
        store.write(item, value)
    except secrets.SecretsUnavailable as exc:
        _fail(str(exc))


@auth_app.command("login")
def auth_login(
    url: Optional[str] = typer.Option(
        None, "--url", help="Server base URL (defaults to the built-in/production URL)."
    ),
) -> None:
    """Verify an API key and store it in the OS credential store."""
    key = _prompt_secret("API key")
    # Verify BEFORE storing, against the server the key will actually be used
    # against. A secret that does not work is a secret that was mistyped, and
    # storing it only hides the mistake until later.
    me = _verify_key(url or config.resolve_api_url(), key)
    _store_secret(secrets.SecretItem.API_KEY, key)
    if url:
        config.save_api_url(url)
    typer.echo(
        f"Stored the API key for {render.safe_text(_identity(me))} "
        "in your OS credential store."
    )


@auth_app.command("vault")
def auth_vault() -> None:
    """Verify the vault password and store it in the OS credential store."""
    cfg = config.load()
    try:
        key = cfg.require_key()
    except config.ConfigError as exc:
        _fail(str(exc))
    password = _prompt_secret("Vault password")
    # Prove it actually decrypts the vault before storing it. This is the check
    # whose absence let a mistyped secret be persisted and reported as success.
    try:
        with api.Client(cfg.api_url, key) as client:
            vault_mod.fetch_and_decrypt(client, password)
    except (api.ApiError, vault_mod.VaultError) as exc:
        _fail(str(exc))
    _store_secret(secrets.SecretItem.VAULT_PASSWORD, password)
    typer.echo("Stored the vault password in your OS credential store.")


def _auth_report() -> dict[str, Any]:
    """What is configured, where it came from, and whether it actually works."""
    cfg = config.load()
    report: dict[str, Any] = {
        "server": cfg.api_url,
        "api_key": {"source": cfg.api_key_source, "valid": False},
        "vault": {"source": None, "unlocks": None},
        "identity": None,
    }
    if cfg.api_key:
        try:
            with api.Client(cfg.api_url, cfg.api_key) as client:
                report["identity"] = _identity(client.me())
            report["api_key"]["valid"] = True
        except api.ApiError:
            pass

    pw = config.resolve_vault_password()
    report["vault"]["source"] = pw.source
    # Only meaningful with a working key — the vault blob is fetched with it.
    if report["api_key"]["valid"] and pw.value:
        try:
            with api.Client(cfg.api_url, cfg.api_key) as client:
                vault_mod.fetch_and_decrypt(client, pw.value)
            report["vault"]["unlocks"] = True
        except (api.ApiError, vault_mod.VaultError):
            report["vault"]["unlocks"] = False

    # Derived here, once, so callers (notably the skill) never re-derive
    # "am I able to do this?" from the parts and drift.
    report["ready"] = {
        "spend": report["api_key"]["valid"],
        "refresh": report["api_key"]["valid"] and report["vault"]["unlocks"] is True,
    }
    return report


def _source_label(source: str | None) -> str:
    return {
        config.SOURCE_STORE: "OS credential store",
        config.SOURCE_ENV: "environment variable",
        config.SOURCE_FILE: "config file (plaintext)",
    }.get(source or "", "not configured")


@auth_app.command("status")
def auth_status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Report whether the API key and vault password are usable."""
    report = _auth_report()
    if as_json:
        typer.echo(json.dumps(report))
        raise typer.Exit(0 if report["ready"]["spend"] else 1)

    typer.echo(f"Server: {report['server']}")
    key = report["api_key"]
    if key["valid"]:
        typer.echo(
            f"API key: {_source_label(key['source'])} — "
            f"authenticated as {render.safe_text(str(report['identity']))}"
        )
    elif key["source"]:
        typer.echo(f"API key: {_source_label(key['source'])} — rejected by the server")
    else:
        typer.echo("API key: not configured")

    vault = report["vault"]
    if vault["unlocks"] is True:
        typer.echo(
            f"Vault password: {_source_label(vault['source'])} — unlocks the vault"
        )
    elif vault["unlocks"] is False:
        typer.echo(
            f"Vault password: {_source_label(vault['source'])} — does NOT unlock the vault"
        )
    elif vault["source"]:
        typer.echo(
            f"Vault password: {_source_label(vault['source'])} — unchecked (needs a valid API key)"
        )
    else:
        typer.echo("Vault password: not configured")

    raise typer.Exit(0 if report["ready"]["spend"] else 1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
