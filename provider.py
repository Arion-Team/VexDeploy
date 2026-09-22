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
