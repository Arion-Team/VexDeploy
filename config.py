"""VexDeploy configuration — white-label defaults."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "vexdeploy.db"))
LOG_FILE = str(BASE_DIR / "vexdeploy.log")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
COMMAND_PREFIX = "!"

# Access
def _ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


ADMIN_IDS: set[int] = _ids(os.getenv("ADMIN_IDS", "1210291131301101618"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1376177459870961694") or 0)

# Docker defaults
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "vexdeploy")
DEFAULT_OS_IMAGE = os.getenv("DEFAULT_OS_IMAGE", "ubuntu:22.04")
MAX_CONTAINERS = int(os.getenv("MAX_CONTAINERS", "100"))

# Resource validation (defaults MUST pass validation)
MIN_MEMORY_MB = 512
MAX_MEMORY_MB = 65536
DEFAULT_MEMORY_MB = int(os.getenv("DEFAULT_MEMORY_MB", "1024"))

MIN_CPUS = 1
MAX_CPUS = 32
DEFAULT_CPUS = int(os.getenv("DEFAULT_CPUS", "1"))

MIN_DISK_GB = 5
MAX_DISK_GB = 1000
DEFAULT_DISK_GB = int(os.getenv("DEFAULT_DISK_GB", "10"))

MAX_VPS_PER_USER = int(os.getenv("MAX_VPS_PER_USER", "3"))

# Runtime settings seeded into DB
DEFAULT_SETTINGS: dict[str, str] = {
    "required_invites": "5",
    "vps_enabled": "1",
    "vps_cooldown_hours": "24",
    "max_total_vps": "20",
    "max_vps_per_user": str(MAX_VPS_PER_USER),
    "default_vps_memory": str(DEFAULT_MEMORY_MB),
    "default_vps_cpu": str(DEFAULT_CPUS),
    "default_vps_disk": str(DEFAULT_DISK_GB),
    "log_channel_id": "0",
    "completion_channel_id": "0",
    "autostart_on_ready": "1",
}

# White-label brand (admin-editable)
DEFAULT_BRAND: dict[str, object] = {
    "brand_name": "VexDeploy",
    "brand_tagline": "Instant VPS Hosting",
    "website": "https://vexdeploy.example",
    "discord": "https://discord.gg/example",
    "support_email": "support@vexdeploy.example",
    "motd_enabled": True,
    "motd_type": "premium",
    "primary_color": "cyan",
    "secondary_color": "magenta",
    "logo": "AUTO",
    "footer": "Powered by VexDeploy",
    "motd_template": "",
}

ANSI_COLORS: dict[str, str] = {
    "black": "0;30",
    "red": "0;31",
    "green": "0;32",
    "yellow": "0;33",
    "blue": "0;34",
    "magenta": "0;35",
    "cyan": "0;36",
    "white": "0;37",
    "bright_black": "1;30",
    "bright_red": "1;31",
    "bright_green": "1;32",
    "bright_yellow": "1;33",
    "bright_blue": "1;34",
    "bright_magenta": "1;35",
    "bright_cyan": "1;36",
    "bright_white": "1;37",
}

BRAND_FIELDS = {
    "brand_name": str,
    "brand_tagline": str,
    "website": str,
    "discord": str,
    "support_email": str,
    "motd_enabled": bool,
    "motd_type": str,
    "primary_color": str,
    "secondary_color": str,
    "logo": str,
    "footer": str,
    "motd_template": str,
}

OS_CHOICES = {
    "ubuntu:22.04": "Ubuntu 22.04",
    "ubuntu:24.04": "Ubuntu 24.04",
    "debian:12": "Debian 12",
    "alpine:3.20": "Alpine 3.20",
}


def parse_created_at(value: str | None):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
