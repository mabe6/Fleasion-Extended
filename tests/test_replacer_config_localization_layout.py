import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from fleasion.config import manager as manager_module
from fleasion.config.manager import ConfigManager
from fleasion.gui.replacer_config import ReplacerConfigWindow
from fleasion.localization import DEFAULT_LANGUAGE, available_languages, get_language, set_language, tr


_APP = None
_TRANSLATED_LANGUAGES = [code for code, _name in available_languages() if code != DEFAULT_LANGUAGE]


def _qapp():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.mark.parametrize('language', _TRANSLATED_LANGUAGES)
def test_translated_replacer_controls_do_not_clip_text(tmp_path, monkeypatch, language):
    app = _qapp()
    config_dir = Path(tmp_path) / 'FleasionNT'
    configs_dir = config_dir / 'configs'
    monkeypatch.setattr(manager_module, 'CONFIG_DIR', config_dir)
    monkeypatch.setattr(manager_module, 'CONFIG_FILE', config_dir / 'settings.json')
    monkeypatch.setattr(manager_module, 'CONFIGS_FOLDER', configs_dir)

    previous_language = get_language()
    set_language(language)
    window = None
    try:
        window = ReplacerConfigWindow(ConfigManager())
        window.resize(900, 750)
        window.show()
        app.processEvents()

        clipped = []
        for label in window.findChildren(QLabel):
            if label.isVisible() and label.text() and label.sizeHint().width() > label.width():
                clipped.append(('label', label.text(), label.width(), label.sizeHint().width()))

        for button in window.findChildren(QPushButton):
            if not button.isVisible() or not button.text() or button.text() == '?':
                continue
            if button.sizeHint().width() > button.width():
                clipped.append(('button', button.text(), button.width(), button.sizeHint().width()))

        for line_edit in window.findChildren(QLineEdit):
            placeholder = line_edit.placeholderText()
            if not line_edit.isVisible() or not placeholder:
                continue
            required_width = line_edit.fontMetrics().horizontalAdvance(placeholder) + 12
            if required_width > line_edit.width():
                clipped.append(('line edit', placeholder, line_edit.width(), required_width))

        assert clipped == []
    finally:
        if window is not None:
            window.enabled_menu.hide()
            window.close()
        set_language(previous_language)
        app.processEvents()



def test_dashboard_restores_fleasion_workflows_and_separates_advanced_sql(
    tmp_path, monkeypatch
):
    app = _qapp()
    config_dir = Path(tmp_path) / 'FleasionExtended'
    configs_dir = config_dir / 'configs'
    monkeypatch.setattr(manager_module, 'CONFIG_DIR', config_dir)
    monkeypatch.setattr(manager_module, 'CONFIG_FILE', config_dir / 'settings.json')
    monkeypatch.setattr(manager_module, 'CONFIGS_FOLDER', configs_dir)

    window = None
    try:
        window = ReplacerConfigWindow(ConfigManager())
        expected_tabs = [
            tr('ui.gui.replacer_config.replacer'),
            'Scraper DB',
            tr('ui.gui.replacer_config.subplace_joiner'),
            tr('ui.gui.replacer_config.miscellaneous'),
            tr('ui.gui.replacer_config.settings'),
            'Advanced SQL',
        ]
        actual_tabs = [
            window.tab_widget.tabText(index)
            for index in range(window.tab_widget.count())
        ]

        assert actual_tabs == expected_tabs
        assert window._scraper_db_tab is window._asset_explorer_tab
        assert window._advanced_sql_tab is window._sqlite_browser_tab
        assert window.tab_widget.indexOf(window._replacer_tab) == 0
        assert window.tab_widget.tabText(2) == tr('ui.gui.replacer_config.subplace_joiner')
        assert window.tab_widget.currentWidget() is window._replacer_tab
        assert not hasattr(window, '_local_db_tab')
        assert not hasattr(window, '_proxy_traffic_tab')
        assert [
            window._replacer_modes.tabText(index)
            for index in range(window._replacer_modes.count())
         ] == ['Cache Hash', 'Asset ID (legacy)']
        assert window._hash_replacer_tab.asset_store.root == config_dir / 'HashReplacer'
        assert window._rando_stuff_tab._hash_asset_store is window._hash_replacer_store
        assert not hasattr(window, '_hidden_replacer_tab')
    finally:
        if window is not None:
            enabled_menu = getattr(window, 'enabled_menu', None)
            if enabled_menu is not None:
                enabled_menu.hide()
            window.close()
        app.processEvents()
