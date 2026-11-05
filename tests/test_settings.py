import pytest

from meeting_copilot.cli import load_settings, user_config


def test_user_file_applies_from_any_directory(tmp_path, monkeypatch):
    user = tmp_path / "user" / "copilot.toml"
    user.parent.mkdir()
    user.write_text('voice = "system"\nname = "Jarvis"\n')
    monkeypatch.setenv("COPILOT_CONFIG", str(user))
    monkeypatch.chdir(tmp_path)
    s = load_settings(None)
    assert s["voice"] == "system" and s["name"] == "Jarvis"


def test_local_file_overrides_user_file(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    user.write_text('voice = "system"\nname = "Jarvis"\n')
    monkeypatch.setenv("COPILOT_CONFIG", str(user))
    (tmp_path / "copilot.toml").write_text('voice = "kokoro"\n')
    monkeypatch.chdir(tmp_path)
    s = load_settings(None)
    assert s["voice"] == "kokoro" and s["name"] == "Jarvis"


def test_missing_explicit_config_is_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        load_settings(str(tmp_path / "nope.toml"))


def test_no_files_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings(None)["voice"] == "auto"


def test_platform_paths(monkeypatch):
    monkeypatch.delenv("COPILOT_CONFIG")
    monkeypatch.setattr("sys.platform", "darwin")
    assert str(user_config()).endswith("Library/Application Support/meeting-copilot/copilot.toml")
