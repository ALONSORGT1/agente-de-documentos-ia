import pytest

from app.core.config import Settings, env_bool


def test_settings_read_environment_at_instantiation(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "modelo-prueba")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test")
    monkeypatch.setenv("LOCAL_ADMIN_KEY", "admin-test")
    config = Settings()
    assert config.openai_model == "modelo-prueba"
    assert "secret-test" not in repr(config) and "admin-test" not in repr(config)


def test_folder_validation(monkeypatch):
    assert Settings(drive_folder_id="folder_authorized_123").checks()["drive_folder"]
    for bad in ("", "abc", "folder' or trashed=true", "https://drive.google.com/folders/123"):
        assert not Settings(drive_folder_id=bad).checks()["drive_folder"]


def test_config_limits_and_boolean(monkeypatch):
    with pytest.raises(ValueError):
        Settings(openai_max_retries=99)
    with pytest.raises(ValueError):
        Settings(api_timeout_seconds=float("nan"))
    monkeypatch.setenv("TEST_FLAG", "false")
    assert env_bool("TEST_FLAG") is False
    monkeypatch.setenv("TEST_FLAG", "perhaps")
    with pytest.raises(ValueError):
        env_bool("TEST_FLAG")
