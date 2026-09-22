import base64
import logging
from config import ANSI_COLORS

logger = logging.getLogger('LexoNodesBot')

DYNAMIC_SHELL = {
    'hostname': r"$(hostname 2>/dev/null || echo unknown)",
    'os': r"$(. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\" || uname -s)",
    'kernel': r"$(uname -r 2>/dev/null || echo unknown)",
    'uptime': r"$(uptime -p 2>/dev/null | sed 's/^up //' || awk '{printf \"%d days\", int($1/86400)}' /proc/uptime)",
    'cpu': r"$(cpu_usage 2>/dev/null || echo 0)%",
    'ram_used': r"$(awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{if(t) print int((t-a)/1024)}' /proc/meminfo 2>/dev/null || echo 0)MB",
    'ram_total': r"$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)MB",
    'ram_percent': r"$(awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{if(t) printf \"%d\", (t-a)*100/t; else printf \"0\"}' /proc/meminfo 2>/dev/null || echo 0)%",
    'disk': r"$(df -P / 2>/dev/null | awk 'NR==2{print $3\" / \"$2\" (\"$5\")\"}')",
    'ip': r"$(hostname -I 2>/dev/null | awk '{print $1}' || echo unknown)",
    'users': r"$(who 2>/dev/null | wc -l | tr -d ' ')",
    'processes': r"$(ps -e --no-headers 2>/dev/null | wc -l | tr -d ' ')",
}

LITERAL_KEYS = ('brand_name', 'tagline', 'footer', 'website', 'discord', 'support')


def _color_code(name, fallback='37'):
    return ANSI_COLORS.get((name or '').strip().lower(), fallback)


def _shell_single_quote(s):
    return str(s).replace("'", "'\\''")


def generate_ascii_logo(brand_name):
    """Block-style ASCII logo with a clean framed fallback."""
    name = (brand_name or 'VPS').strip().upper()
    art = {
        'A': ['  ##  ', ' #  # ', ' #### ', ' #  # ', ' #  # '],
        'B': [' ###  ', ' #  # ', ' ###  ', ' #  # ', ' ###  '],
        'C': [' #### ', ' #    ', ' #    ', ' #    ', ' #### '],
        'D': [' ###  ', ' #  # ', ' #  # ', ' #  # ', ' ###  '],
        'E': [' #### ', ' #    ', ' ###  ', ' #    ', ' #### '],
        'F': [' #### ', ' #    ', ' ###  ', ' #    ', ' #    '],
        'G': [' #### ', ' #    ', ' # ## ', ' #  # ', ' #### '],
        'H': [' #  # ', ' #  # ', ' #### ', ' #  # ', ' #  # '],
        'I': [' ### ', '  #  ', '  #  ', '  #  ', ' ### '],
        'J': ['   ## ', '    # ', '    # ', '#   # ', ' ###  '],
        'K': [' #  # ', ' # #  ', ' ##   ', ' # #  ', ' #  # '],
        'L': [' #    ', ' #    ', ' #    ', ' #    ', ' #### '],
        'M': [' #   # ', ' ## ## ', ' # # # ', ' #   # ', ' #   # '],
        'N': [' #   # ', ' ##  # ', ' # # # ', ' #  ## ', ' #   # '],
        'O': [' ###  ', ' #  # ', ' #  # ', ' #  # ', ' ###  '],
        'P': [' ###  ', ' #  # ', ' ###  ', ' #    ', ' #    '],
        'Q': [' ###  ', ' #  # ', ' #  # ', ' # ## ', ' ### #'],
        'R': [' ###  ', ' #  # ', ' ###  ', ' # #  ', ' #  # '],
        'S': [' #### ', ' #    ', ' ###  ', '    # ', ' #### '],
        'T': ['##### ', '  #   ', '  #   ', '  #   ', '  #   '],
        'U': [' #  # ', ' #  # ', ' #  # ', ' #  # ', ' ###  '],
        'V': [' #   # ', ' #   # ', '  # #  ', '  # #  ', '   #   '],
        'W': [' #   # ', ' #   # ', ' # # # ', ' ## ## ', ' #   # '],
        'X': [' #   # ', '  # #  ', '   #   ', '  # #  ', ' #   # '],
        'Y': [' #   # ', '  # #  ', '   #   ', '   #   ', '   #   '],
        'Z': [' #### ', '    # ', '   #  ', '  #   ', ' #### '],
        '0': [' ###  ', ' #  # ', ' #  # ', ' #  # ', ' ###  '],
        '1': ['  #  ', ' ##  ', '  #  ', '  #  ', ' ### '],
        '2': [' ###  ', '    # ', ' ###  ', ' #    ', ' #### '],
        '3': [' ###  ', '    # ', '  ##  ', '    # ', ' ###  '],
        '4': [' #  # ', ' #  # ', ' #### ', '    # ', '    # '],
        '5': [' #### ', ' #    ', ' ###  ', '    # ', ' ###  '],
        '6': [' ###  ', ' #    ', ' ###  ', ' #  # ', ' ###  '],
        '7': [' #### ', '    # ', '   #  ', '  #   ', '  #   '],
        '8': [' ###  ', ' #  # ', ' ###  ', ' #  # ', ' ###  '],
        '9': [' ###  ', ' #  # ', ' #### ', '    # ', ' ###  '],
        ' ': ['   ', '   ', '   ', '   ', '   '],
        '-': ['   ', '   ', ' ### ', '   ', '   '],
        '.': ['   ', '   ', '   ', '   ', '  # '],
        '_': ['     ', '     ', '     ', '     ', '#####'],
    }

    if not name:
        name = 'VPS'

    rows = ['', '', '', '', '']
    for ch in name:
        glyph = art.get(ch)
        if not glyph:
            width = max(len(name) + 4, 34)
            bar = '━' * width
            return f"{bar}\n        {name}\n{bar}"
        for i in range(5):
            rows[i] += glyph[i] + ' '
    return '\n'.join(rows).rstrip()


