import pytest
from pathlib import Path
import json

# We need to mock the settings file path specifically
from unittest.mock import patch
import agent.settings as settings

@pytest.fixture(autouse=True)
def setup_mock_settings(tmp_path):
    """Fixture to ensure each test uses a fresh, isolated settings directory."""
    # Create a temporary settings folder for this test
    test_config_dir = tmp_path / ".local-code-agent"
    test_config_dir.mkdir()
    test_settings_file = test_config_dir / "settings.json"

    # Patch SETTINGS_FILE in the agent.settings module directly
    with patch("agent.settings.SETTINGS_FILE", test_settings_file):
        yield test_settings_file

def test_settings_load_defaults(setup_mock_settings):
    # Test that it loads defaults when file doesn't exist
    config = settings.load()
    assert config["theme"] == "auto"
    assert config["font_chat"] == 14
    assert config["font_ui"] == 13

def test_settings_save_and_load(setup_mock_settings):
    # Test round-trip save and load
    test_data = {
        "theme": "light",
        "font_chat": 18,
        "font_ui": 15
    }
    settings.save(test_data)

    loaded = settings.load()
    assert loaded["theme"] == "light"
    assert loaded["font_chat"] == 18
    assert loaded["font_ui"] == 15

def test_settings_fallback_on_invalid_json(setup_mock_settings):
    # Test fallback on corrupted JSON
    settings_file = setup_mock_settings
    with open(settings_file, "w") as f:
        f.write("invalid json{")

    config = settings.load()
    assert config["theme"] == "auto" # Default value

def test_settings_fallback_on_missing_keys(setup_mock_settings):
    # Test fallback for missing keys in an existing file
    settings_file = setup_mock_settings
    with open(settings_file, "w") as f:
        json.dump({"theme": "dark"}, f) # missing font keys

    config = settings.load()
    assert config["theme"] == "dark"
    assert config["font_chat"] == 14 # Default value
