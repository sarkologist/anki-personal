# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Short-lived, repository-scoped installation tokens for the trusted controller."""

import base64
import http.client
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "actions": "read",
    "administration": "read",
}


def clean_environment():
    return {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "CODEX_REPAIR_APP_PRIVATE_KEY",
            "CODEX_AUTH_JSON",
        }
    }


def encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GitHubApp:
    def __init__(self, app_id, private_key, repository):
        if not app_id or not private_key:
            raise ValueError("Missing GitHub App ID or private key")
        self.app_id = app_id
        self.repository = repository
        self.current_token = None
        self.expires = 0
        self.directory = tempfile.TemporaryDirectory(prefix="anki-github-app-")
        self.key_path = Path(self.directory.name, "private-key.pem")
        self.key_path.touch(mode=0o600)
        self.key_path.write_text(private_key)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def jwt(self):
        now = int(time.time())
        header = encoded(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        payload = encoded(
            json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode()
        )
        message = f"{header}.{payload}"
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(self.key_path)],
            input=message.encode(),
            capture_output=True,
            timeout=30,
            check=False,
            env=clean_environment(),
        )
        if signed.returncode:
            raise RuntimeError("GitHub App signing failed")
        return f"{message}.{encoded(signed.stdout)}"

    def request(self, method, path, credential, body=None):
        # Fixed HTTPS host; no redirects and no credential-bearing command arguments.
        connection = http.client.HTTPSConnection("api.github.com", timeout=30)
        try:
            connection.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "anki-cargo-deny-controller",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            data = response.read()
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"GitHub App request failed (HTTP {response.status})"
                )
            return json.loads(data) if data else {}
        except (OSError, http.client.HTTPException, ValueError):
            raise RuntimeError("GitHub App request failed; response omitted") from None
        finally:
            connection.close()

    def token(self):
        # Leave more than the controller's five-minute subprocess timeout remaining.
        if self.current_token and time.time() < self.expires - 600:
            return self.current_token
        jwt = self.jwt()
        installation = self.request(
            "GET", f"/repos/{self.repository}/installation", jwt
        )
        result = self.request(
            "POST",
            f"/app/installations/{int(installation['id'])}/access_tokens",
            jwt,
            {
                "repositories": [self.repository.split("/")[1]],
                "permissions": PERMISSIONS,
            },
        )
        token = result.get("token")
        expiry = datetime.fromisoformat(
            result["expires_at"].replace("Z", "+00:00")
        ).timestamp()
        if not isinstance(token, str) or not token or expiry <= time.time() + 600:
            raise RuntimeError("GitHub App returned an invalid or nearly expired token")
        # Mask newly minted secrets before any child process can run.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{token}", flush=True)
        old = self.current_token
        self.current_token, self.expires = token, expiry
        if old:
            self.revoke(old)
        return token

    def revoke(self, token):
        try:
            self.request("DELETE", "/installation/token", token)
        except Exception:
            # Expiry still bounds lifetime if cleanup fails. Never print response bodies.
            print(
                "Warning: installation token revocation failed; token will expire.",
                flush=True,
            )

    def close(self):
        try:
            if self.current_token:
                self.revoke(self.current_token)
                self.current_token = None
        finally:
            self.directory.cleanup()
