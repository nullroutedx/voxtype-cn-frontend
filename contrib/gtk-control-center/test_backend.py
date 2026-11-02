import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend import Backend, BackendError


class FakeBackend(Backend):
    def __init__(self, path):
        super().__init__(config_path=path)
        self.state = "idle"
        self.running = True
        self.meeting = "No meeting currently in progress."
        self.calls = []
        self.fail_key = None
        self.model_installed = True
        self.compiled = True
        self.ready_error = False

    def status(self):
        return {"ActiveState": "active" if self.running else "inactive"}, {"class": self.state}

    def vox(self, *args, **kwargs):
        self.calls.append(args)
        if args == ("meeting", "status"):
            return self.meeting
        if args[:2] == ("config", "set"):
            if args[2] == self.fail_key:
                raise BackendError("invalid config")
            with self.config_path.open("a") as f:
                f.write(f"\n# {args[2]}={args[3]}")
        return ""

    def run(self, args, timeout=15):
        self.calls.append(tuple(args))
        return ""

    def wait_ready(self):
        if self.ready_error:
            raise BackendError("model load failed")

    def snapshot(self):
        return {"engines": [{"name": "sensevoice", "compiled": self.compiled}], "models": {"engines": {"sensevoice": {"models": [{"name": "small", "download_arg": "sensevoice-small", "installed": self.model_installed}]}}}}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "config.toml"
        self.original = b'# preserve comments\nengine = "sensevoice"\n'
        self.path.write_bytes(self.original)
        self.backend = FakeBackend(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_recording_and_meetings_block_mutations(self):
        for state in ("recording", "transcribing", "streaming"):
            self.backend.state = state
            with self.assertRaises(BackendError):
                self.backend.set_values({"output.mode": "clipboard"})
        self.backend.state = "idle"
        self.backend.meeting = "Meeting in progress: project review"
        with self.assertRaises(BackendError):
            self.backend.control("stop")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(any(call[:2] == ("config", "set") for call in self.backend.calls))

    def test_partial_config_failure_restores_exact_bytes(self):
        self.backend.fail_key = "engine"
        with self.assertRaises(BackendError):
            self.backend.set_values({"sensevoice.model": "small", "engine": "sensevoice"})
        self.assertEqual(self.path.read_bytes(), self.original)
        backups = list(self.path.parent.glob("*.bak.gui-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)

    def test_model_load_failure_restores_config_and_restarts_old_service(self):
        self.backend.ready_error = True
        with self.assertRaises(BackendError):
            self.backend.set_values({"output.mode": "clipboard"})
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.backend.calls.count(("systemctl", "--user", "restart", "voxtype.service")), 2)

    def test_stopped_service_stays_stopped_after_save(self):
        self.backend.running = False
        self.backend.set_values({"output.mode": "clipboard"})
        self.assertFalse(any(call[0] == "systemctl" for call in self.backend.calls))

    def test_activation_changes_engine_and_model_together(self):
        self.backend.activate("sensevoice", "small")
        self.assertIn(("config", "set", "engine", "sensevoice"), self.backend.calls)
        self.assertIn(("config", "set", "sensevoice.model", "sensevoice-small"), self.backend.calls)

    def test_missing_model_or_backend_never_changes_config(self):
        self.backend.model_installed = False
        with self.assertRaises(BackendError):
            self.backend.activate("sensevoice", "small")
        self.backend.model_installed = True
        self.backend.compiled = False
        with self.assertRaises(BackendError):
            self.backend.activate("sensevoice", "small")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_download_is_separate_from_activation_and_cancellable(self):
        # A real subprocess that blocks simulates a stalled downloader. This
        # checks cancellation even when no progress lines are being emitted.
        executable = self.path.parent / "fake-voxtype"
        trace = self.path.parent / "args.json"
        executable.write_text("#!/usr/bin/python\nimport sys,time,json\nfrom pathlib import Path\n" + f"Path({str(trace)!r}).write_text(json.dumps(sys.argv))\n" + "print('{\"event\":\"progress\",\"pct\":10}',flush=True)\ntime.sleep(60)\n")
        executable.chmod(0o755)
        backend = Backend(binary=str(executable), config_path=self.path)
        events, errors = [], []
        def download():
            try:
                backend.download("sensevoice-small", events.append)
            except BackendError as exc:
                errors.append(str(exc))
        with patch.object(backend, "data", return_value={"engines": {"sensevoice": {"models": [{"download_arg": "sensevoice-small", "downloadable": True}]}}}):
            thread = threading.Thread(target=download, daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while not events and time.monotonic() < deadline:
                time.sleep(0.02)
            backend.download_cancel.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(events)
        self.assertIn("取消", errors[0])
        self.assertNotIn("--activate", json.loads(trace.read_text()))
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
