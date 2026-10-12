"""Token storage: in-app Settings file + load_config fallback order."""

import os
import stat

import pytest

from config import (
    ConfigError,
    load_config,
    load_user_token,
    save_user_token,
    user_token_path,
)


@pytest.fixture()
def isolated_xdg(tmp_path, monkeypatch):
    cfg = tmp_path / "xdg"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    for var in ("TODOIST_API_TOKEN", "TODOIST_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return cfg


def test_save_load_roundtrip(isolated_xdg):
    path = save_user_token("  secret-token-123  ")
    assert path == user_token_path()
    assert load_user_token() == "secret-token-123"


def test_save_is_owner_only(isolated_xdg):
    path = save_user_token("tok")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_save_blank_raises(isolated_xdg):
    with pytest.raises(ConfigError):
        save_user_token("   ")


def test_load_user_token_empty_when_missing(isolated_xdg):
    assert load_user_token() == ""


def test_load_config_prefers_file_over_env(tmp_path, monkeypatch, isolated_xdg):
    save_user_token("user-file-token")
    monkeypatch.setenv("TODOIST_API_TOKEN", "env-token")
    env = tmp_path / ".env"
    env.write_text("TODOIST_API_TOKEN=file-token\n", encoding="utf-8")
    assert load_config(env_path=env).token == "file-token"


def test_load_config_prefers_file_over_user_file(tmp_path, monkeypatch, isolated_xdg):
    save_user_token("user-file-token")
    env = tmp_path / ".env"
    env.write_text("TODOIST_API_TOKEN=file-token\n", encoding="utf-8")
    assert load_config(env_path=env).token == "file-token"


def test_load_config_falls_back_to_user_file(tmp_path, isolated_xdg):
    save_user_token("user-file-token")
    env = tmp_path / ".env"
    env.write_text("# empty\n", encoding="utf-8")
    assert load_config(env_path=env).token == "user-file-token"


def test_load_config_missing_token_raises(tmp_path, isolated_xdg):
    env = tmp_path / ".env"
    env.write_text("# empty\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env_path=env)


def test_load_config_allow_missing_token(tmp_path, isolated_xdg):
    env = tmp_path / ".env"
    env.write_text("# empty\n", encoding="utf-8")
    config = load_config(env_path=env, allow_missing_token=True)
    assert config.token == ""
