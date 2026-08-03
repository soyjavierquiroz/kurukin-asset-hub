import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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

    def copyto_local_to_remote(
        self,
        local_path: str,
        remote_name: str,
        remote_path: str,
        timeout: int = 900,
    ) -> None:
        target = self._target(remote_name, remote_path)
        command = [
            self.binary,
            "copyto",
            local_path,
            target,
        ]
        self._run(command, operation="copyto upload", timeout=timeout)

    def moveto(
        self,
        remote_name: str,
        source_path: str,
        destination_path: str,
        timeout: int = 900,
    ) -> None:
        source = self._target(remote_name, source_path)
        destination = self._target(remote_name, destination_path)
        command = [
            self.binary,
            "moveto",
            source,
            destination,
        ]
        self._run(command, operation="moveto", timeout=timeout)

    def copyto_remote_to_remote(
        self,
        remote_name: str,
        source_path: str,
        destination_path: str,
        timeout: int = 900,
    ) -> None:
        source = self._target(remote_name, source_path)
        destination = self._target(remote_name, destination_path)
        command = [
            self.binary,
            "copyto",
            source,
            destination,
        ]
        self._run(command, operation="copyto remote", timeout=timeout)

    def rename_remote_file(
        self,
        remote_name: str,
        source_path: str,
        destination_path: str,
        timeout: int = 900,
    ) -> None:
        try:
            self.moveto(remote_name, source_path, destination_path, timeout=timeout)
        except RcloneError:
            if self.remote_file_exists(remote_name, destination_path):
                return
            try:
                self.copyto_remote_to_remote(
                    remote_name,
                    source_path,
                    destination_path,
                    timeout=timeout,
                )
            except RcloneError:
                if self.remote_file_exists(remote_name, destination_path):
                    return
                raise
            self.delete_remote_file(remote_name, source_path, timeout=timeout)

    def delete_remote_file(
        self,
        remote_name: str,
        remote_path: str,
        timeout: int = 300,
    ) -> None:
        target = self._target(remote_name, remote_path)
        command = [
            self.binary,
            "deletefile",
            target,
        ]
        self._run(command, operation="deletefile", timeout=timeout)

    def mkdir_remote(
        self,
        remote_name: str,
        remote_path: str,
        timeout: int = 300,
    ) -> None:
        target = self._target(remote_name, remote_path)
        command = [
            self.binary,
            "mkdir",
            target,
        ]
        self._run(command, operation="mkdir", timeout=timeout)

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

    def remote_file_exists(
        self,
        remote_name: str,
        remote_path: str,
        timeout: int = 120,
    ) -> bool:
        target = self._target(remote_name, remote_path)
        command = [
            self.binary,
            "lsjson",
            target,
            "--stat",
        ]
        try:
            result = self._run(command, operation="lsjson stat", timeout=timeout)
        except RcloneError:
            return False
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        if isinstance(payload, dict):
            return not bool(payload.get("IsDir"))
        return False

    def list_remotes(self) -> list[str]:
        result = self._run([self.binary, "listremotes"], operation="listremotes", timeout=60)
        remotes = []
        for line in result.stdout.splitlines():
            remote = line.strip().rstrip(":")
            if remote:
                remotes.append(remote)
        return remotes

    def remote_exists(self, remote: str) -> bool:
        return remote.strip().rstrip(":") in set(self.list_remotes())

    def _run(
        self,
        command: list[str],
        *,
        operation: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        with self._runtime() as runtime:
            command = runtime.command(command)
            env = runtime.env()
            try:
                return subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
            except FileNotFoundError as exc:
                raise RcloneError("rclone binary was not found in PATH") from exc
            except subprocess.TimeoutExpired as exc:
                raise RcloneError(f"rclone {operation} timed out") from exc
            except subprocess.CalledProcessError as exc:
                message = sanitize_rclone_message((exc.stderr or exc.stdout or "").strip())
                detail = f": {message}" if message else ""
                raise RcloneError(f"rclone {operation} failed{detail}") from exc

    def _runtime(self) -> "RcloneRuntime":
        return RcloneRuntime()

    @staticmethod
    def _target(remote: str, root: str) -> str:
        normalized_remote = normalize_remote_name(remote)
        normalized_root = normalize_remote_path(root, normalized_remote)
        if normalized_root:
            return f"{normalized_remote}:{normalized_root}"
        return f"{normalized_remote}:"


class RcloneRuntime:
    def __init__(self) -> None:
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.config_path: Path | None = None
        self.cache_dir: Path | None = None
        self.config_source = rclone_config_path()

    def __enter__(self) -> "RcloneRuntime":
        self._tempdir = tempfile.TemporaryDirectory(prefix="kurukin-rclone-")
        root = Path(self._tempdir.name)
        self.cache_dir = root / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.config_source is not None and self.config_source.is_file():
            self.config_path = root / "rclone.conf"
            shutil.copy2(self.config_source, self.config_path)
            self.config_path.chmod(0o600)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()

    def command(self, command: list[str]) -> list[str]:
        if not command:
            return command
        prepared = [command[0]]
        if self.config_path is not None:
            prepared.extend(["--config", str(self.config_path)])
        if self.cache_dir is not None:
            prepared.extend(["--cache-dir", str(self.cache_dir)])
        prepared.extend(command[1:])
        return prepared

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.config_path is not None:
            env["RCLONE_CONFIG"] = str(self.config_path)
        if self.cache_dir is not None:
            env["XDG_CACHE_HOME"] = str(self.cache_dir)
        return env


def rclone_config_path() -> Path | None:
    raw_path = os.environ.get("RCLONE_CONFIG")
    if raw_path:
        return Path(raw_path)
    default = Path.home() / ".config" / "rclone" / "rclone.conf"
    if default.exists():
        return default
    return None


def normalize_remote_name(remote: str) -> str:
    return remote.strip().rstrip(":")


def normalize_remote_path(path: str, remote: str | None = None) -> str:
    normalized = path.strip()
    clean_remote = normalize_remote_name(remote or "")
    if clean_remote and normalized.startswith(f"{clean_remote}:"):
        normalized = normalized[len(clean_remote) + 1 :]
    return normalized.strip("/")


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
