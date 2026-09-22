"""Clean Docker VPS provider — VexDeploy (no legacy code)."""

from __future__ import annotations

import logging
import secrets
import string
import time
from dataclasses import dataclass, field

import docker
from docker.errors import APIError, DockerException, NotFound

from config import (
    DOCKER_NETWORK,
    MAX_CONTAINERS,
    MAX_CPUS,
    MAX_DISK_GB,
    MAX_MEMORY_MB,
    MIN_CPUS,
    MIN_DISK_GB,
    MIN_MEMORY_MB,
)

logger = logging.getLogger("vexdeploy.provider")

LABEL_MANAGED = "vexdeploy.managed"
LABEL_OWNER = "vexdeploy.owner"
LABEL_VPS_ID = "vexdeploy.vps_id"


class ProviderError(Exception):
    """Provider-level failure with a user-safe message."""


def validate_resources(memory_mb: int, cpus: int, disk_gb: int) -> None:
    if memory_mb < MIN_MEMORY_MB or memory_mb > MAX_MEMORY_MB:
        raise ProviderError(
            f"Memory must be between {MIN_MEMORY_MB}MB and {MAX_MEMORY_MB}MB"
        )
    if cpus < MIN_CPUS or cpus > MAX_CPUS:
        raise ProviderError(f"CPU must be between {MIN_CPUS} and {MAX_CPUS}")
    if disk_gb < MIN_DISK_GB or disk_gb > MAX_DISK_GB:
        raise ProviderError(
            f"Disk space must be between {MIN_DISK_GB}GB and {MAX_DISK_GB}GB"
        )


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    # avoid shell-hostile chars in passwords used with chpasswd
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_name() -> str:
    return f"vex-{secrets.token_hex(4)}"


@dataclass
class VPSResult:
    container_id: str
    container_name: str
    ip_address: str
    ssh_port: int
    username: str
    password: str
    image: str
    memory_mb: int
    cpus: int
    disk_gb: int
    status: str = "running"
    notes: list[str] = field(default_factory=list)


