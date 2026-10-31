"""Tests for dashboard tab visibility while HTTP proxy support is unavailable."""

from __future__ import annotations

import os
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication, QTabWidget, QWidget

from fleasion.gui.settings_tab import SettingsTab

import fleasion.gui.replacer_config as replacer_config


def test_http_proxy_dependent_tabs_are_hidden_until_support_is_available(monkeypatch) -> None:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    app = QApplication.instance() or QApplication([])
    tabs = QTabWidget()
    replacer = QWidget()
    scraper = QWidget()
    local_db = QWidget()
    settings = QWidget()
    tabs.addTab(replacer, 'Replacer')
    tabs.addTab(scraper, 'Scraper')
    tabs.addTab(local_db, 'Local DB')
    tabs.addTab(settings, 'Settings')

    proxy_dependent = [replacer, scraper]
    monkeypatch.setattr(replacer_config, 'HTTP_PROXY_SUPPORT_AVAILABLE', False)
    replacer_config._sync_http_proxy_tab_visibility(tabs, proxy_dependent)

    assert not tabs.isTabVisible(tabs.indexOf(replacer))
    assert not tabs.isTabVisible(tabs.indexOf(scraper))
    assert tabs.isTabVisible(tabs.indexOf(local_db))
    assert tabs.isTabVisible(tabs.indexOf(settings))

    # The same helper can reveal them once the HTTP proxy implementation lands.
    monkeypatch.setattr(replacer_config, 'HTTP_PROXY_SUPPORT_AVAILABLE', True)
    replacer_config._sync_http_proxy_tab_visibility(tabs, proxy_dependent)
    assert tabs.isTabVisible(tabs.indexOf(replacer))
    assert tabs.isTabVisible(tabs.indexOf(scraper))

    tabs.close()
    del app



def test_manual_proxy_credential_timer_is_safe_when_proxy_controls_are_hidden() -> None:
    class FakeTimer:
        def __init__(self):
            self.stopped = 0
            self.started = 0

        def stop(self):
            self.stopped += 1

        def start(self):
            self.started += 1

    timer = FakeTimer()
    # SettingsTab does not build the proxy transport widgets in this build.
    state = SimpleNamespace(
        _manual_proxy_credentials_timer=timer,
        _selected_manual_proxy_has_credentials=lambda: SettingsTab._selected_manual_proxy_has_credentials(SimpleNamespace()),
    )

    assert state._selected_manual_proxy_has_credentials() is True
    SettingsTab._sync_manual_proxy_credentials_timer(state)

    assert timer.stopped == 1
    assert timer.started == 0
