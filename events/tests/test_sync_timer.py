"""Tests for the Elastic Beanstalk hook that schedules the hourly sync."""

import getpass
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

PLATFORM = Path(settings.BASE_DIR) / ".platform"
INSTALL_SCRIPT = PLATFORM / "sync" / "install_sync_timer.sh"
HOOKS = [
    PLATFORM / "hooks" / "postdeploy" / "02_sync_timer.sh",
    PLATFORM / "confighooks" / "postdeploy" / "02_sync_timer.sh",
]


class SyncTimerInstallTests(SimpleTestCase):
    """Run the install script against temporary folders and a fake systemctl."""

    def setUp(self):
        """Create temporary system folders and a systemctl that logs its calls."""
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.systemd_dir = self.tmp / "systemd"
        self.systemd_dir.mkdir()
        self.calls = self.tmp / "calls.txt"
        self.systemctl = self.fake_systemctl(exit_code=0)

    def fake_systemctl(self, exit_code):
        """Write a stand-in systemctl that records its arguments."""
        path = self.tmp / f"systemctl-{exit_code}"
        path.write_text(f'#!/bin/bash\necho "$*" >> "{self.calls}"\nexit {exit_code}\n')
        path.chmod(0o755)
        return path

    def run_script(self, script=INSTALL_SCRIPT, **overrides):
        """Run ``script`` with every system path pointed at the temp folder."""
        env = {
            **os.environ,
            "SYSTEMD_DIR": str(self.systemd_dir),
            "LOGTASKS_DIR": str(self.tmp / "logtasks"),
            "SYSTEMCTL": str(self.systemctl),
            "APP_USER": getpass.getuser(),
            "LOG_FILE": str(self.tmp / "team5_sync.log"),
            **overrides,
        }
        return subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True
        )

    def test_installs_hourly_timer_and_service(self):
        """The timer fires on the hour and runs sync_events as the app user."""
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Hourly sync scheduled", result.stdout)

        timer = (self.systemd_dir / "team5-sync.timer").read_text()
        self.assertIn("OnCalendar=*-*-* *:00:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

        service = (self.systemd_dir / "team5-sync.service").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn(f"User={getpass.getuser()}", service)
        self.assertIn("WorkingDirectory=/var/app/current", service)
        self.assertIn("EnvironmentFile=-/opt/elasticbeanstalk/deployment/env", service)
        self.assertIn("python3 manage.py sync_events", service)
        self.assertIn(f"StandardOutput=append:{self.tmp}/team5_sync.log", service)

        self.assertEqual(
            self.calls.read_text().splitlines(),
            ["daemon-reload", "enable --now team5-sync.timer"],
        )

    def test_log_is_created_and_added_to_eb_logs(self):
        """The log file exists and both tail and bundle logs include it."""
        self.run_script()
        log_file = self.tmp / "team5_sync.log"
        self.assertTrue(log_file.exists())
        for task in ("tail", "bundle"):
            conf = self.tmp / "logtasks" / task / "team5-sync.conf"
            self.assertEqual(conf.read_text().strip(), str(log_file))

    def test_running_twice_is_harmless(self):
        """Every deploy reruns the script, which rewrites the same units."""
        self.run_script()
        first = (self.systemd_dir / "team5-sync.timer").read_text()
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.systemd_dir / "team5-sync.timer").read_text(), first)

    def test_systemctl_failure_does_not_fail_the_deploy(self):
        """If systemd refuses the timer, the script explains and exits 0."""
        result = self.run_script(SYSTEMCTL=str(self.fake_systemctl(exit_code=1)))
        self.assertEqual(result.returncode, 0)
        self.assertIn("Could not enable team5-sync.timer", result.stdout)

    def test_unwritable_systemd_dir_does_not_fail_the_deploy(self):
        """If the unit files can't be written, nothing is enabled."""
        result = self.run_script(SYSTEMD_DIR=str(self.tmp / "missing"))
        self.assertEqual(result.returncode, 0)
        self.assertIn("Could not write the systemd units", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_hooks_run_the_install_script(self):
        """Deploy and config-change hooks are executable and call the script."""
        for hook in HOOKS:
            with self.subTest(hook=hook.parent.parent.name):
                self.assertTrue(hook.stat().st_mode & stat.S_IXUSR)
                self.assertIn("sync/install_sync_timer.sh", hook.read_text())
                result = self.run_script(script=hook)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.systemd_dir / "team5-sync.timer").exists())
        self.assertTrue(INSTALL_SCRIPT.stat().st_mode & stat.S_IXUSR)
