"""Check credential setup without starting nginx, Python services or Docker."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ViewerAuthSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="viewer-auth-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.secrets = self.work / "secrets"
        self.services = self.work / "services.log"
        binaries = self.work / "bin"
        binaries.mkdir()
        for name in ("nginx", "python3", "tail"):
            stub = binaries / name
            stub.write_text(
                '#!/bin/sh\n'
                f'printf "%s\\n" "{name} $*" >> "$TEST_SERVICE_LOG"\n'
            )
            stub.chmod(0o755)
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("VIEWER_") and key != "STREAM_KEY"
        }
        self.env["PATH"] = str(binaries) + os.pathsep + self.env["PATH"]
        self.env["TEST_SERVICE_LOG"] = str(self.services)
        # Redirect only the container's secrets directory into the test sandbox.
        startup = (ROOT / "start.sh").read_text().replace(
            "dir=/etc/nginx/secrets", "dir=" + shlex.quote(str(self.secrets))
        )
        self.startup = self.work / "start.sh"
        self.startup.write_text(startup)

    def start(self, **credentials):
        return subprocess.run(
            ["bash", str(self.startup)], env={**self.env, **credentials},
            capture_output=True, text=True, timeout=10,
        )

    def assert_started(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nginx -t", self.services.read_text())

    def assert_password(self, username, password):
        stored_user, stored_hash = (self.secrets / ".htpasswd").read_text().strip().split(":", 1)
        self.assertEqual(stored_user, username)
        self.assertTrue(stored_hash.startswith("$6$"))
        salt = stored_hash.split("$")[2]
        expected = subprocess.run(
            ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
            input=password + "\n", capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(stored_hash, expected)

    def test_viewer_password_works_without_publishing_credentials(self):
        self.assert_started(self.start(VIEWER_PASSWORD="test-viewer-password"))
        self.assert_password("viewer", "test-viewer-password")

    def test_missing_viewer_password_stops_before_services(self):
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing viewer password", result.stderr)
        self.assertFalse(self.services.exists())

    def test_empty_mounted_password_file_stops_before_services(self):
        self.secrets.mkdir()
        (self.secrets / ".htpasswd").touch()
        self.assertNotEqual(self.start().returncode, 0)
        self.assertFalse(self.services.exists())

    def test_password_hash_works_without_publishing_credentials(self):
        hashed = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"], input="hashed-viewer-password\n",
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assert_started(self.start(VIEWER_USER="audience", VIEWER_PASSWORD_HASH=hashed))
        self.assert_password("audience", "hashed-viewer-password")

    def test_mounted_password_file_works_without_env_credentials(self):
        self.secrets.mkdir()
        entry = "viewer:$6$test$existing-hash\n"
        (self.secrets / ".htpasswd").write_text(entry)
        self.assert_started(self.start())
        self.assertEqual((self.secrets / ".htpasswd").read_text(), entry)

    def test_legacy_stream_key_does_not_affect_viewer_setup(self):
        self.assert_started(self.start(
            VIEWER_PASSWORD="test-viewer-password", STREAM_KEY="unused/key?pwd=old",
        ))
        self.assert_password("viewer", "test-viewer-password")

    def test_secrets_helper_creates_viewer_credentials(self):
        result = subprocess.run(
            ["sh", str(ROOT / "make-secrets.sh"), "audience"],
            cwd=self.work, input="helper-viewer-password\n", env=self.env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_password("audience", "helper-viewer-password")
        self.assertFalse((self.secrets / "publish_key.conf").exists())


if __name__ == "__main__":
    unittest.main()
