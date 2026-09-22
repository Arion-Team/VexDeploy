"""MOTD script generation and installation for VexDeploy."""

from __future__ import annotations

import base64
import logging
import secrets
from pathlib import Path
from typing import Any

logger = logging.getLogger("vexdeploy.motd")

_EOF = "VEX_MOTD_EOF"
_MARKER = "VEXDEPLOY_MOTD"

_template_path = Path(__file__).resolve().parent / "templates" / "motd.sh"


def generate_ascii_logo(text: str, width: int = 56) -> str:
    text = (text or "").strip() or "VexDeploy"
    font = {
        "A": ["  #  ", " # # ", "#####", "#   #", "#   #"],
        "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
        "C": [" ####", "#    ", "#    ", "#    ", " ####"],
        "D": ["#### ", "#   #", "#   #", "#   #", "#### "],
        "E": ["#####", "#    ", "#### ", "#    ", "#####"],
        "F": ["#####", "#    ", "#### ", "#    ", "#    "],
        "G": [" ####", "#    ", "#  ##", "#   #", " ####"],
        "H": ["#   #", "#   #", "#####", "#   #", "#   #"],
        "I": ["#####", "  #  ", "  #  ", "  #  ", "#####"],
        "J": ["    #", "    #", "    #", "#   #", " ####"],
        "K": ["#   #", "#  # ", "###  ", "#  # ", "#   #"],
        "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
        "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
        "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
        "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
        "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
        "Q": [" ### ", "#   #", "# # #", "#  # ", " ## #"],
        "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
        "S": [" ####", "#    ", " ### ", "    #", "#### "],
        "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
        "U": ["#   #", "#   #", "#   #", "#   #", " ### "],
        "V": ["#   #", "#   #", "#   #", " # # ", "  #  "],
        "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
        "X": ["#   #", " # # ", "  #  ", " # # ", "#   #"],
        "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
        "Z": ["#####", "   # ", "  #  ", " #   ", "#####"],
        "0": [" ### ", "#  ##", "# # #", "##  #", " ### "],
        "1": ["  #  ", " ##  ", "  #  ", "  #  ", " ### "],
        "2": [" ### ", "#   #", "   # ", "  #  ", "#####"],
        "3": ["#### ", "    #", " ### ", "    #", "#### "],
        "4": ["#  # ", "#  # ", "#####", "   # ", "   # "],
        "5": ["#####", "#    ", "#### ", "    #", "#### "],
        "6": [" ### ", "#    ", "#### ", "#   #", " ### "],
        "7": ["#####", "    #", "   # ", "  #  ", "  #  "],
        "8": [" ### ", "#   #", " ### ", "#   #", " ### "],
        "9": [" ### ", "#   #", " ####", "    #", " ### "],
        " ": ["     ", "     ", "     ", "     ", "     "],
        "-": ["     ", "     ", " ### ", "     ", "     "],
        ".": ["     ", "     ", "     ", "     ", "  #  "],
    }

    max_chars = max(1, min(len(text), (width - 4) // 6))
    sample = text.upper()[:max_chars]

    rows = [""] * 5
    for ch in sample:
        glyph = font.get(ch, font[" "])
        for i in range(5):
            rows[i] += glyph[i] + " "

    if not any(r.strip() for r in rows):
        return framed(text, width)

    out = ["  " + r.rstrip() for r in rows]
    return "\n".join(out)


def framed(text: str, width: int = 56) -> str:
    text = (text or "").strip() or "VexDeploy"
    inner = width - 4
    if len(text) > inner:
        text = text[:inner]
    pad = inner - len(text)
    left = pad // 2
    right = pad - left
    return (
        "+" + "-" * (width - 2) + "+\n"
        "| " + " " * left + text + " " * right + " |\n"
        "+" + "-" * (width - 2) + "+"
    )


def _ansi(code: str) -> str:
    return f"\\033[{code}m"


def build_motd_script(brand: dict[str, Any]) -> str:
    name = str(brand.get("brand_name") or "VexDeploy")
    tagline = str(brand.get("brand_tagline") or "")
    footer = str(brand.get("footer") or "")
    website = str(brand.get("website") or "")
    discord_url = str(brand.get("discord") or "")
    support = str(brand.get("support_email") or "")
    logo = str(brand.get("logo") or "AUTO")
    template = str(brand.get("motd_template") or "").strip()

    from config import ANSI_COLORS

    p = ANSI_COLORS.get(str(brand.get("primary_color", "cyan")), "0;36")
    s = ANSI_COLORS.get(str(brand.get("secondary_color", "magenta")), "0;35")

    if logo.upper() in {"NONE", "OFF"}:
        logo_block = ""
    elif logo.upper() == "AUTO" or not logo:
        logo_block = generate_ascii_logo(name)
    else:
        logo_block = logo

    if template:
        return _script_from_template(brand, template, p, s)

    footer_line = footer or (f"Need help? {support}" if support else "")
    links = " | ".join(x for x in (website, discord_url, support) if x)

    logo_export = ""
    if logo_block:
        logo_export = (
            "cat <<'" + _EOF + "_LOGO'\n" + logo_block + "\n" + _EOF + "_LOGO\n"
        )

    tagline_echo = (
        f'echo "${{S}}{tagline}${{R}}"' if tagline else 'echo ""'
    )
    links_echo = f'echo "${{S}}{links}${{R}}"' if links else 'echo ""'
    footer_echo = (
        f'echo "${{S}}{footer_line}${{R}}"' if footer_line else 'echo ""'
    )
    support_echo = (
        f'echo "${{B}}Support${{R}}  {support}"' if support else 'echo ""'
    )

    return f"""#!/bin/bash
# {_MARKER} v1 brand={name}
P="{_ansi(p)}"
S="{_ansi(s)}"
B="\\033[1m"
R="\\033[0m"

HOST="$(hostname 2>/dev/null || echo vex)"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS="${{PRETTY_NAME:-Linux}}"
else
  OS="$(uname -s)"
fi
KERNEL="$(uname -r)"
UPTIME_STR="$(uptime -p 2>/dev/null || uptime | sed 's/.*up /up /')"
CPU="$(nproc 2>/dev/null || echo 1)"
MEM_TOTAL="$(free -h 2>/dev/null | awk '/Mem:/{{print $2}}')"
MEM_USED="$(free -h 2>/dev/null | awk '/Mem:/{{print $3}}')"
MEM_PCT="$(free | awk '/Mem:/{{printf "%.0f", $3/$2*100}}' 2>/dev/null || echo 0)"
DISK="$(df -h / 2>/dev/null | awk 'NR==2{{print $3"/"$2" ("$5)"}}')"
IP="$(hostname -I 2>/dev/null | awk '{{print $1}}')"
LOAD="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo n/a)"
USERS="$(who 2>/dev/null | wc -l)"
PROCS="$(ps -e --no-headers 2>/dev/null | wc -l)"

echo ""
{logo_export}
echo "${{P}}${{B}}{name}${{R}}"
{tagline_echo}
echo "${{P}}----------------------------------------------${{R}}"
echo "${{B}} Host${{R}}     $HOST"
echo "${{B}} OS${{R}}       $OS"
echo "${{B}} Kernel${{R}}   $KERNEL"
echo "${{B}} Uptime${{R}}   $UPTIME_STR"
echo "${{B}} CPU${{R}}      $CPU core(s)  load $LOAD"
echo "${{B}} Memory${{R}}   $MEM_USED/$MEM_TOTAL ($MEM_PCT%)"
echo "${{B}} Disk${{R}}     $DISK"
echo "${{B}} Processes${{R}} $PROCS"
echo "${{B}} Users${{R}}    $USERS online"
echo "${{B}} IP${{R}}       $IP"
echo "${{P}}----------------------------------------------${{R}}"
{links_echo}
{footer_echo}
{support_echo}
echo ""
"""


def _script_from_template(
    brand: dict[str, Any], template: str, p_code: str, s_code: str
) -> str:
    replacements = {
        "brand_name": str(brand.get("brand_name", "VexDeploy")),
        "tagline": str(brand.get("brand_tagline", "")),
        "footer": str(brand.get("footer", "")),
        "website": str(brand.get("website", "")),
        "discord": str(brand.get("discord", "")),
        "support": str(brand.get("support_email", "")),
        "hostname": "$(hostname)",
        "os": '$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)',
        "kernel": "$(uname -r)",
        "uptime": "$(uptime -p 2>/dev/null || uptime)",
        "cpu": "$(nproc 2>/dev/null || echo 1)",
        "ram_used": "$(free -h 2>/dev/null | awk '/Mem:/ {print $3}')",
        "ram_total": "$(free -h 2>/dev/null | awk '/Mem:/ {print $2}')",
        "ram_percent": "$(free 2>/dev/null | awk '/Mem:/ {printf \"%d\", $3/$2*100}')",
        "disk": "$(df -h / 2>/dev/null | awk 'NR==2 {print $3\"/\"$2\" (\"$5)\")}')",
        "ip": "$(hostname -I 2>/dev/null | awk '{print $1}')",
        "users": "$(who 2>/dev/null | wc -l)",
        "processes": "$(ps -e --no-headers 2>/dev/null | wc -l)",
    }

    body = template
    for key, expr in replacements.items():
        body = body.replace("{" + key + "}", expr)

    return f"""#!/bin/bash
# {_MARKER} v1 template brand={brand.get('brand_name')}
P="{_ansi(p_code)}"
S="{_ansi(s_code)}"
B="\\033[1m"
R="\\033[0m"
echo ""
echo "${{P}}{name_echo(brand)}${{R}}"
{f'echo "${{S}}{brand.get("tagline", brand.get("brand_tagline", ""))}${{R}}"' if brand.get("brand_tagline") else ''}
cat <<'{_EOF}'
{body}
{_EOF}
echo "${{S}}{str(brand.get("footer") or "")}${{R}}"
echo ""
"""


def name_echo(brand: dict[str, Any]) -> str:
    return str(brand.get("brand_name") or "VexDeploy")


def build_installer(brand: dict[str, Any]) -> str:
    script = build_motd_script(brand)
    brand_name = str(brand.get("brand_name") or "VexDeploy")
    return f"""#!/bin/bash
set -e
# {_MARKER} installer brand={brand_name}

# backups (once)
for f in /etc/pam.d/sshd /etc/pam.d/login /etc/motd; do
  if [ -f "$f" ] && [ ! -f "$f.backup-brand" ]; then
    cp -a "$f" "$f.backup-brand"
  fi
done

# disable default MOTD scripts (once)
if [ -d /etc/update-motd.d ] && [ ! -d /etc/update-motd.d.disabled ]; then
  mkdir -p /etc/update-motd.d.disabled
  find /etc/update-motd.d -maxdepth 1 -type f -exec mv {{}} /etc/update-motd.d.disabled/ \\; 2>/dev/null || true
fi

# ensure single pam_motd entry
for pam in /etc/pam.d/sshd /etc/pam.d/login; do
  [ -f "$pam" ] || continue
  if ! grep -q "{_MARKER}" "$pam" 2>/dev/null; then
    if grep -qE '^session\\s+optional\\s+pam_motd\\.so' "$pam"; then
      sed -i -E '/^session\\s+optional\\s+pam_motd\\.so/d' "$pam"
    fi
    echo "session optional pam_motd.so motd_dynamic=/run/motd.dynamic # {_MARKER}" >> "$pam"
  fi
done

# write installer script
mkdir -p /etc/update-motd.d
cat > /etc/update-motd.d/00-brand-motd <<'MOTD_EOF'
{script}
MOTD_EOF
chmod 755 /etc/update-motd.d/00-brand-motd

# prefill dynamic motd
if mkdir -p /run 2>/dev/null || [ -d /run ]; then
  /etc/update-motd.d/00-brand-motd > /run/motd.dynamic 2>/dev/null || true
  chmod 644 /run/motd.dynamic 2>/dev/null || true
fi

# clear static /etc/motd so PAM dynamic wins
: > /etc/motd 2>/dev/null || true

echo "MOTD_OK pam=ok scripts=ok brand={brand_name}"
"""


def encode_installer(installer: str) -> str:
    return base64.b64encode(installer.encode("utf-8")).decode("ascii")


def build_installer_payload(brand: dict[str, Any]) -> str:
    installer = build_installer(brand)
    b64 = encode_installer(installer)
    token = secrets.token_hex(6)
    path = f"/tmp/.vex-motd-{token}.sh"
    return (
        f"echo {b64} | base64 -d > {path} && chmod +x {path} && "
        f"bash {path}; ec=$?; rm -f {path}; exit $ec"
    )


def run_installer(exec_fn, brand: dict[str, Any]) -> tuple[bool, str]:
    payload = build_installer_payload(brand)
    try:
        code, output = exec_fn(payload)
    except Exception as exc:
        return False, str(exc)
    text = (output or "").strip()
    ok = "MOTD_OK" in text or code == 0
    return ok, text


def install_branding_files(exec_fn, brand: dict[str, Any]) -> tuple[bool, str]:
    name = str(brand.get("brand_name") or "VexDeploy")
    website = str(brand.get("website") or "")
    support = str(brand.get("support_email") or "")
    content = f"{name}\n{website}\n{support}\n"
    b64 = base64.b64encode(content.encode()).decode()
    cmd = (
        f"mkdir -p /etc/vexdeploy && echo {b64} | base64 -d > /etc/vexdeploy/brand && "
        f"chmod 644 /etc/vexdeploy/brand && echo BRAND_OK"
    )
    try:
        code, output = exec_fn(cmd)
    except Exception as exc:
        return False, str(exc)
    ok = code == 0 and "BRAND_OK" in (output or "")
    return ok, (output or "")