def _literals(brand):
    return {
        'brand_name': brand.get('brand_name', ''),
        'tagline': brand.get('brand_tagline', ''),
        'footer': brand.get('footer') or brand.get('brand_tagline', ''),
        'website': brand.get('website', ''),
        'discord': brand.get('discord', ''),
        'support': brand.get('support_email', ''),
    }


def _default_motd_body(brand):
    name = brand.get('brand_name', 'VPS')
    tagline = brand.get('brand_tagline') or 'High Performance • Secure • Reliable Infrastructure'
    footer = brand.get('footer') or tagline
    return f"""
$p_line
${{bold}}${{p_color}}\U0001f680 Welcome to {name}${{reset}}
{tagline}
${{p_color}}$p_line${{reset}}

Hostname:          ${{hostname}}
OS:                ${{os}}
Kernel:            ${{kernel}}
Uptime:            ${{uptime}}
CPU Usage:         ${{cpu}}
Memory:            ${{ram_used}} / ${{ram_total}} (${{ram_percent}})
Disk:              ${{disk}}
Processes:         ${{processes}}
Users:             ${{users}}
IP:                ${{ip}}

${{p_color}}$p_line${{reset}}

Support:           ${{support}}
Discord:           ${{discord}}
Website:           ${{website}}

${{p_color}}${{bold}}{name} — {footer} 💎${{reset}}
"""


def _render_custom_template(template, brand):
    out = template
    literals = _literals(brand)
    for key, val in literals.items():
        out = out.replace('{' + key + '}', str(val))
    for key, snippet in DYNAMIC_SHELL.items():
        if key in literals:
            continue
        if '{' + key + '}' in out:
            resolved = snippet
            for lk, lv in literals.items():
                resolved = resolved.replace('{' + lk + '}', str(lv))
            out = out.replace('{' + key + '}', resolved)
    # Leave any unknown {braces} untouched for visibility in output
    return out


def build_motd_script(brand):
    """Build the MOTD shell script for a brand profile."""
    try:
        with open('templates/motd.sh', 'r', encoding='utf-8') as f:
            base = f.read()
    except FileNotFoundError:
        base = (
            "#!/bin/bash\n"
            "p_color=$'\\033[{PRIMARY_COLOR}m'\n"
            "s_color=$'\\033[{SECONDARY_COLOR}m'\n"
            "reset=$'\\033[0m'\n"
            "bold=$'\\033[1m'\n"
            "p_line=\"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\"\n"
            "cpu_usage() { echo 0; }\n"
            "{LOGO_BLOCK}\n"
            "{MOTD_BODY}\n"
        )

    pcolor = _color_code(brand.get('primary_color'), '35')
    scolor = _color_code(brand.get('secondary_color'), '36')

    custom_logo = str(brand.get('logo') or 'AUTO').strip()
    if custom_logo and custom_logo.upper() != 'AUTO':
        logo = custom_logo
    else:
        logo = generate_ascii_logo(brand.get('brand_name', 'VPS'))

    logo_block = (
        f'while IFS= read -r _l || [ -n "$_l" ]; do '
        f'printf \'%s\\n\' "${{p_color}}${{_l}}${{reset}}"; '
        f'done << \'LOGO_EOF\'\n{logo}\nLOGO_EOF'
    )

    custom_template = (brand.get('motd_template') or '').strip()
    if custom_template:
        body = _render_custom_template(custom_template, brand)
    else:
        body = _default_motd_body(brand)

    script = base
    script = script.replace('{PRIMARY_COLOR}', pcolor)
    script = script.replace('{SECONDARY_COLOR}', scolor)
    script = script.replace('{LOGO_BLOCK}', logo_block)
    script = script.replace('{MOTD_BODY}', body)

    if not script.startswith('#!'):
        script = '#!/bin/bash\n' + script
    return script


