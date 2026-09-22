"""Clean LXD/Incus VPS provider — VexDeploy (no legacy code)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import string
import subprocess
import time
from dataclasses import dataclass, field

from config import (
    LXD_CLI,
    LXD_NETWORK,
    MAX_CONTAINERS,
    MAX_CPUS,
    MAX_DISK_GB,
    MAX_MEMORY_MB,
    MIN_CPUS,
    MIN_DISK_GB,
    MIN_MEMORY_MB,
)

logger = logging.getLogger("vexdeploy.provider")

LABEL_MANAGED = "user.vexdeploy.managed"
LABEL_OWNER = "user.vexdeploy.owner"
LABEL_VPS_ID = "user.vexdeploy.vps_id"

IMAGE_MAP = {
    "ubuntu:22.04": "images:ubuntu/22.04",
    "ubuntu:24.04": "images:ubuntu/24.04",
    "debian:12": "images:debian/12",
    "debian:13": "images:debian/13",
    "alpine:3.20": "images:alpine/3.20/cloud",
    "alpine:3.21": "images:alpine/3.21/cloud",
    "rocky:9": "images:rockylinux/9",
    "almalinux:9": "images:almalinux/9",
    "fedora:40": "images:fedora/40",
    "fedora:41": "images:fedora/41",
    "oracle:9": "images:oracle/9",
    "opensuse:15": "images:opensuse/15.6",
}

IMAGE_CANDIDATES: dict[str, list[str]] = {
    "ubuntu:22.04": [
        "images:ubuntu/22.04",
        "ubuntu:22.04",
        "ubuntu:jammy",
        "images:ubuntu/jammy",
    ],
    "ubuntu:24.04": [
        "images:ubuntu/24.04",
        "ubuntu:24.04",
        "ubuntu:noble",
        "images:ubuntu/noble",
    ],
    "debian:12": ["images:debian/12", "images:debian/bookworm"],
    "debian:13": ["images:debian/13", "images:debian/trixie"],
    "alpine:3.20": ["images:alpine/3.20/cloud", "images:alpine/3.20"],
    "alpine:3.21": ["images:alpine/3.21/cloud", "images:alpine/3.21"],
    "rocky:9": ["images:rockylinux/9", "images:rockylinux/9/cloud"],
    "almalinux:9": ["images:almalinux/9", "images:almalinux/9/cloud"],
    "fedora:40": ["images:fedora/40", "images:fedora/40/cloud"],
    "fedora:41": ["images:fedora/41", "images:fedora/41/cloud"],
    "oracle:9": ["images:oracle/9", "images:oracle/9/cloud"],
    "opensuse:15": [
        "images:opensuse/15.6",
        "images:opensuse/15",
        "images:opensuse/leap/15.6",
    ],
}


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
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_name() -> str:
    return f"vex-{secrets.token_hex(4)}"


def _detect_cli() -> str:
    override = (LXD_CLI or "").strip()
    if override:
        if shutil.which(override) or os.path.isfile(override):
            return override
        raise ProviderError(f"LXD_CLI is set but not executable: {override}")
    for cand in ("incus", "lxc"):
        path = shutil.which(cand)
        if path:
            return path
    for path in ("/snap/bin/lxc", "/snap/bin/incus", "/usr/bin/lxc", "/usr/bin/incus"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise ProviderError(
        "Neither lxc nor incus CLI found (install LXD/Incus or set LXD_CLI)"
    )


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


class LXDProvider:
    """Creates and manages VPS instances on local LXD/Incus via the lxc CLI."""

    def __init__(self, network: str = LXD_NETWORK) -> None:
        self._cli = _detect_cli()
        self._exec_flags = self._detect_exec_flags()
        self.network_name = network
        self._ensure_network()
        logger.info(
            "LXD provider ready (cli=%s, network=%s, exec=%s)",
            self._cli,
            self.network_name,
            " ".join(self._exec_flags) or "default",
        )

    def _run(
        self, args: list[str], *, timeout: int = 60, check: bool = True
    ) -> tuple[int, str]:
        cmd = [self._cli, *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"LXD CLI not found: {self._cli}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"LXD command timed out after {timeout}s: {' '.join(args[:4])}"
            ) from exc
        out = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise ProviderError(
                f"LXD command failed ({proc.returncode}): {out.strip()[:500]}"
            )
        return proc.returncode, out

    def _detect_exec_flags(self) -> list[str]:
        candidates = (
            ["--mode", "non-interactive"],
            ["--force-noninteractive"],
            [],
        )
        for flags in candidates:
            code, out = self._run(
                ["exec", "__vex_probe__", *flags, "--", "true"],
                check=False,
                timeout=15,
            )
            low = (out or "").lower()
            if any(
                s in low
                for s in (
                    "unknown",
                    "unexpected",
                    "flag provided",
                    "invalid argument",
                    "unknown option",
                )
            ):
                continue
            return flags
        return []

    def _ensure_network(self) -> None:
        code, _ = self._run(
            ["network", "show", self.network_name], check=False, timeout=30
        )
        if code == 0:
            return
        try:
            self._run(
                [
                    "network",
                    "create",
                    self.network_name,
                    "ipv4.address=auto",
                    "ipv4.nat=true",
                ],
                timeout=60,
            )
            logger.info("Created LXD network %s", self.network_name)
        except ProviderError as exc:
            code2, _ = self._run(
                ["network", "show", self.network_name], check=False, timeout=30
            )
            if code2 != 0:
                raise ProviderError(f"Failed to create LXD network: {exc}") from exc

    def _list_all_json(self) -> list:
        code, out = self._run(["list", "--format", "json"], check=False, timeout=90)
        if code != 0:
            raise ProviderError(f"Failed to list instances: {out.strip()[:400]}")
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            raise ProviderError("Failed to parse LXD instance list") from exc
        return data if isinstance(data, list) else []

    def _list_managed_json(self) -> list:
        managed = []
        for inst in self._list_all_json():
            cfg = inst.get("config") or {}
            if str(cfg.get(LABEL_MANAGED, "")) == "1":
                managed.append(inst)
        return managed

    def managed_count(self) -> int:
        return len(self._list_managed_json())

    def assert_capacity(self, max_containers: int = MAX_CONTAINERS) -> None:
        total = self.managed_count()
        limit = max(1, int(max_containers))
        if total >= limit:
            raise ProviderError(
                f"Server is at capacity ({total}/{limit} instances)."
            )

    def list_managed(self) -> list:
        return self._list_managed_json()

    def _resolve_image(self, image: str) -> str:
        image = (image or "").strip()
        if image in IMAGE_MAP:
            return IMAGE_MAP[image]
        if ":" in image and not image.startswith("images:"):
            remote = image.split(":", 1)[0]
            if remote in {"ubuntu", "ubuntu-daily", "images", "simplestreams"}:
                return image
        if "/" in image and not image.startswith("images:"):
            return f"images:{image}"
        return image

    def _image_candidates(self, image: str) -> list[str]:
        image = (image or "").strip()
        if image in IMAGE_CANDIDATES:
            return list(IMAGE_CANDIDATES[image])
        if image in IMAGE_MAP:
            primary = IMAGE_MAP[image]
            alts = IMAGE_CANDIDATES.get(image, [])
            out = [primary]
            for alt in alts:
                if alt not in out:
                    out.append(alt)
            return out
        resolved = self._resolve_image(image)
        out = [resolved]
        for key, alts in IMAGE_CANDIDATES.items():
            if image == key or resolved == IMAGE_MAP.get(key):
                for alt in alts:
                    if alt not in out:
                        out.append(alt)
        if image not in out:
            out.append(image)
        return out

    @staticmethod
    def _is_missing_image_error(text: str) -> bool:
        low = (text or "").lower()
        return any(
            s in low
            for s in (
                "image couldn't be found",
                "image not found",
                "failed getting remote image",
                "failed getting image",
                "no such image",
                "remote image info",
            )
        )

    def _launch_instance(
        self,
        *,
        image_uri: str,
        name: str,
        owner_id: str,
        memory_mb: int,
        cpus: int,
        vps_id: str,
    ) -> str:
        """Launch with image fallbacks. Returns the image URI that worked."""
        args = [
            "launch",
            image_uri,
            name,
            "-n",
            self.network_name,
            "-c",
            f"limits.memory={int(memory_mb)}MB",
            "-c",
            f"limits.cpu={int(cpus)}",
            "-c",
            f"{LABEL_MANAGED}=1",
            "-c",
            f"{LABEL_OWNER}={str(owner_id)}",
            "-c",
            f"{LABEL_VPS_ID}={vps_id or ''}",
        ]
        try:
            self._run(args, timeout=180)
            return image_uri
        except ProviderError as exc:
            self._safe_remove(name)
            if not self._is_missing_image_error(str(exc)):
                raise ProviderError(f"Failed to create instance: {exc}") from exc
            logger.warning("image %s missing, trying fallbacks: %s", image_uri, exc)

        candidates = self._image_candidates(image_uri)
        if image_uri in candidates:
            candidates = [c for c in candidates if c != image_uri] + [image_uri]
        last_err: Exception | None = None
        for alt in candidates:
            args_alt = list(args)
            args_alt[1] = alt
            try:
                self._run(args_alt, timeout=180)
                logger.info("Launched %s with fallback image %s", name, alt)
                return alt
            except ProviderError as exc:
                self._safe_remove(name)
                last_err = exc
                if not self._is_missing_image_error(str(exc)):
                    raise ProviderError(f"Failed to create instance: {exc}") from exc
                logger.warning("image %s failed: %s", alt, exc)
                continue
        raise ProviderError(
            "Failed to create instance: no usable image "
            f"(tried {', '.join(candidates)}). "
            f"Last error: {last_err}"
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
        notes: list[str] = []

        image_uri = self._launch_instance(
            image_uri=self._resolve_image(image),
            name=name,
            owner_id=owner_id,
            memory_mb=memory_mb,
            cpus=cpus,
            vps_id=vps_id,
        )

        code, _ = self._run(
            [
                "config",
                "device",
                "override",
                name,
                "root",
                f"size={int(disk_gb)}GB",
            ],
            check=False,
            timeout=30,
        )
        if code != 0:
            notes.append(
                f"Disk quota {disk_gb}GB may not be enforced on this storage backend."
            )

        try:
            self._wait_exec_ready(name, timeout=90)
            self._bootstrap(name, password)
            ip = self._instance_ip(name)
            self._wait_ssh(name, timeout=90)
            ip = self._instance_ip(name) or ip
            return VPSResult(
                container_id=name,
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
            self._safe_remove(name)
            raise

    @staticmethod
    def _bootstrap_script(password: str) -> str:
        return (
            "set -e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then "
            "  apt-get update -qq && apt-get install -y -qq "
            "openssh-server sudo curl wget ca-certificates bash; "
            "elif command -v apk >/dev/null 2>&1; then "
            "  apk add --no-cache openssh-server sudo curl wget bash; "
            "fi; "
            "mkdir -p /var/run/sshd /root/.ssh; "
            "chmod 700 /root/.ssh; "
            "echo 'root:" + password + "' | chpasswd; "
            "if [ -f /etc/ssh/sshd_config ]; then "
            "  sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config; "
            "  sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
            "  sed -i 's/^#\\?PermitEmptyPasswords.*/PermitEmptyPasswords no/' /etc/ssh/sshd_config; "
            "fi; "
            "if command -v ssh-keygen >/dev/null 2>&1 && [ ! -f /etc/ssh/ssh_host_rsa_key ]; then "
            "  ssh-keygen -A; "
            "fi; "
            "if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then "
            "  systemctl enable ssh >/dev/null 2>&1 || systemctl enable sshd >/dev/null 2>&1 || true; "
            "  systemctl restart ssh >/dev/null 2>&1 || systemctl restart sshd >/dev/null 2>&1 || true; "
            "elif command -v rc-service >/dev/null 2>&1; then "
            "  rc-update add sshd default >/dev/null 2>&1 || true; "
            "  rc-service sshd restart >/dev/null 2>&1 || rc-service sshd start >/dev/null 2>&1 || true; "
            "else "
            "  (setsid /usr/sbin/sshd -e >/tmp/vex-sshd.log 2>&1 &); "
            "fi; "
            "echo BOOTSTRAP_OK"
        )

    def _wait_exec_ready(self, name: str, timeout: int = 90) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.status(name) == "missing":
                raise ProviderError("Instance disappeared during startup")
            code, _ = self._run(
                ["exec", name, "--", "true"], check=False, timeout=15
            )
            if code == 0:
                return
            time.sleep(1)
        raise ProviderError("Instance never became ready for exec")

    def _bootstrap(self, name: str, password: str) -> None:
        script = self._bootstrap_script(password)
        args = ["exec", name, *self._exec_flags, "--", "sh", "-lc", script]
        code, out = self._run(args, check=False, timeout=240)
        if code != 0:
            raise ProviderError(
                f"SSH bootstrap failed (exit {code}): {out.strip()[:500]}"
            )
        if "BOOTSTRAP_OK" not in out:
            logger.warning("bootstrap finished without OK marker: %s", out[-400:])

    def _instance_json(self, name: str) -> dict | None:
        code, out = self._run(
            ["list", name, "--format", "json"], check=False, timeout=30
        )
        if code != 0:
            return None
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError:
            return None
        if isinstance(data, list) and data:
            return data[0]
        return None

    def _instance_ip(self, name: str) -> str:
        inst = self._instance_json(name)
        if not inst:
            return ""
        network = ((inst.get("state") or {}).get("network")) or {}
        candidates = []
        for iface, info in network.items():
            if iface == "lo":
                continue
            for addr in info.get("addresses") or []:
                if addr.get("family") != "inet":
                    continue
                ip = str(addr.get("address") or "").strip()
                if not ip:
                    continue
                if addr.get("scope") == "link":
                    candidates.append(ip)
                else:
                    return ip
        return candidates[0] if candidates else ""

    def _wait_ssh(self, name: str, timeout: int = 90) -> None:
        probe = (
            "nc -z 127.0.0.1 22 >/dev/null 2>&1 || "
            "grep -qE ':0016[[:space:]]' /proc/net/tcp 2>/dev/null || "
            "ss -lnt 2>/dev/null | grep -q ':22'"
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status(name)
            if st == "missing":
                raise ProviderError("Instance stopped during bootstrap")
            if st == "running":
                code, _ = self._run(
                    ["exec", name, *self._exec_flags, "--", "sh", "-lc", probe],
                    check=False,
                    timeout=15,
                )
                if code == 0:
                    return
            time.sleep(2)
        logger.warning("SSH readiness check timed out for %s", name)

    # ── lifecycle ───────────────────────────────────────────
    def get_container(self, container_id: str) -> str:
        code, _ = self._run(["info", container_id], check=False, timeout=30)
        if code != 0:
            raise ProviderError("Instance not found")
        return container_id

    def start(self, container_id: str) -> None:
        code, out = self._run(["start", container_id], check=False, timeout=90)
        if code != 0:
            low = (out or "").lower()
            if "already running" in low or "instance is already running" in low:
                return
            raise ProviderError(f"Start failed: {out.strip()[:400]}")

    def stop(self, container_id: str, timeout: int = 10) -> None:
        args = ["stop", container_id]
        if timeout and int(timeout) > 0:
            args += ["--timeout", str(int(timeout))]
        else:
            args.append("--force")
        code, out = self._run(args, check=False, timeout=max(60, int(timeout) + 30))
        if code != 0:
            low = (out or "").lower()
            if "not running" in low or "already stopped" in low:
                return
            raise ProviderError(f"Stop failed: {out.strip()[:400]}")

    def restart(self, container_id: str, timeout: int = 10) -> None:
        args = ["restart", container_id]
        if timeout and int(timeout) > 0:
            args += ["--timeout", str(int(timeout))]
        code, out = self._run(args, check=False, timeout=max(90, int(timeout) + 30))
        if code != 0:
            raise ProviderError(f"Restart failed: {out.strip()[:400]}")

    def remove(self, container_id: str, force: bool = True) -> None:
        args = ["delete", container_id]
        if force:
            args.append("--force")
        code, out = self._run(args, check=False, timeout=90)
        if code != 0:
            low = (out or "").lower()
            if "not found" in low or "does not exist" in low:
                return
            raise ProviderError(f"Delete failed: {out.strip()[:400]}")

    def _safe_remove(self, container_id: str) -> None:
        try:
            self.remove(container_id, force=True)
        except Exception as exc:
            logger.error("Cleanup failed for %s: %s", container_id, exc)

    def status(self, container_id: str) -> str:
        code, out = self._run(
            ["list", container_id, "--format", "csv", "-c", "s"],
            check=False,
            timeout=30,
        )
        if code != 0 or not out.strip():
            return "missing"
        raw = out.strip().splitlines()[0].strip().lower()
        if raw == "running":
            return "running"
        if raw in {"stopped", "frozen"}:
            return "stopped"
        if raw in {"ready", "started"}:
            return "running"
        if raw in {"error", "errored"}:
            return "missing"
        return raw or "missing"

    def set_password(self, container_id: str, password: str) -> None:
        code, out = self.exec_command(
            container_id, f"echo 'root:{password}' | chpasswd", timeout=30
        )
        if code != 0:
            raise ProviderError(
                "Failed to update SSH password inside instance"
            )

    def exec_command(
        self, container_id: str, command: str, timeout: int = 60
    ) -> tuple[int, str]:
        args = [
            "exec",
            container_id,
            *self._exec_flags,
            "--",
            "bash",
            "-lc",
            command,
        ]
        code, out = self._run(args, check=False, timeout=timeout)
        return code, out

    def logs(self, container_id: str, tail: int = 50) -> str:
        n = max(1, int(tail))
        code, raw = self._run(
            ["info", container_id, "--show-log"], check=False, timeout=30
        )
        if code == 0 and (raw or "").strip():
            lines = raw.strip().splitlines()
            return "\n".join(lines[-n:])
        code2, out = self.exec_command(
            container_id,
            "journalctl -n %d --no-pager 2>/dev/null || "
            "dmesg 2>/dev/null | tail -n %d || true" % (n, n),
            timeout=30,
        )
        if code2 == 0:
            return out
        return raw or out or ""

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

# tmate: static binary fallback (image may lack package / universe)
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

    def _run_script(
        self, container_id: str, script: str, timeout: int = 60
    ) -> tuple[int, str]:
        """Write script to /tmp and execute with an outer timeout (avoids quoting issues)."""
        import base64

        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        wrapper = (
            f"echo {b64} | base64 -d > /tmp/vex-run.sh && "
            f"chmod +x /tmp/vex-run.sh && "
            f"timeout {max(5, int(timeout))} bash /tmp/vex-run.sh"
        )
        return self.exec_command(container_id, wrapper, timeout=timeout + 15)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        import re

        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "")
        text = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", text)
        return text.replace("\x1b", "")

    @staticmethod
    def _extract_sshx_url(text: str) -> str:
        import re

        text = LXDProvider._strip_ansi(text)
        for pat in (
            r"https://sshx\.io/\S+",
            r"ssh\s+\S+@sshx\.io\S*",
            r"sshx\.io/\S+",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                url = m.group(0).strip().rstrip(".,);'\"")
                url = re.sub(r"[\x00-\x1f]+$", "", url)
                return url
        return ""

    @staticmethod
    def _extract_tmate_ssh(text: str) -> str:
        import re

        text = LXDProvider._strip_ansi(text)
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
                hint = " Instance may have no outbound network to tmate.io."
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
        """Best-effort stop of sshx/tmate sessions inside an instance."""
        if tool in ("sshx", "all"):
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

    @staticmethod
    def _extract_pinggy_url(text: str) -> str:
        import re

        text = LXDProvider._strip_ansi(text)
        m = re.search(r"https://[A-Za-z0-9._-]+pinggy\.[A-Za-z]+", text, re.IGNORECASE)
        return m.group(0).strip().rstrip(".,);'\"") if m else ""

    def start_file_manager(self, container_id: str, timeout: int = 75) -> dict:
        """Start the in-VPS file manager on localhost and expose it via Pinggy."""
        from filemanager import build_start_script

        token = secrets.token_urlsafe(16)
        port = 8765
        script = build_start_script(token, port)
        try:
            code, out = self._run_script(container_id, script, timeout=timeout)
        except Exception as exc:
            raise ProviderError(f"file manager exec failed: {exc}") from exc
        out = self._strip_ansi(out or "")
        url = self._extract_pinggy_url(out)
        if not url:
            code2, out2 = self.exec_command(
                container_id,
                "cat /tmp/vex-pinggy.log 2>/dev/null || true",
                timeout=15,
            )
            url = self._extract_pinggy_url(out2 or "")
            del code2
        url = (url or "").strip()
        if not url or "FM_OK" not in out:
            tail = out[-600:].strip()
            raise ProviderError(
                f"Pinggy tunnel did not come up (exit {code}): {tail}"
            )
        if "?" in url:
            full_url = f"{url}&token={token}"
        else:
            full_url = f"{url}?token={token}"
        return {"url": full_url, "token": token, "port": port}

    def stop_file_manager(self, container_id: str) -> None:
        from filemanager import build_stop_script

        try:
            self._run_script(container_id, build_stop_script(), timeout=20)
        except Exception as exc:
            raise ProviderError(f"file manager stop failed: {exc}") from exc

    def _cpu_sample(self, name: str) -> tuple[float | None, float]:
        t = time.time()
        code, out = self._run(
            ["query", f"/1.0/instances/{name}/state"], check=False, timeout=30
        )
        if code != 0:
            return None, t
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            return None, t
        usage = float((data.get("cpu") or {}).get("usage") or 0)
        return usage, t

    def stats(self, container_id: str) -> dict:
        inst = self._instance_json(container_id)
        if not inst:
            raise ProviderError("Instance not found")
        status_raw = str(inst.get("status") or "").lower()
        status = "running" if status_raw in {"running", "ready"} else status_raw or "unknown"
        if status_raw in {"frozen"}:
            status = "stopped"
        state = inst.get("state") or {}
        mem = float((state.get("memory") or {}).get("usage") or 0)
        cpu_percent = 0.0
        if status == "running":
            c1, t1 = self._cpu_sample(container_id)
            time.sleep(0.5)
            c2, t2 = self._cpu_sample(container_id)
            if c1 is not None and c2 is not None and t2 > t1:
                delta_ns = max(0.0, c2 - c1)
                wall_ns = (t2 - t1) * 1_000_000_000
                if wall_ns > 0:
                    cpu_percent = (delta_ns / wall_ns) * 100.0
        started_at = str(inst.get("created_at") or "")
        pid = 0
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return {
            "status": status,
            "cpu_percent": round(cpu_percent, 2),
            "mem_used_mb": round(mem / (1024 * 1024), 2),
            "started_at": started_at,
            "pid": pid,
        }

    def ping(self) -> bool:
        try:
            code, _ = self._run(["version"], check=False, timeout=15)
            return code == 0
        except ProviderError:
            return False
