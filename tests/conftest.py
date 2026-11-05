import pytest


@pytest.fixture(autouse=True)
def _no_user_config(tmp_path_factory, monkeypatch):
    """Tests never read the real per-user copilot.toml."""
    monkeypatch.setenv("COPILOT_CONFIG", str(tmp_path_factory.mktemp("cfg") / "copilot.toml"))
