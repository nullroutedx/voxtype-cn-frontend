"""GTK integration checks. Uses a fixture backend; never touches real settings."""
import copy
import sys
import time
import unittest
from unittest.mock import patch

import app
from app import Adw, Gio, GLib
from backend import BackendError


class FixtureBackend:
    def __init__(self):
        self.download_cancel = __import__("threading").Event()
        self.config = {"engine": "sensevoice", "sensevoice.model": "sensevoice-small", "sensevoice.language": "auto", "audio.device": "default", "audio.max_duration_secs": 60, "audio.pause_media": True, "output.mode": "type"}
        self.phase = "idle"
        self.calls = []

    def snapshot(self):
        engines = {}
        for engine, model, *_ in app.MODELS:
            engines.setdefault(engine, {"models": []})["models"].append({"name": model, "download_arg": "sensevoice-small" if engine == "sensevoice" else model, "installed": engine == "sensevoice", "downloadable": True})
        return {"config": copy.deepcopy(self.config), "models": {"engines": engines}, "engines": [{"name": e, "compiled": True} for e in engines]}

    def status(self):
        return {"ActiveState": "active", "SubState": "running", "UnitFileState": "enabled"}, {"class": self.phase, "model": "sensevoice-small", "backend": "ONNX CPU (AVX2)"}

    def set_values(self, changes):
        self.calls.append(changes)
        self.config.update(changes)

    def control(self, action):
        self.calls.append(action)

    def data(self, *_):
        return [{"name": "default"}, {"name": "sysdefault:CARD=EarPods"}]


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.errors = []
        cls.old_hook = sys.excepthook
        sys.excepthook = lambda *error: cls.errors.append(error)
        cls.app = Adw.Application(application_id="io.github.voxtype.ControlCenter.Tests", flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    @classmethod
    def tearDownClass(cls):
        sys.excepthook = cls.old_hook

    def spin(self, until=lambda: True):
        deadline = time.monotonic() + 5
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if until():
                self.assertFalse(self.errors, self.errors)
                return
            time.sleep(0.01)
        self.fail("UI operation did not finish")

    def setUp(self):
        self.mock = patch.object(app, "Backend", FixtureBackend)
        self.mock.start()
        self.window = app.Window(self.app)
        self.window.present()
        self.spin(lambda: self.window.snapshot_data is not None and not self.window.busy and self.window.status_pair is not None)

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.mock.stop()
        self.spin()

    def test_three_pages_render_and_recording_disables_restart(self):
        for name in ("home", "models", "settings"):
            self.window.stack.set_visible_child_name(name)
            self.spin()
            self.assertEqual(self.window.stack.get_visible_child_name(), name)
        self.assertEqual(len(self.window.model_controls), 3)
        self.window.backend.phase = "recording"
        self.window.render_status(self.window.backend.status())
        self.assertFalse(self.window.start_button.get_sensitive())
        self.assertFalse(self.window.stop_button.get_sensitive())

    def test_restart_button_restarts_instead_of_starting(self):
        self.window.start_button.emit("clicked")
        self.spin(lambda: not self.window.busy)
        self.assertIn("restart", self.window.backend.calls)

    def test_settings_save_retains_selected_values(self):
        self.assertTrue(self.window.output.is_sensitive())
        self.assertTrue(self.window.duration.is_sensitive())
        self.assertTrue(self.window.device_button.is_sensitive())
        self.window.output.set_selected(1)
        self.window.language.set_selected(1)
        self.window.duration.set_value(120)
        self.assertTrue(self.window.dirty)
        self.window.save_button.emit("clicked")
        self.spin(lambda: not self.window.busy)
        self.assertEqual(self.window.backend.config["output.mode"], "clipboard")
        self.assertEqual(self.window.backend.config["sensevoice.language"], "zh")
        self.assertEqual(self.window.backend.config["audio.max_duration_secs"], 120)
        self.assertFalse(self.window.dirty)

    def test_device_discovery_preserves_current_selection(self):
        self.window.device_button.emit("clicked")
        self.spin(lambda: not self.window.busy)
        self.assertIn("sysdefault:CARD=EarPods", self.window.device_names)
        self.assertEqual(self.window.device_combo.get_selected(), 0)
        self.assertFalse(self.window.dirty)

    def test_error_is_shown_inline_and_controls_recover(self):
        def fail():
            raise BackendError("网络不可用")
        self.window.task("测试失败状态", fail)
        self.spin(lambda: not self.window.busy)
        self.assertTrue(self.window.error.get_visible())
        self.assertIn("网络不可用", self.window.error.get_text())
        self.assertTrue(self.window.start_button.get_sensitive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