INSTALLER_TEMPLATE = r'''
set -e
BACKUP_SUFFIX=".backup-brand"

# --- Backups (once, idempotent) ---
for f in /etc/pam.d/sshd /etc/pam.d/login /etc/motd; do
    if [ -f "$f" ] && [ ! -f "${f}${BACKUP_SUFFIX}" ]; then
        cp -p "$f" "${f}${BACKUP_SUFFIX}" || true
    fi
done
if [ -d /etc/update-motd.d ] && [ ! -d /etc/update-motd.d.backup-brand ]; then
    cp -a /etc/update-motd.d /etc/update-motd.d.backup-brand 2>/dev/null || true
fi

# --- Disable default MOTD scripts carefully (idempotent) ---
mkdir -p /etc/update-motd.d.disabled
if [ -d /etc/update-motd.d ]; then
    for f in /etc/update-motd.d/*; do
        [ -e "$f" ] || continue
        base=$(basename "$f")
        [ "$base" = "00-brand-motd" ] && continue
        if [ ! -e "/etc/update-motd.d.disabled/$base" ]; then
            mv "$f" "/etc/update-motd.d.disabled/$base" 2>/dev/null || true
        else
            rm -f "$f" 2>/dev/null || true
        fi
    done
fi

# Clear static / dynamic MOTD buffers to avoid duplicate output (backed up above)
: > /etc/motd 2>/dev/null || true
: > /run/motd.dynamic 2>/dev/null || true

# --- PAM: ensure pam_motd present exactly once (never duplicate) ---
if [ -f /etc/pam.d/sshd ] && ! grep -qE '^[[:space:]]*session[[:space:]].*pam_motd\.so' /etc/pam.d/sshd; then
    echo "session optional pam_motd.so" >> /etc/pam.d/sshd
fi
if [ -f /etc/pam.d/login ] && ! grep -qE '^[[:space:]]*session[[:space:]].*pam_motd\.so' /etc/pam.d/login; then
    echo "session optional pam_motd.so" >> /etc/pam.d/login
fi

# Disable Ubuntu motd-news if present (leave unrelated config intact)
if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now motd-news.timer >/dev/null 2>&1 || true
    systemctl stop motd-news.service >/dev/null 2>&1 || true
fi

# --- Install branded MOTD script (overwrite = idempotent) ---
mkdir -p /etc/update-motd.d
chmod 755 /etc/update-motd.d 2>/dev/null || true

cat > /etc/update-motd.d/00-brand-motd << 'BRAND_MOTD_EOF'
{SCRIPT}
BRAND_MOTD_EOF
chmod 755 /etc/update-motd.d/00-brand-motd

# Prefill once so SSH shows branding even before first manual run
/etc/update-motd.d/00-brand-motd > /run/motd.dynamic 2>/dev/null || true

CNT_PAM=$(grep -cE '^[[:space:]]*session[[:space:]].*pam_motd\.so' /etc/pam.d/sshd 2>/dev/null || echo 0)
CNT_SCRIPT=$(ls /etc/update-motd.d/00-brand-motd 2>/dev/null | wc -l | tr -d ' ')
echo "MOTD_OK pam=$CNT_PAM scripts=$CNT_SCRIPT"
'''


def build_installer(brand):
    script = build_motd_script(brand)
    return INSTALLER_TEMPLATE.replace('{SCRIPT}', script)


def encode_installer(brand):
    return base64.b64encode(build_installer(brand).encode('utf-8')).decode('ascii')


async def run_installer(exec_fn, brand):
    """Run the branded MOTD installer through a provider exec adapter.

    exec_fn(b64_payload) -> (ok, output)
    Returns (success, message).
    """
    if not brand or not int(brand.get('motd_enabled') or 0):
        return False, "MOTD disabled in branding configuration"

    payload = encode_installer(brand)
    logger.info("MOTD installation started")
    try:
        ok, output = await exec_fn(payload)
    except Exception as e:
        logger.error(f"MOTD installation failed: {e}")
        return False, str(e)

    if ok and 'MOTD_OK' in (output or ''):
        logger.info("MOTD installation completed")
        return True, "MOTD installed"
    logger.error(f"MOTD installation failed: {output}")
    return False, (output or 'Unknown installer error')[:500]


async def install_branding_files(exec_fn, brand):
    """Write lightweight branding files inside the VPS."""
    name = _shell_single_quote(brand.get('brand_name') or 'VPS')
    website = _shell_single_quote(brand.get('website') or '')
    tagline = _shell_single_quote(brand.get('brand_tagline') or '')
    support = _shell_single_quote(brand.get('support_email') or '')
    discord_url = _shell_single_quote(brand.get('discord') or '')
    raw = (
        f"printf 'PRETTY_HOSTNAME=\"%s\"\\n' '{name}' > /etc/machine-info; "
        f"printf '%s\\n' '{name} | {tagline} | {website}' > /etc/brand-release; "
        f"printf '%s\\n' 'Support: {support}' 'Discord: {discord_url}' 'Website: {website}' >> /etc/brand-release; "
        f"echo BRANDING_OK"
    )
    payload = base64.b64encode(raw.encode('utf-8')).decode('ascii')
    try:
        ok, output = await exec_fn(payload)
    except Exception as e:
        logger.error(f"Branding file install failed: {e}")
        return False, str(e)
    if ok and 'BRANDING_OK' in (output or ''):
        return True, "Branding files installed"
    return False, (output or 'Branding install failed')[:500]
