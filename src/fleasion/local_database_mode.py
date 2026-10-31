"""Startup migration for the local-database-first community fork."""

from __future__ import annotations

from typing import Any

DEFAULT_DATABASE_MODE_SETTINGS = {
    "proxy_features_enabled": False,
    "clear_cache_on_launch": False,
    "auto_delete_cache_on_exit": False,
}


def migrate_local_database_mode(config_manager: Any) -> bool:
    """Disable proxy interception and automatic cache deletion once in this fork."""
    if config_manager.settings.get("local_database_mode_migration_v1_complete", False):
        return False

    config_manager.proxy_features_enabled = False
    config_manager.clear_cache_on_launch = False
    config_manager.auto_delete_cache_on_exit = False
    config_manager.settings["local_database_mode_migration_v1_complete"] = True
    config_manager._save_settings()
    return True
