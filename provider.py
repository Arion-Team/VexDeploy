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
    def _ensure_remote_tools(self, container_id: str) -> str:
        """Install curl/tmate/sshx. Returns a status log for diagnostics.

        Scripts are written to a file (no nested quotes / single-line # comments).
        """
        script = r"""set +e
export DEBIAN_FRONTEND=noninteractive
export APT_LISTCHANGES_FRONTEND=none
echo '--- install ---'
if command -v apt-get >/dev/null 2>&1; then
  timeout 60 apt-get update -qq >/tmp/vex-apt.log 2>&1 || true
  timeout 90 apt-get install -y -qq --no-install-recommends curl ca-certificates tmate \
    >>/tmp/vex-apt.log 2>&1 || \
  timeout 60 apt-get install -y -qq --no-install-recommends curl ca-certificates \
    >>/tmp/vex-apt.log 2>&1 || true
elif command -v apk >/dev/null 2>&1; then
  timeout 60 apk add --no-cache curl ca-certificates tmate >/tmp/vex-apt.log 2>&1 || \
  timeout 60 apk add --no-cache curl ca-certificates >>/tmp/vex-apt.log 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  timeout 90 yum install -y curl ca-certificates tmate >/tmp/vex-apt.log 2>&1 || \
  timeout 60 yum install -y curl ca-certificates >>/tmp/vex-apt.log 2>&1 || true
fi

# tmate: static binary fallback (Ubuntu docker often lacks universe / package)
if ! command -v tmate >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
  echo 'tmate: trying static binary'
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64|amd64) TA=amd64 ;;
    aarch64|arm64) TA=arm64v8 ;;
    armv7l|armhf) TA=arm32v7 ;;
    armv6*) TA=arm32v6 ;;
    i386|i686) TA=i386 ;;
    *) TA=amd64 ;;
  esac
  TM_VER=2.4.0
  TM_URL="https://github.com/tmate-io/tmate/releases/download/${TM_VER}/tmate-${TM_VER}-static-linux-${TA}.tar.xz"
  if timeout 45 curl -fsSL --retry 2 --connect-timeout 10 "$TM_URL" -o /tmp/tmate.tar.xz; then
    mkdir -p /tmp/tmate-extract
    if tar -xJf /tmp/tmate.tar.xz -C /tmp/tmate-extract 2>/tmp/vex-tmate-extract.log; then
      SRC=$(find /tmp/tmate-extract -type f -name tmate 2>/dev/null | head -n1)
      if [ -n "$SRC" ]; then
        chmod +x "$SRC"
        cp -f "$SRC" /usr/local/bin/tmate 2>/dev/null || true
        chmod +x /usr/local/bin/tmate 2>/dev/null || true
      fi
    fi
    rm -rf /tmp/tmate.tar.xz /tmp/tmate-extract
  fi
fi

# sshx: official installer (GitHub releases ship no binary assets)
if ! command -v sshx >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
  echo 'sshx: official installer'
  if timeout 45 curl -sSf --retry 2 --connect-timeout 10 https://sshx.io/get -o /tmp/sshx-get.sh; then
    timeout 30 sh /tmp/sshx-get.sh >/tmp/vex-sshx-install.log 2>&1 || \
      echo 'sshx installer failed'
  else
    echo 'sshx get script download failed'
  fi
fi

command -v sshx >/dev/null 2>&1 && echo HAVE_SSHX || echo NO_SSHX
command -v tmate >/dev/null 2>&1 && echo HAVE_TMATE || echo NO_TMATE
command -v curl >/dev/null 2>&1 && echo HAVE_CURL || echo NO_CURL
echo '--- apt tail ---'
tail -n 40 /tmp/vex-apt.log 2>/dev/null || true
echo '--- tmate extract tail ---'
tail -n 20 /tmp/vex-tmate-extract.log 2>/dev/null || true
echo '--- sshx install tail ---'
tail -n 30 /tmp/vex-sshx-install.log 2>/dev/null || true
"""
        try:
            code, out = self._run_script(container_id, script, timeout=180)
        except Exception as exc:
            logger.warning("remote tools install failed: %s", exc)
            return f"install error: {exc}"
        text = out or ""
        if "NO_SSHX" in text or "NO_TMATE" in text:
            logger.warning("remote tools partial (exit %s): %s", code, text[-800:])
        return text

    def _run_script(self, container_id: str, script: str, timeout: int = 60) -> tuple[int, str]:
        """Write script to /tmp and execute with an outer timeout (avoids quoting issues)."""
        # base64 avoids any shell-quoting of the script body
        import base64

        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        # split long payloads to avoid argv limits
        wrapper = (
            f"echo {b64} | base64 -d > /tmp/vex-run.sh && "
            f"chmod +x /tmp/vex-run.sh && "
            f"timeout {max(5, int(timeout))} bash /tmp/vex-run.sh"
        )
        return self.exec_command(container_id, wrapper, timeout=timeout + 15)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        import re

        # ESC [ ... m  and bare ESC sequences
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "")
        text = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", text)
        return text.replace("\x1b", "")

    @staticmethod
    def _extract_sshx_url(text: str) -> str:
        import re

        text = DockerProvider._strip_ansi(text)
        for pat in (
            r"https://sshx\.io/\S+",
            r"ssh\s+\S+@sshx\.io\S*",
            r"sshx\.io/\S+",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                url = m.group(0).strip().rstrip(".,);'\"")
                # drop trailing reset junk if any survived
                url = re.sub(r"[\x00-\x1f]+$", "", url)
                return url
        return ""

    @staticmethod
    def _extract_tmate_ssh(text: str) -> str:
        import re

        text = DockerProvider._strip_ansi(text)
        m = re.search(r"ssh\s+\S+@\S+", text)
        if m:
            return m.group(0).strip().rstrip(".,);'\"")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ssh ") and "@" in line:
                return line.rstrip(".,);'\"")
        return ""

    def start_sshx(self, container_id: str, timeout: int = 45) -> str:
        """Install sshx if needed and return a share URL/SSH command.

        Uses the official installer (GitHub releases have no binary assets).
        Detaches via PID file only — never pkill -f (that SIGTERMs the launcher).
        """
        install_log = self._ensure_remote_tools(container_id)

        script = f"""set +e
if [ -f /tmp/sshx.pid ]; then kill "$(cat /tmp/sshx.pid 2>/dev/null)" >/dev/null 2>&1 || true; fi
pkill -x sshx >/dev/null 2>&1 || true
rm -f /tmp/sshx.log /tmp/sshx.pid /tmp/sshx.out
if command -v sshx >/dev/null 2>&1; then
  setsid sshx </dev/null >/tmp/sshx.log 2>&1 &
  echo $! > /tmp/sshx.pid
  for i in $(seq 1 {timeout}); do
    if grep -Eq 'https://sshx\\.io|ssh .*@sshx\\.io' /tmp/sshx.log 2>/dev/null; then break; fi
    PID=$(cat /tmp/sshx.pid 2>/dev/null)
    if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then break; fi
    sleep 1
  done
  cat /tmp/sshx.log 2>/dev/null || true
else
  if [ -f /tmp/sshx-get.sh ]; then
    timeout {timeout} sh /tmp/sshx-get.sh run >/tmp/sshx.log 2>&1
    cat /tmp/sshx.log 2>/dev/null || true
  elif command -v curl >/dev/null 2>&1; then
    timeout {timeout} sh -c 'curl -sSf https://sshx.io/get | sh -s run' >/tmp/sshx.log 2>&1
    cat /tmp/sshx.log 2>/dev/null || true
  else
    echo 'sshx binary missing and curl unavailable'
    exit 2
  fi
fi
"""
        try:
            code, out = self._run_script(container_id, script, timeout=timeout + 45)
        except Exception as exc:
            raise ProviderError(f"sshx exec failed: {exc}") from exc
        url = self._extract_sshx_url(out or "")
        if not url:
            _, out2 = self.exec_command(
                container_id, "cat /tmp/sshx.log 2>/dev/null || true", timeout=15
            )
            url = self._extract_sshx_url(out2 or "")
        # normalize: always return clean URL (no ANSI / trailing junk)
        url = self._strip_ansi(url or "").strip()
        if not url:
            tail = self._strip_ansi(((out or "") + "\n" + (install_log or "")))[-500:].strip()
            raise ProviderError(
                "sshx did not return a share link "
                f"(exit {code}). Install/network issue: {tail}"
            )
        return url

    def start_tmate(self, container_id: str, timeout: int = 30) -> str:
        """Install tmate if needed and return an SSH share command."""
        install_log = self._ensure_remote_tools(container_id)
        script = f"""set +e
if ! command -v tmate >/dev/null 2>&1; then
  echo 'tmate not installed'
  exit 2
fi
SOCK=/tmp/tmate.sock
tmate -S "$SOCK" kill-server >/dev/null 2>&1 || true
rm -f "$SOCK"
timeout 20 tmate -S "$SOCK" new-session -d >/tmp/tmate.log 2>&1
for i in $(seq 1 {timeout}); do
  SSH=$(timeout 3 tmate -S "$SOCK" show -qF '#{{tmate_ssh}}' 2>/dev/null)
  if [ -n "$SSH" ]; then echo "$SSH"; exit 0; fi
  RO=$(timeout 3 tmate -S "$SOCK" show -qF '#{{tmate_ssh_ro}}' 2>/dev/null)
  if [ -n "$RO" ]; then echo "$RO"; exit 0; fi
  sleep 1
done
echo 'TMATE_TIMEOUT'
cat /tmp/tmate.log 2>/dev/null || true
timeout 3 tmate -S "$SOCK" show 2>/dev/null | head -n 20 || true
exit 3
"""
        try:
            code, out = self._run_script(container_id, script, timeout=timeout + 30)
        except Exception as exc:
            raise ProviderError(f"tmate exec failed: {exc}") from exc
        ssh_cmd = self._extract_tmate_ssh(out or "")
        if not ssh_cmd:
            clean = self._strip_ansi(out or "")
            for line in clean.splitlines():
                line = line.strip()
                if line and " " not in line.split("@")[0] and "@" in line and "TMATE" not in line:
                    ssh_cmd = line
                    break
            if not ssh_cmd and clean and "@" in clean:
                ssh_cmd = clean.strip().splitlines()[-1].strip()
        ssh_cmd = self._strip_ansi(ssh_cmd or "").strip()
        if not ssh_cmd:
            hint = ""
            low = self._strip_ansi(out or "").lower()
            if code == 137:
                hint = " Process was SIGKILLed (OOM or external kill)."
            elif code == 124:
                hint = " Timed out waiting for tmate relay."
            elif "could not resolve" in low or "network" in low or "connection" in low:
                hint = " Container may have no outbound network to tmate.io."
            elif "NO_TMATE" in (install_log or "") or "tmate not installed" in low:
                hint = " tmate failed to install (apt package + static binary both failed)."
            tail = (
                self._strip_ansi((out or "") + "\n" + (install_log or ""))
            )[-400:].strip()
            raise ProviderError(
                "tmate did not return an SSH command "
                f"(exit {code}).{hint} Details: {tail}"
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
