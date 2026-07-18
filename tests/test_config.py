from __future__ import annotations

import stat

import pytest

from costcompass import config, secrets


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    # conftest's autouse fixtures already isolate the credential store and env;
    # this narrows the config file to this test's own tmp_path.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _write_config(path, body: str):
    d = path / "costcompass"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(body)


# --- URL ------------------------------------------------------------------


def test_default_url_when_nothing_set(xdg):
    cfg = config.load()
    assert cfg.api_key is None
    assert cfg.api_url == config.DEFAULT_API_URL


def test_env_url_wins_over_file_and_strips_slash(xdg, monkeypatch):
    _write_config(xdg, 'api_url = "https://file.example/api/v1"\n')
    monkeypatch.setenv(config.ENV_API_URL, "https://env.example/api/v1/")
    assert config.load().api_url == "https://env.example/api/v1"


def test_save_api_url_preserves_other_settings(xdg):
    _write_config(xdg, 'vault_password = "pw"\n')
    config.save_api_url("https://new.example/api/v1")
    assert config.resolve_vault_password().value == "pw"
    assert config.resolve_api_url() == "https://new.example/api/v1"


def test_save_api_url_preserves_comments_and_non_string_values(xdg):
    # The rewrite is textual, so hand-added comments, non-string values, and
    # tables must survive verbatim (a parse-and-rewrite would str() them into
    # invalid or corrupted TOML).
    body = (
        "# my note\n"
        'vault_password = "pw"\n'
        "some_flag = true\n"
        'api_url = "https://old.example/api/v1"\n'
        "[future_section]\n"
        "nested = 1\n"
    )
    _write_config(xdg, body)
    config.save_api_url("https://new.example/api/v1")
    text = config.config_path().read_text()
    assert "# my note" in text
    assert "some_flag = true" in text
    assert "[future_section]" in text
    assert "nested = 1" in text
    assert 'api_url = "https://new.example/api/v1"' in text
    assert "old.example" not in text
    assert config.resolve_api_url() == "https://new.example/api/v1"


def test_save_api_url_lands_in_root_table_despite_sections(xdg):
    # Prepend, not append: an appended line would fall inside the last
    # [section] and vanish from the root-table lookup.
    _write_config(xdg, "[future_section]\nnested = 1\n")
    config.save_api_url("https://new.example/api/v1")
    assert config.resolve_api_url() == "https://new.example/api/v1"


def test_save_api_url_replaces_a_corrupt_file(xdg):
    # Matches _read_file: a file that doesn't parse is treated as absent.
    _write_config(xdg, "this is not valid toml {{{\n")
    config.save_api_url("https://new.example/api/v1")
    assert config.resolve_api_url() == "https://new.example/api/v1"


def test_config_file_is_0600(xdg):
    path = config.save_api_url("https://x.example/api/v1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_config_does_not_break_resolution(xdg, monkeypatch):
    _write_config(xdg, "this is not valid toml {{{\n")
    monkeypatch.setenv(config.ENV_API_KEY, "sk-env")
    # A broken file must not take down a command whose key resolves elsewhere.
    assert config.load().api_key == "sk-env"
    assert config.load().api_url == config.DEFAULT_API_URL


# --- API key: credential store, then env; NEVER the file ------------------


def test_api_key_is_never_read_from_the_config_file(xdg):
    _write_config(xdg, 'api_key = "sk-from-file"\n')
    resolved = config.resolve_api_key()
    assert resolved.value is None, "the config file must not be an API-key source"
    assert resolved.source is None


def test_api_key_from_store_is_preferred_over_env(xdg, fake_store, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-env")
    fake_store.write(secrets.SecretItem.API_KEY, "sk-stored")
    resolved = config.resolve_api_key(fake_store)
    assert resolved.value == "sk-stored"
    assert resolved.source == config.SOURCE_STORE


def test_api_key_falls_back_to_env(xdg, fake_store, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-env")
    resolved = config.resolve_api_key(fake_store)
    assert resolved.value == "sk-env"
    assert resolved.source == config.SOURCE_ENV


def test_require_key_raises_when_missing(xdg):
    with pytest.raises(config.ConfigError):
        config.load().require_key()


# --- Vault password: store, then env, then file ---------------------------


def test_vault_prefers_store_over_env_and_file(xdg, fake_store, monkeypatch):
    _write_config(xdg, 'vault_password = "pw-file"\n')
    monkeypatch.setenv(config.ENV_VAULT_PASSWORD, "pw-env")
    fake_store.write(secrets.SecretItem.VAULT_PASSWORD, "pw-stored")
    resolved = config.resolve_vault_password(fake_store)
    assert resolved.value == "pw-stored"
    assert resolved.source == config.SOURCE_STORE


def test_vault_prefers_env_over_file(xdg, fake_store, monkeypatch):
    _write_config(xdg, 'vault_password = "pw-file"\n')
    monkeypatch.setenv(config.ENV_VAULT_PASSWORD, "pw-env")
    resolved = config.resolve_vault_password(fake_store)
    assert resolved.value == "pw-env"
    assert resolved.source == config.SOURCE_ENV


def test_vault_falls_back_to_file(xdg, fake_store):
    _write_config(xdg, 'vault_password = "pw-file"\n')
    resolved = config.resolve_vault_password(fake_store)
    assert resolved.value == "pw-file"
    assert resolved.source == config.SOURCE_FILE


def test_vault_absent_everywhere(xdg, fake_store):
    resolved = config.resolve_vault_password(fake_store)
    assert resolved.value is None
    assert resolved.source is None
    assert not resolved


def test_toml_escaping_roundtrip(xdg, fake_store):
    weird = 'pw-"quote"\\back'
    config._write_text(f"vault_password = {config._toml_str(weird)}\n")
    assert config.resolve_vault_password(fake_store).value == weird


def test_save_api_url_escaping_roundtrip(xdg):
    weird = 'https://x.example/api/v1?a="q"\\b'
    config.save_api_url(weird)
    assert config.resolve_api_url() == weird


# --- No usable backend is a fallback, not a crash -------------------------


def test_null_store_reports_absent_and_refuses_writes():
    store = secrets.NullStore()
    assert store.read(secrets.SecretItem.API_KEY) is None
    assert store.available is False
    with pytest.raises(secrets.SecretsUnavailable):
        store.write(secrets.SecretItem.API_KEY, "x")


def test_broken_backend_reads_as_absent_rather_than_raising():
    class Exploding:
        def get_password(self, *a):
            raise RuntimeError("keyring is locked")

    store = secrets.KeyringStore(Exploding())
    assert store.read(secrets.SecretItem.API_KEY) is None
