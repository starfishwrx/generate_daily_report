from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from autodatareport.process_runner import TaskProcessRunner


class TaskProcessRunnerTests(unittest.TestCase):
    def test_windows_open_creates_a_new_process_group(self) -> None:
        with mock.patch("autodatareport.process_runner.sys.platform", "win32"):
            with mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True):
                with mock.patch("autodatareport.process_runner.subprocess.Popen") as popen:
                    TaskProcessRunner().open(["python", "job.py"], cwd=Path("C:/work"), env={})
        self.assertEqual(popen.call_args.kwargs["creationflags"], 512)

    def test_windows_terminate_kills_the_process_tree(self) -> None:
        process = mock.Mock()
        process.pid = 4321
        process.poll.return_value = None
        with mock.patch("autodatareport.process_runner.sys.platform", "win32"):
            with mock.patch(
                "autodatareport.process_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                TaskProcessRunner.terminate(process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "4321", "/T", "/F"])
        process.terminate.assert_not_called()

    def test_windows_terminate_falls_back_when_taskkill_fails(self) -> None:
        process = mock.Mock()
        process.pid = 4321
        process.poll.return_value = None
        with mock.patch("autodatareport.process_runner.sys.platform", "win32"):
            with mock.patch("autodatareport.process_runner.subprocess.run", side_effect=OSError("missing")):
                TaskProcessRunner.terminate(process)
        process.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
