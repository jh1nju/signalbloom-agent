"""Running as a packaged exe: files go next to the exe, first-run setup."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from signalbloom_agent import __main__ as entry
from signalbloom_agent import app
from signalbloom_agent.config import ConfigError, load_config
from signalbloom_agent.register import NEW_CODE_MESSAGE, RegistrationError


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Pretend to be SignalBloom-Agent.exe in tmp_path/exe, started from tmp_path/elsewhere."""
    exe_dir = tmp_path / "exe"
    exe_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "SignalBloom-Agent.exe"))
    monkeypatch.chdir(elsewhere)
    return exe_dir, elsewhere


def test_app_dir_is_exe_folder_when_frozen(frozen):
    exe_dir, _ = frozen
    assert app.app_dir() == exe_dir.resolve()


def test_app_dir_is_cwd_from_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert app.app_dir() == tmp_path
    assert app.resource_path(app.TEMPLATE_NAME).is_file()


def test_create_config_fills_url_and_code(tmp_path):
    path = tmp_path / "config.yaml"
    app.create_config(path, "SB-ABCD-1234")
    config = load_config(path)
    assert config.cloud_url == app.DEFAULT_CLOUD_URL
    assert config.setup_code == "SB-ABCD-1234"
    assert config.site_id is None and config.site_token is None
    assert config.interval_seconds == 30


def test_pause_does_nothing_from_source(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not wait"))
    app.pause_before_exit()


def fake_register(config, path):
    """Stands in for the cloud: saves a site the way the real one does."""
    from signalbloom_agent.register import save_registration
    save_registration(path, "site-1", "secret-token")
    import dataclasses
    return dataclasses.replace(config, site_id="site-1", site_token="secret-token",
                               setup_code=None)


def test_first_run_creates_config_and_data_next_to_exe(frozen, capsys):
    exe_dir, elsewhere = frozen
    seen = {}

    def fake_run(config, demo_fault):
        seen["data_dir"] = config.data_dir
        raise KeyboardInterrupt

    with patch.object(entry, "ask_setup_code", return_value="SB-ABCD-1234"), \
         patch.object(entry, "ensure_registered", side_effect=fake_register), \
         patch.object(entry, "run", side_effect=fake_run), \
         patch("signalbloom_agent.register._restrict_to_owner"):
        assert entry.main([]) == 0

    assert (exe_dir / "config.yaml").is_file()
    assert not (elsewhere / "config.yaml").exists()
    assert seen["data_dir"] == exe_dir.resolve() / "data"
    assert (exe_dir / "data" / "agent.log").is_file()
    assert list(elsewhere.iterdir()) == []
    out = capsys.readouterr().out
    assert "Registered as site site-1." in out

    # Second run: no prompt, just starts.
    with patch.object(entry, "ask_setup_code", side_effect=AssertionError("asked again")), \
         patch.object(entry, "run", side_effect=KeyboardInterrupt):
        assert entry.main([]) == 0
    assert "Already registered as site site-1." in capsys.readouterr().out


def test_bad_code_on_first_run_removes_config_and_waits(frozen, capsys, monkeypatch):
    exe_dir, _ = frozen
    pauses = []
    monkeypatch.setattr("builtins.input", lambda prompt="": pauses.append(prompt) or "")
    with patch.object(entry, "ask_setup_code", return_value="SB-WRONG"), \
         patch.object(entry, "ensure_registered",
                      side_effect=RegistrationError(NEW_CODE_MESSAGE)):
        assert entry.main([]) == 1

    assert not (exe_dir / "config.yaml").exists()
    err = capsys.readouterr().err
    assert "That setup code didn't work" in err
    assert "run SignalBloom-Agent.exe again" in err
    assert pauses == ["Press Enter to close..."]


def test_cancelled_setup_creates_nothing(frozen, monkeypatch):
    exe_dir, _ = frozen
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    with patch.object(entry, "ask_setup_code", side_effect=KeyboardInterrupt):
        assert entry.main([]) == 1
    assert list(exe_dir.iterdir()) == []


def test_from_source_missing_config_is_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with patch.object(entry, "ask_setup_code", side_effect=AssertionError("prompted")):
        assert entry.main([]) == 1
    assert "Config file not found" in capsys.readouterr().err
    with pytest.raises(ConfigError):
        load_config(Path("config.yaml"))


def test_ask_setup_code_cleans_up_and_reasks_when_blank(monkeypatch):
    answers = iter(["", "   ", "  sb-7kq2-91xd \n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert app.ask_setup_code() == "SB-7KQ2-91XD"


def test_ask_setup_code_lets_cancel_through(monkeypatch):
    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(EOFError):
        app.ask_setup_code()
