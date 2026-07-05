import json
from pathlib import Path
import re
import subprocess
from typing import Any


class RcloneError(RuntimeError):
    pass


class RcloneService:
    def __init__(self, binary: str = "rclone") -> None:
        self.binary = binary

    def copyto(
        self,
        remote: str,
        remote_path: str,
        local_path: str,
        timeout: int = 900,
    ) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        source = self._target(remote, remote_path)
        command = [
            self.binary,
            "copyto",
            source,
            local_path,
        ]
        self._run(command, operation="copyto", timeout=timeout)

    def list_json(self, remote: str, root: str) -> list[dict[str, Any]]:
        target = self._target(remote, root)
        command = [
            self.binary,
            "lsjson",
            target,
            "--recursive",
            "--files-only",
        ]
        result = self._run(command, operation="lsjson", timeout=300)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RcloneError("rclone lsjson returned invalid JSON") from exc

        if not isinstance(payload, list):
            raise RcloneError("rclone lsjson returned an unexpected JSON payload")

        entries: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise RcloneError("rclone lsjson returned a non-object entry")
            entries.append(item)
        return entries

    def _run(
        self,
        command: list[str],
        *,
        operation: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RcloneError("rclone binary was not found in PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RcloneError(f"rclone {operation} timed out") from exc
        except subprocess.CalledProcessError as exc:
            message = sanitize_rclone_message((exc.stderr or exc.stdout or "").strip())
            detail = f": {message}" if message else ""
            raise RcloneError(f"rclone {operation} failed{detail}") from exc

    @staticmethod
    def _target(remote: str, root: str) -> str:
        normalized_root = root.strip("/")
        if normalized_root:
            return f"{remote}:{normalized_root}"
        return f"{remote}:"


def sanitize_rclone_message(message: str) -> str:
    patterns = [
        r"(?i)(access_token\s*[=:]\s*)\S+",
        r"(?i)(refresh_token\s*[=:]\s*)\S+",
        r"(?i)(client_secret\s*[=:]\s*)\S+",
        r"(?i)(token\s*[=:]\s*)\S+",
        r"(?i)(authorization:\s*bearer\s+)\S+",
    ]
    sanitized = message
    for pattern in patterns:
        sanitized = re.sub(pattern, r"\1[redacted]", sanitized)
    return sanitized
