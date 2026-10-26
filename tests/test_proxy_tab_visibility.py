"""Tests for dashboard tab visibility while HTTP proxy support is unavailable."""

from __future__ import annotations

import os

from PyQt6.QtWidgets import QApplication, QTabWidget, QWidget

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
