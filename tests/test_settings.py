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


def test_custom_name_drops_claudes_mishearings(tmp_path, monkeypatch):
    from meeting_copilot.cli import matcher
    user = tmp_path / "user.toml"
    user.write_text('name = "Jarvis"\n')
    monkeypatch.setenv("COPILOT_CONFIG", str(user))
    monkeypatch.chdir(tmp_path)
    m = matcher(load_settings(None))
    assert m("Jarvis, what time is it?")
    assert not m("the cloud bill went up") and not m("call me later")
    assert matcher({**load_settings(None), "name": "Claude"})("cloud, what time is it?")


def test_custom_name_keeps_its_own_variants(tmp_path, monkeypatch):
    from meeting_copilot.cli import matcher
    user = tmp_path / "user.toml"
    user.write_text('name = "Jarvis"\nvariants = ["jarvas"]\n')
    monkeypatch.setenv("COPILOT_CONFIG", str(user))
    monkeypatch.chdir(tmp_path)
    assert matcher(load_settings(None))("jarvas, mute")
