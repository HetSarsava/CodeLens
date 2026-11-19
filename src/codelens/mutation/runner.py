"""A new network-disabled Linux container per observation; no writable host mount."""

import contextlib
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from .models import Observation


class DockerRunner:
    def image(self) -> str:
        tag = os.environ.get("CODELENS_MUTATION_IMAGE", "codelens-mutation:local")
        try:
            return subprocess.check_output(
                ["docker", "image", "inspect", "--format", "{{.Id}}", "--", tag],
                stderr=subprocess.DEVNULL,
                timeout=5,
                text=True,
            ).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(
                "Docker runner unavailable. Start Docker and build the image "
                "using docs/adaptive-mutation.md."
            ) from exc

    def run(
        self, source: Path, scope: list[str], image: str, timeout: float, cancelled: threading.Event
    ) -> Observation:
        name = "codelens-" + uuid.uuid4().hex
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=1",
            "--pids-limit=64",
            "--ulimit",
            "fsize=4194304:4194304",
            "--log-driver=none",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--mount",
            f"type=bind,src={source},dst=/input,readonly",
            "--mount",
            f"type=bind,src={Path(__file__).with_name('probe.py')},dst=/probe.py,readonly",
            "--entrypoint",
            "/usr/bin/env",
            image,
            "-i",
            "HOME=/tmp",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE=1",
            "timeout",
            "-s",
            "KILL",
            str(max(1, int(timeout))),
            "python",
            "-I",
            "/probe.py",
            "/input",
            "/tmp/work",
            *scope,
        ]
        start = time.monotonic()
        result = Observation(scope=scope)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(command, stdout=out, stderr=err)
            except OSError as exc:
                result.reason = f"Docker could not start: {exc}"
                return result
            try:
                while proc.poll() is None:
                    if cancelled.wait(0.05):
                        result.reason = "cancelled"
                        break
                    if time.monotonic() - start >= timeout:
                        result.reason = "timeout"
                        break
                    if max(os.fstat(f.fileno()).st_size for f in (out, err)) > 1048576:
                        result.reason = "runner output limit"
                        break
                if not result.reason:
                    out.seek(0)
                    try:
                        result = Observation.model_validate_json(out.read(1048577))
                        if result.scope != scope or proc.returncode != 0:
                            result = Observation(scope=scope, reason="runner report mismatch")
                    except ValidationError:
                        err.seek(0)
                        result.reason = (
                            "timeout"
                            if proc.returncode in (124, 137)
                            else "invalid runner report: " + err.read(1500).decode(errors="replace")
                        )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        subprocess.run(
                            ["docker", "rm", "-f", name],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            check=False,
                        )
        result.seconds = round(time.monotonic() - start, 3)
        return result
