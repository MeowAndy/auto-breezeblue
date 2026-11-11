"""Offline batch and configuration regression tests; never call the live API."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bb_checkin as core


class BatchCheckinTests(unittest.TestCase):
    def test_account_exception_does_not_abort_remaining_accounts(self):
        accounts = [core.new_account(id=str(i), cookie="session=secret-cookie",
                                     password="secret-password") for i in range(3)]
        logs = []
        with patch.object(core, "get_accounts", return_value=accounts), \
             patch.object(core, "log", side_effect=logs.append), \
             patch.object(core, "update_account") as update, \
             patch.object(core, "run_checkin_account", side_effect=[
                 TimeoutError("secret-password session=secret-cookie"),
                 {"ok": True, "kind": "claimed"},
                 {"ok": True, "kind": "already"},
             ]) as run:
            result = core.run_checkin_all()
        self.assertEqual(run.call_count, 3)
        self.assertFalse(result["ok"])
        self.assertEqual([r["kind"] for r in result["results"]],
                         ["error", "claimed", "already"])
        self.assertIn("成功/已签 2,失败 1", result["message"])
        update.assert_called_once()
        emitted = repr(result) + repr(logs) + repr(update.call_args)
        self.assertNotIn("secret-password", emitted)
        self.assertNotIn("secret-cookie", emitted)

    def test_result_persistence_error_does_not_abort_batch(self):
        accounts = [core.new_account(id=str(i), cookie="secret-cookie") for i in range(2)]
        with patch.object(core, "get_accounts", return_value=accounts), \
             patch.object(core, "log") as log, \
             patch.object(core, "update_account", side_effect=OSError("secret-cookie")), \
             patch.object(core, "run_checkin_account", side_effect=[
                 ValueError("secret-cookie"), {"ok": True, "kind": "claimed"}
             ]) as run:
            result = core.run_checkin_all()
        self.assertEqual(run.call_count, 2)
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-cookie", repr(result) + repr(log.call_args_list))

    def test_disabled_accounts_are_not_run(self):
        with patch.object(core, "get_accounts", return_value=[{"enabled": False}]), \
             patch.object(core, "log"), patch.object(core, "run_checkin_account") as run:
            self.assertEqual(core.run_checkin_all()["kind"], "unconfigured")
        run.assert_not_called()


# Each worker has its own module and thread lock, as cron and web processes do.
# Pause the first worker immediately after its mocked config read; the second
# must not read/write a stale snapshot while that transaction is still open.
WORKER = r'''
import json, pathlib, sys, time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import bb_checkin as core
core.CONFIG_PATH = pathlib.Path(sys.argv[2])
role, signals = sys.argv[3], pathlib.Path(sys.argv[4])
original_load = core.load_config

def paused_load():
    cfg = original_load()
    if role == "first":
        (signals / "first_read").touch()
        deadline = time.monotonic() + 15
        while not (signals / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("test release timeout")
            time.sleep(0.02)
    return cfg

(signals / (role + "_started")).touch()
with patch.object(core, "load_config", side_effect=paused_load):
    if role == "first":
        core.update_account("a", last_result={"ok": True})
    else:
        core.update_account("b", cookie="refreshed-cookie")
'''


class ConfigConcurrencyTests(unittest.TestCase):
    def wait_for(self, marker):
        deadline = time.monotonic() + 10
        while not marker.exists():
            if time.monotonic() > deadline:
                self.fail("worker did not reach synchronization point")
            time.sleep(0.02)

    def test_two_process_updates_preserve_both_accounts(self):
        with tempfile.TemporaryDirectory(prefix="bb-config-test-") as temp:
            folder = Path(temp)
            config = folder / "config.json"
            config.write_text(json.dumps({"version": 2, "accounts": [
                {"id": "a"}, {"id": "b", "cookie": "old-cookie"}
            ]}), encoding="utf-8")
            command = [sys.executable, "-I", "-c", WORKER, str(ROOT), str(config)]
            workers = []
            try:
                workers.append(subprocess.Popen(command + ["first", temp],
                                                cwd=tempfile.gettempdir(),
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                self.wait_for(folder / "first_read")
                workers.append(subprocess.Popen(command + ["second", temp],
                                                cwd=tempfile.gettempdir(),
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                self.wait_for(folder / "second_started")
                time.sleep(0.5)
                second_blocked = workers[1].poll() is None
            finally:
                (folder / "release").touch()
                for worker in workers:
                    try:
                        stdout, stderr = worker.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.communicate()
                        raise
                    self.assertEqual(worker.returncode, 0, stderr.decode(errors="replace"))
            accounts = json.loads(config.read_text(encoding="utf-8"))["accounts"]
            self.assertEqual(accounts[1]["cookie"], "refreshed-cookie",
                             "stale first-process write lost the second account update")
            self.assertTrue(accounts[0]["last_result"]["ok"])
            self.assertTrue(second_blocked, "other process entered an active config transaction")

    def test_invalid_config_is_not_silently_rebuilt(self):
        for contents in ('{"version": 2, "secret-cookie": ', '[]',
                         '{"version": 2, "accounts": null}'):
            with self.subTest(contents=contents), \
                 tempfile.TemporaryDirectory(prefix="bb-config-test-") as temp:
                config = Path(temp) / "config.json"
                config.write_text(contents, encoding="utf-8")
                with patch.object(core, "CONFIG_PATH", config):
                    with self.assertRaises(ValueError) as error:
                        core.add_account(cookie="new-cookie")
                self.assertNotIn("secret-cookie", str(error.exception))
                self.assertEqual(config.read_text(encoding="utf-8"), contents)

    def test_failed_atomic_replace_keeps_previous_config(self):
        with tempfile.TemporaryDirectory(prefix="bb-config-test-") as temp:
            config = Path(temp) / "config.json"
            original = '{"version": 2, "accounts": []}'
            config.write_text(original, encoding="utf-8")
            with patch.object(core, "CONFIG_PATH", config), \
                 patch.object(core.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    core.save_config({"version": 2, "accounts": [{"id": "a"}]})
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse(list(Path(temp).glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
