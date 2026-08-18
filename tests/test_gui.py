import tkinter as tk
from pathlib import Path

from mongodb_migrate import gui
from mongodb_migrate.gui import MongoMigrateApp


def test_gui_does_not_override_tk_internal_options_method():
    # Tk calls Misc._options while creating/configuring widgets. Accidentally
    # reusing this private name makes the app exit before the first frame.
    assert MongoMigrateApp._options is tk.Misc._options


def test_windows_app_data_uses_local_appdata(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\demo\AppData\Local")
    assert gui.default_app_data_dir() == Path(
        r"C:\Users\demo\AppData\Local"
    ) / "MongoDB Migrate"
