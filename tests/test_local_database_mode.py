"""Tests for the fork's local-database-first startup defaults."""

from types import SimpleNamespace

from fleasion.local_database_mode import migrate_local_database_mode as _migrate_local_database_mode
from fleasion.local_database_mode import DEFAULT_DATABASE_MODE_SETTINGS


def test_new_installs_default_to_local_database_mode():
    assert DEFAULT_DATABASE_MODE_SETTINGS["proxy_features_enabled"] is False
    assert DEFAULT_DATABASE_MODE_SETTINGS["clear_cache_on_launch"] is False
    assert DEFAULT_DATABASE_MODE_SETTINGS["auto_delete_cache_on_exit"] is False


def test_existing_configuration_is_migrated_once_to_local_database_mode():
    saves = []
    config = SimpleNamespace(
        settings={"proxy_features_enabled": True},
        proxy_features_enabled=True,
        clear_cache_on_launch=True,
        auto_delete_cache_on_exit=True,
        _save_settings=lambda: saves.append(True),
    )

    assert _migrate_local_database_mode(config) is True
    assert config.proxy_features_enabled is False
    assert config.clear_cache_on_launch is False
    assert config.auto_delete_cache_on_exit is False
    assert config.settings["local_database_mode_migration_v1_complete"] is True
    assert saves == [True]

    # Once migrated, the user can manually re-enable an optional proxy mode.
    config.proxy_features_enabled = True
    assert _migrate_local_database_mode(config) is False
    assert config.proxy_features_enabled is True
