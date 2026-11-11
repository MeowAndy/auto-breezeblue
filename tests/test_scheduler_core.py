"""Offline scheduler tests. All command execution and scheduler paths are mocked."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bb_checkin as core


class CronInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bb-cron-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.patchers = [
            patch.object(core, "SCRIPT_DIR", self.folder / "app dir"),
            patch.object(core, "LOG_DIR", self.folder / "log dir"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self):
        return core._install_crontab("08", "05", "/usr/bin/python3",
                                     self.folder / "app dir" / "bb_checkin.py")

    def test_read_failure_never_overwrites_existing_table(self):
        for response in [(1, "permission denied"), (127, "command not found"),
                         (2, "cannot open crontab"), (1, "")]:
            with self.subTest(response=response), \
                 patch.object(core, "_run_quiet", return_value=response) as run:
                ok, message = self.install()
                self.assertFalse(ok)
                self.assertIn("读取", message)
                self.assertEqual(run.call_args_list[0].args[0], ["crontab", "-l"])
                self.assertEqual(run.call_count, 1)

    def test_write_failure_is_not_success(self):
        with patch.object(core, "_run_quiet", side_effect=[
            (0, "0 1 * * * unrelated-job"), (1, "disk full")
        ]) as run:
            ok, message = self.install()
        self.assertFalse(ok)
        self.assertIn("写入", message)
        self.assertEqual(run.call_count, 2)

    def test_success_requires_exact_readback_and_preserves_unrelated_jobs(self):
        original = "# unrelated\n0 1 * * * unrelated-job\n" + core.CRON_MARKER + " old\n"
        written = []

        def command(args, input_text=None):
            if args == ["crontab", "-"]:
                written.append(input_text)
                return 0, ""
            self.assertEqual(args, ["crontab", "-l"])
            return 0, written[0].rstrip("\n") if written else original

        with patch.object(core, "_run_quiet", side_effect=command):
            ok, message = self.install()
        self.assertTrue(ok, message)
        self.assertTrue(written[0].startswith("# unrelated\n0 1 * * * unrelated-job\n"))
        self.assertEqual(written[0].count(core.CRON_MARKER), 2)
        self.assertIn("5 8 * * * cd '", written[0])
        self.assertIn("@reboot cd '", written[0])
        self.assertNotIn(" old", written[0])
        self.assertTrue(core.LOG_DIR.is_dir())

    def test_no_previous_table_can_be_created(self):
        written = []

        def command(args, input_text=None):
            if args == ["crontab", "-"]:
                written.append(input_text)
                return 0, ""
            return (0, written[0].rstrip("\n")) if written else (1, "no crontab for tester")

        with patch.object(core, "_run_quiet", side_effect=command):
            self.assertTrue(self.install()[0])

    def test_readback_missing_or_changed_is_failure(self):
        for readback in [(0, ""), (1, "permission denied"),
                         (0, "0 1 * * * unrelated-job\n" + core.CRON_MARKER)]:
            with self.subTest(readback=readback), patch.object(
                core, "_run_quiet", side_effect=[(0, "0 1 * * * unrelated-job"),
                                                 (0, ""), readback]
            ):
                ok, message = self.install()
                self.assertFalse(ok)
                self.assertNotIn("已注册", message)

    def test_percent_in_path_is_escaped_for_cron(self):
        written = []

        def command(args, input_text=None):
            if args == ["crontab", "-"]:
                written.append(input_text)
                return 0, ""
            return 0, written[0].rstrip("\n") if written else ""

        with patch.object(core, "SCRIPT_DIR", self.folder / "100% app"), \
             patch.object(core, "_run_quiet", side_effect=command):
            self.assertTrue(self.install()[0])
        self.assertIn(r"100\% app", written[0])


class CronRemoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bb-remove-test-")
        self.addCleanup(self.temp.cleanup)
        self.patchers = [patch.object(core.Path, "home", return_value=Path(self.temp.name)),
                         patch.object(core.shutil, "which", return_value="/usr/bin/crontab")]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.original = "0 1 * * * unrelated-job\n5 8 * * * check-in " + core.CRON_MARKER

    def remove_with(self, responses):
        def command(args, input_text=None):
            if args[0] == "systemctl":
                return 1, "not installed"
            self.assertEqual(args[0], "crontab", "never run a real external command")
            return responses.pop(0)
        with patch.object(core, "_run_quiet", side_effect=command) as run:
            result = core._remove_linux()
        self.assertFalse(responses)
        return result, run

    def test_read_failure_is_reported_and_never_written(self):
        (ok, message), run = self.remove_with([(1, "permission denied")])
        self.assertFalse(ok)
        self.assertIn("读取", message)
        self.assertFalse(any(call.args[0] == ["crontab", "-"] for call in run.call_args_list))

    def test_failed_remove_write_is_reported(self):
        (ok, message), _run = self.remove_with([(0, self.original), (1, "permission denied")])
        self.assertFalse(ok)
        self.assertIn("移除失败", message)
        self.assertNotIn("已移除", message)

    def test_remove_must_verify_job_is_absent(self):
        (ok, message), _run = self.remove_with([(0, self.original), (0, ""), (0, self.original)])
        self.assertFalse(ok)
        self.assertIn("验证失败", message)

    def test_remove_readback_failure_is_not_success(self):
        (ok, _message), _run = self.remove_with([(0, self.original), (0, ""),
                                               (1, "permission denied")])
        self.assertFalse(ok)

    def test_remove_preserves_unrelated_jobs_and_verifies(self):
        (ok, message), run = self.remove_with([(0, self.original), (0, ""),
                                              (0, "0 1 * * * unrelated-job")])
        self.assertTrue(ok, message)
        writes = [call.kwargs["input_text"] for call in run.call_args_list
                  if call.args[0] == ["crontab", "-"]]
        self.assertEqual(writes, ["0 1 * * * unrelated-job\n"])

    def test_remove_detects_loss_of_unrelated_jobs(self):
        (ok, message), _run = self.remove_with([(0, self.original), (0, ""), (0, "")])
        self.assertFalse(ok)
        self.assertIn("验证失败", message)

    def test_remove_last_job_verifies_empty_table(self):
        (ok, message), run = self.remove_with([(0, "job " + core.CRON_MARKER),
                                              (0, ""), (1, "no crontab for tester")])
        self.assertTrue(ok, message)
        writes = [call.kwargs["input_text"] for call in run.call_args_list
                  if call.args[0] == ["crontab", "-"]]
        self.assertEqual(writes, [""])

    def test_no_crontab_is_already_removed(self):
        (ok, message), _run = self.remove_with([(1, "no crontab for tester")])
        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