class DockerProvider:
    """Creates and manages VPS containers on a local Docker engine."""

    def __init__(self, network: str = DOCKER_NETWORK) -> None:
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as exc:
            raise ProviderError(f"Docker is not available: {exc}") from exc
        self.network_name = network
        self._ensure_network()
        logger.info("Docker provider ready (network=%s)", self.network_name)

    def _ensure_network(self) -> None:
        try:
            self.client.networks.get(self.network_name)
        except NotFound:
            self.client.networks.create(
                self.network_name, driver="bridge", check_duplicate=True
            )
            logger.info("Created Docker network %s", self.network_name)

    def managed_count(self) -> int:
        containers = self.client.containers.list(
            all=True, filters={"label": LABEL_MANAGED}
        )
        return len(containers)

    def assert_capacity(self, max_containers: int = MAX_CONTAINERS) -> None:
        total = self.managed_count()
        limit = max(1, int(max_containers))
        if total >= limit:
            raise ProviderError(
                f"Server is at capacity ({total}/{limit} containers)."
            )

    # ── create ──────────────────────────────────────────────
    def create_vps(
        self,
        *,
        owner_id: str,
        memory_mb: int,
        cpus: int,
        disk_gb: int,
        image: str,
        vps_id: str = "",
        max_containers: int = MAX_CONTAINERS,
    ) -> VPSResult:
        validate_resources(memory_mb, cpus, disk_gb)
        self.assert_capacity(max_containers)

        password = generate_password()
        name = generate_name()
        username = "root"

        run_kwargs: dict = {
            "image": image,
            "name": name,
            "detach": True,
            "tty": True,
            "stdin_open": True,
            "mem_limit": f"{memory_mb}m",
            "memswap_limit": f"{memory_mb}m",
            "nano_cpus": int(cpus) * 1_000_000_000,
            "network": self.network_name,
            "restart_policy": {"Name": "unless-stopped"},
            "labels": {
                LABEL_MANAGED: "1",
                LABEL_OWNER: str(owner_id),
                LABEL_VPS_ID: vps_id or "",
            },
            "environment": {
                "DEBIAN_FRONTEND": "noninteractive",
                "VEX_PASSWORD": password,
            },
            # best-effort rootfs size (requires supported storage driver)
            "storage_opt": {"size": f"{disk_gb}G"},
        }

        # bootstrap: install sshd, set root password, start ssh
        run_kwargs["command"] = [
            "bash",
            "-lc",
            self._bootstrap_script(password),
        ]

        notes: list[str] = []
        try:
            container = self.client.containers.run(**run_kwargs)
        except APIError as exc:
            # storage_opt often unsupported — retry without
            if "storage" in str(exc).lower() or "size" in str(exc).lower():
                notes.append(
                    f"Disk quota {disk_gb}GB applied as plan metadata (storage-opt unsupported)."
                )
                run_kwargs.pop("storage_opt", None)
                try:
                    container = self.client.containers.run(**run_kwargs)
                except APIError as exc2:
                    raise ProviderError(f"Failed to create container: {exc2}") from exc2
            else:
                raise ProviderError(f"Failed to create container: {exc}") from exc

        try:
            container.reload()
            ip = self._container_ip(container)
            # wait briefly for sshd
            self._wait_ssh(container, timeout=90)
            container.reload()
            ip = self._container_ip(container) or ip
            return VPSResult(
                container_id=container.id,
                container_name=name,
                ip_address=ip,
                ssh_port=22,
                username=username,
                password=password,
                image=image,
                memory_mb=memory_mb,
                cpus=cpus,
                disk_gb=disk_gb,
                status="running",
                notes=notes,
            )
        except Exception:
            self._safe_remove(container.id)
            raise

    @staticmethod
    def _bootstrap_script(password: str) -> str:
        # runs as container main process
        return (
            "set -e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then "
            "  apt-get update -qq && apt-get install -y -qq openssh-server sudo curl wget ca-certificates; "
            "elif command -v apk >/dev/null 2>&1; then "
            "  apk add --no-cache openssh-server sudo curl wget; "
            "fi; "
            "mkdir -p /var/run/sshd /root/.ssh; "
            "echo 'root:" + password + "' | chpasswd; "
            "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config || true; "
            "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config || true; "
            "sed -i 's/^#\\?PermitEmptyPasswords.*/PermitEmptyPasswords no/' /etc/ssh/sshd_config || true; "
            "if command -v ssh-keygen >/dev/null 2>&1 && [ ! -f /etc/ssh/ssh_host_rsa_key ]; then "
            "  ssh-keygen -A; "
            "fi; "
            "exec /usr/sbin/sshd -D -e"
        )

    def _container_ip(self, container) -> str:
        container.reload()
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        if self.network_name in networks:
            ip = networks[self.network_name].get("IPAddress", "")
            if ip:
                return ip
        for info in networks.values():
            ip = info.get("IPAddress", "")
            if ip:
                return ip
        return container.attrs.get("NetworkSettings", {}).get("IPAddress", "") or ""

    def _wait_ssh(self, container, timeout: int = 90) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                container.reload()
                if container.status != "running":
                    raise ProviderError("Container stopped during bootstrap")
                code, _ = container.exec_run(
                    ["bash", "-lc", "nc -z 127.0.0.1 22 || ss -lnt | grep -q ':22'"],
                    demux=True,
                )
                if code == 0:
                    return
            except DockerException:
                pass
            time.sleep(2)
        # non-fatal: SSH may still come up; credentials are still valid
        logger.warning("SSH readiness check timed out for %s", container.short_id)

    # ── lifecycle ───────────────────────────────────────────
    def get_container(self, container_id: str):
        try:
            return self.client.containers.get(container_id)
        except NotFound as exc:
            raise ProviderError("Container not found") from exc

    def start(self, container_id: str) -> None:
        self.get_container(container_id).start()

    def stop(self, container_id: str, timeout: int = 10) -> None:
        self.get_container(container_id).stop(timeout=timeout)

    def restart(self, container_id: str, timeout: int = 10) -> None:
        self.get_container(container_id).restart(timeout=timeout)

    def remove(self, container_id: str, force: bool = True) -> None:
        try:
            c = self.get_container(container_id)
            c.remove(force=force)
        except NotFound:
            pass

    def _safe_remove(self, container_id: str) -> None:
        try:
            self.remove(container_id, force=True)
        except Exception as exc:
            logger.error("Cleanup failed for %s: %s", container_id, exc)

    def status(self, container_id: str) -> str:
        try:
            self.get_container(container_id).reload()
            return self.get_container(container_id).status
        except ProviderError:
            return "missing"

    def set_password(self, container_id: str, password: str) -> None:
        c = self.get_container(container_id)
        code, output = c.exec_run(
            ["bash", "-lc", f"echo 'root:{password}' | chpasswd"]
        )
        if code != 0:
            raise ProviderError("Failed to update SSH password inside container")

    def exec_command(
        self, container_id: str, command: str, timeout: int = 60
    ) -> tuple[int, str]:
        c = self.get_container(container_id)
        code, output = c.exec_run(
            ["bash", "-lc", command], workdir="/", demux=False
        )
        text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
        return int(code), text

    def logs(self, container_id: str, tail: int = 50) -> str:
        c = self.get_container(container_id)
        raw = c.logs(tail=max(1, int(tail)), timestamps=False)
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")

    # ── reverse SSH (sshx / tmate) ──────────────────────────
    def _ensure_remote_tools(self, container_id: str) -> None:
        script = (
            "set +e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then "
            "  apt-get update -qq >/dev/null 2>&1 || true; "
            "  apt-get install -y -qq curl ca-certificates tmate >/dev/null 2>&1 || "
            "  apt-get install -y -qq curl ca-certificates >/dev/null 2>&1 || true; "
            "elif command -v apk >/dev/null 2>&1; then "
            "  apk add --no-cache curl ca-certificates tmate >/dev/null 2>&1 || "
            "  apk add --no-cache curl ca-certificates >/dev/null 2>&1 || true; "
            "elif command -v yum >/dev/null 2>&1; then "
            "  yum install -y curl ca-certificates tmate >/dev/null 2>&1 || "
            "  yum install -y curl ca-certificates >/dev/null 2>&1 || true; "
            "fi; "
            "if ! command -v sshx >/dev/null 2>&1; then "
            "  ARCH=$(uname -m); "
            "  case \"$ARCH\" in "
            "    x86_64|amd64) A=x86_64 ;; "
            "    aarch64|arm64) A=aarch64 ;; "
            "    armv7l|armhf) A=armv7 ;; "
            "    *) A=x86_64 ;; "
            "  esac; "
            "  mkdir -p /usr/local/bin; "
            "  (curl -fsSL --retry 3 --connect-timeout 15 "
            "    \"https://github.com/ekzhang/sshx/releases/latest/download/sshx-${A}-unknown-linux-musl\" "
            "    -o /usr/local/bin/sshx || "
            "   curl -fsSL --retry 3 --connect-timeout 15 "
            "    \"https://github.com/ekzhang/sshx/releases/latest/download/sshx-${A}-unknown-linux-gnu\" "
            "    -o /usr/local/bin/sshx) && chmod +x /usr/local/bin/sshx || true; "
            "fi; "
            "command -v sshx >/dev/null 2>&1 && echo HAVE_SSHX || echo NO_SSHX; "
            "command -v tmate >/dev/null 2>&1 && echo HAVE_TMATE || echo NO_TMATE"
        )
        code, out = self.exec_command(container_id, script, timeout=120)
        if code != 0 or "NO_SSHX" in (out or ""):
            logger.warning("remote tools install exit %s: %s", code, (out or "")[-400:])

    @staticmethod
    def _extract_sshx_url(text: str) -> str:
        import re

        for pat in (
            r"https://sshx\.io/\S+",
            r"ssh\s+\S+@sshx\.io\S*",
            r"sshx\.io/\S+",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(0).strip().rstrip(".,)")
        return ""

    @staticmethod
    def _extract_tmate_ssh(text: str) -> str:
        import re

        m = re.search(r"ssh\s+\S+@\S+", text)
        if m:
            return m.group(0).strip()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ssh ") and "@" in line:
                return line
        return ""

    def start_sshx(self, container_id: str, timeout: int = 45) -> str:
        """Install sshx if needed and return a share URL/SSH command.

        Detaches via PID file only — never pkill -f (that SIGTERMs the launcher shell).
        """
        self._ensure_remote_tools(container_id)
        script = (
            "set +e; "
            # kill only by exact name / pid file — never match the parent bash cmdline
            "if [ -f /tmp/sshx.pid ]; then kill \"$(cat /tmp/sshx.pid 2>/dev/null)\" >/dev/null 2>&1 || true; fi; "
            "pkill -x sshx >/dev/null 2>&1 || true; "
            "rm -f /tmp/sshx.log /tmp/sshx.pid /tmp/sshx.out; "
            "if ! command -v sshx >/dev/null 2>&1; then "
            "  echo 'sshx binary missing'; exit 2; "
            "fi; "
            # fully detach stdin/stdout so sshx cannot block on a missing TTY
            "setsid sshx </dev/null >/tmp/sshx.log 2>&1 & "
            "echo $! > /tmp/sshx.pid; "
            "for i in $(seq 1 " + str(timeout) + "); do "
            "  if grep -Eq 'https://sshx\\.io|ssh .*@sshx\\.io' /tmp/sshx.log 2>/dev/null; then break; fi; "
            "  PID=$(cat /tmp/sshx.pid 2>/dev/null); "
            "  if [ -n \"$PID\" ] && ! kill -0 \"$PID\" 2>/dev/null; then break; fi; "
            "  sleep 1; "
            "done; "
            "cat /tmp/sshx.log 2>/dev/null || true"
        )
        code, out = self.exec_command(container_id, script, timeout=timeout + 30)
        url = self._extract_sshx_url(out or "")
        if not url:
            # second chance: re-read the log file (launcher may have exited after writing URL)
            _, out2 = self.exec_command(container_id, "cat /tmp/sshx.log 2>/dev/null || true", timeout=15)
            url = self._extract_sshx_url(out2 or "")
        if not url:
            raise ProviderError(
                "sshx did not return a share link "
                f"(exit {code}). Check outbound network in the container."
            )
        return url

    def start_tmate(self, container_id: str, timeout: int = 30) -> str:
        """Install tmate if needed and return an SSH share command."""
        self._ensure_remote_tools(container_id)
        script = (
            "set +e; "
            "if ! command -v tmate >/dev/null 2>&1; then "
            "  echo 'tmate not installed'; exit 2; "
            "fi; "
            "SOCK=/tmp/tmate.sock; "
            "tmate -S \"$SOCK\" kill-server >/dev/null 2>&1 || true; "
            "rm -f \"$SOCK\"; "
            # detached session; tmate writes keys once the relay accepts
            "tmate -S \"$SOCK\" new-session -d >/tmp/tmate.log 2>&1; "
            "for i in $(seq 1 " + str(timeout) + "); do "
            "  SSH=$(tmate -S \"$SOCK\" show -qF '#{tmate_ssh}' 2>/dev/null); "
            "  if [ -n \"$SSH\" ]; then echo \"$SSH\"; exit 0; fi; "
            "  RO=$(tmate -S \"$SOCK\" show -qF '#{tmate_ssh_ro}' 2>/dev/null); "
            "  if [ -n \"$RO\" ]; then echo \"$RO\"; exit 0; fi; "
            "  sleep 1; "
            "done; "
            # diagnostics for callers
            "echo 'TMATE_TIMEOUT'; "
            "cat /tmp/tmate.log 2>/dev/null || true; "
            "tmate -S \"$SOCK\" show 2>/dev/null | head -n 20 || true; "
            "exit 3"
        )
        code, out = self.exec_command(container_id, script, timeout=timeout + 30)
        ssh_cmd = self._extract_tmate_ssh(out or "")
        if not ssh_cmd:
            for line in (out or "").splitlines():
                line = line.strip()
                if line and " " not in line.split("@")[0] and "@" in line and "TMATE" not in line:
                    ssh_cmd = line
                    break
            if not ssh_cmd and out and "@" in out:
                ssh_cmd = out.strip().splitlines()[-1].strip()
        if not ssh_cmd:
            hint = ""
            low = (out or "").lower()
            if "could not resolve" in low or "network" in low or "connection" in low:
                hint = " Container may have no outbound network to tmate.io."
            raise ProviderError(
                "tmate did not return an SSH command "
                f"(exit {code}). Check outbound network / tmate package.{hint}"
            )
        if not ssh_cmd.startswith("ssh "):
            ssh_cmd = f"ssh {ssh_cmd}"
        return ssh_cmd

    def stop_remote_share(self, container_id: str, tool: str = "all") -> None:
        """Best-effort stop of sshx/tmate sessions inside a container."""
        if tool in ("sshx", "all"):
            # PID file + exact name only — never pkill -f (matches parent shell)
            self.exec_command(
                container_id,
                "if [ -f /tmp/sshx.pid ]; then kill \"$(cat /tmp/sshx.pid 2>/dev/null)\" >/dev/null 2>&1 || true; fi; "
                "pkill -x sshx >/dev/null 2>&1 || true; true",
                timeout=15,
            )
        if tool in ("tmate", "all"):
            self.exec_command(
                container_id,
                "tmate -S /tmp/tmate.sock kill-server 2>/dev/null || true; true",
                timeout=15,
            )

    def stats(self, container_id: str) -> dict:
        c = self.get_container(container_id)
        c.reload()
        raw = c.stats(stream=False)
        cpu = 0.0
        mem = 0.0
        try:
            cpu_delta = (
                raw["cpu_stats"]["cpu_usage"]["total_usage"]
                - raw["precpu_stats"]["cpu_usage"]["total_usage"]
            )
            system_delta = raw["cpu_stats"].get(
                "system_cpu_usage", 0
            ) - raw["precpu_stats"].get("system_cpu_usage", 0)
            online = raw["cpu_stats"].get("online_cpus") or 1
            if system_delta > 0:
                cpu = (cpu_delta / system_delta) * online * 100.0
            mem = float(raw.get("memory_stats", {}).get("usage", 0))
        except (KeyError, ZeroDivisionError):
            pass
        state = c.attrs.get("State", {})
        return {
            "status": c.status,
            "cpu_percent": round(cpu, 2),
            "mem_used_mb": round(mem / (1024 * 1024), 2),
            "started_at": state.get("StartedAt", ""),
            "pid": state.get("Pid", 0),
        }

    def list_managed(self) -> list:
        return self.client.containers.list(
            all=True, filters={"label": LABEL_MANAGED}
        )

    def ping(self) -> bool:
        try:
            self.client.ping()
            return True
        except DockerException:
            return False
