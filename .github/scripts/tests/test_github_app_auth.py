# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "app_auth", Path(__file__).parents[1] / "github_app_auth.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Authentication(unittest.TestCase):
    def test_refresh_and_scope(self):
        with m.GitHubApp("123", "test-key", "owner/repo") as auth:
            responses = [
                {"id": 42},
                {"token": "token-one", "expires_at": "2030-01-01T01:00:00Z"},
                {"id": 42},
                {"token": "token-two", "expires_at": "2030-01-01T02:00:00Z"},
                {},
                {},
            ]
            with (
                patch.object(auth, "jwt", return_value="jwt"),
                patch.object(auth, "request", side_effect=responses) as request,
                patch.object(m.time, "time", return_value=1893456000) as now,
            ):
                self.assertEqual(auth.token(), "token-one")
                self.assertEqual(auth.token(), "token-one")
                self.assertEqual(request.call_count, 2)
                self.assertEqual(
                    request.call_args.args[3],
                    {"repositories": ["repo"], "permissions": m.PERMISSIONS},
                )
                now.return_value += 3100
                self.assertEqual(auth.token(), "token-two")
                self.assertIn(
                    unittest.mock.call("DELETE", "/installation/token", "token-one"),
                    request.call_args_list,
                )
                auth.close()
                self.assertIn(
                    unittest.mock.call("DELETE", "/installation/token", "token-two"),
                    request.call_args_list,
                )
            self.assertFalse(auth.key_path.exists())

    def test_private_key_file_and_cleanup(self):
        with m.GitHubApp("123", "test-key", "owner/repo") as auth:
            key = auth.key_path
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(key.exists())

    def test_subprocess_environment_excludes_credentials(self):
        secret_env = {
            "PATH": "/bin",
            "GH_TOKEN": "old",
            "GITHUB_TOKEN": "other",
            "CODEX_REPAIR_APP_PRIVATE_KEY": "private",
            "CODEX_AUTH_JSON": "login",
        }
        with patch.dict(m.os.environ, secret_env, clear=True):
            self.assertEqual(m.clean_environment(), {"PATH": "/bin"})

    def test_no_token_on_mint_failure(self):
        with m.GitHubApp("123", "test-key", "owner/repo") as auth:
            with (
                patch.object(auth, "jwt", return_value="jwt"),
                patch.object(
                    auth, "request", side_effect=RuntimeError("request failed")
                ),
            ):
                with self.assertRaises(RuntimeError):
                    auth.token()
            self.assertIsNone(auth.current_token)

    def test_real_rs256_signature(self):
        import base64
        import json
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory, "key.pem")
            public = Path(directory, "public.pem")
            subprocess.run(
                ["openssl", "genrsa", "-out", str(key), "2048"],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)],
                capture_output=True,
                check=True,
            )
            with m.GitHubApp("123", key.read_text(), "owner/repo") as auth:
                token = auth.jwt()
            header, payload, signature = token.split(".")
            self.assertEqual(
                json.loads(base64.urlsafe_b64decode(header + "=="))["alg"], "RS256"
            )
            claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
            self.assertEqual(claims["iss"], "123")
            self.assertEqual(claims["exp"] - claims["iat"], 600)
            sig = Path(directory, "sig")
            sig.write_bytes(base64.urlsafe_b64decode(signature + "=="))
            subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public),
                    "-signature",
                    str(sig),
                ],
                input=f"{header}.{payload}".encode(),
                capture_output=True,
                check=True,
            )
