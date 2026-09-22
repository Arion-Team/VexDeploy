# VexDeploy

White-label Discord VPS deployment bot — invite-gated access, clean LXD/Incus provisioning, and automatic branded SSH MOTD.

Built as a full clean rewrite with **VexDeploy** as the default brand. No legacy provider code.

---

## Features

- **Invite-gated `/createvps`** — users must hit a configurable invite goal
- **Plan catalog + creation UI** — `/plans`, `/createplan`, pick plan + OS in Discord, custom resources modal
- **12 OS images** — Ubuntu, Debian, Alpine, Rocky, Alma, Fedora, Oracle, openSUSE
- **Invite tracking** — join attribution, leave invalidation, dedup, no credit on ambiguity
- **Completion alerts** — one-time DM + optional channel ping when goal is reached
- **Clean LXD provider** — resource limits (RAM/CPU/disk), network, labels, bootstrap SSH
- **Post-deploy branding** — brand files + idempotent MOTD installer on every new VPS
- **Multi-profile branding** — versioned fields, switchable profiles, white-label ready (AytroCloud scrubbed)
- **Secure credentials** — passwords delivered via DM spoilers only
- **Web file manager** — browse/upload/edit/delete files over a token-protected localhost HTTP server, exposed via **Pinggy** (`/file_manager` or dashboard **📁 Files**)
- **SQLite** — zero external database
- **Auto-stop on bot offline** — when the bot shuts down (SIGINT/SIGTERM/disconnect), every managed VPS is stopped and marked `stopped` in the DB
- **Auto-start on bot ready** — when the bot reconnects, stopped VPS instances are started again (toggle via `autostart_on_ready` in settings; default `1`)

---

## Architecture

```text
Discord slash command
        |
Invite / cooldown / blacklist / limits check
        |
LXDProvider.create_vps()
        |
SQLite instance row
        |
Post-deploy: brand files + MOTD installer
        |
Credentials via DM
```

### Layout

```text
bot.py           # Discord bot + all slash commands
config.py        # defaults, env, resource limits, brand seed
database.py      # SQLite layer
provider.py      # clean LXD/Incus VPS provider
invites.py       # invite tracker + completion notifications
branding.py      # branding manager + embed
motd.py          # MOTD generator + idempotent installer
templates/       # MOTD variable reference
requirements.txt
.env.example
```

Runtime files: `vexdeploy.db`, `vexdeploy.log`.

---

## Requirements

- Python 3.10+
- LXD or Incus reachable by the bot process (`lxc` or `incus` CLI; set `LXD_CLI` to override)
- Discord bot token with **Server Members Intent**

```bash
pip install -r requirements.txt
```

---

## Setup

```bash
git clone https://github.com/Arion-Team/VexDeploy.git
cd VexDeploy
# install/configure LXD or Incus (remotes, network, .env LXD_CLI)
sudo bash setup_lxd.sh
cp .env.example .env
# edit .env — set DISCORD_TOKEN
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### .env

```env
DISCORD_TOKEN=
ADMIN_IDS=1210291131301101618
ADMIN_ROLE_ID=1376177459870961694
DATABASE_PATH=vexdeploy.db
LXD_NETWORK=vexdeploy
LXD_CLI=
DEFAULT_OS_IMAGE=ubuntu:22.04
MAX_CONTAINERS=100
MAX_VPS_PER_USER=3
DEFAULT_MEMORY_MB=1024
DEFAULT_CPUS=1
DEFAULT_DISK_GB=10
```

Resource validation:

| Resource | Min | Default | Max |
|----------|-----|---------|-----|
| Memory | 512 MB | 1024 MB | 65536 MB |
| CPU | 1 | 1 | 32 |
| Disk | **5 GB** | **10 GB** | 1000 GB |

Defaults always pass validation (fixes the old 5GB / min-10GB mismatch).

---

## Commands

### User

`/createvps` (plan + OS picker UI) `/plans` `/invites` `/leaderboard` `/vps` `/list` `/manage_vps` `/file_manager` `/stop_file_manager` `/connect_vps` `/vps_stats` `/change_ssh_password` `/vps_shell` `/vps_console` `/vps_usage` `/transfer_vps` `/refresh-motd` `/help`

### Admin — access

`/setinvites` `/addinvites` `/removeinvites` `/resetinvites` `/resetcooldown` `/blacklist` `/unblacklist` `/ban_user` `/unban_user` `/list_banned` `/vps-enable` `/vps-disable` `/setlogchannel` `/setcompletionchannel` `/add_admin` `/remove_admin` `/list_admins`

### Admin — VPS

`/create_vps` `/vps_list` `/delete_vps` `/suspend_vps` `/unsuspend_vps` `/edit_vps` `/emergency_stop` `/emergency_remove` `/admin_stats` `/global_stats` `/system_info` `/cleanup_vps` `/backup_data` `/restore_data` `/container_limit` `/reinstall_bot`

### Admin — plans

`/createplan` `/editplan` `/deleteplan` `/listplans`

Seed via `.env`:

```env
PLANS=Starter:1024:1:10:Free,Pro:2048:2:25:Popular:$4,Business:4096:4:50:Best
```

### Admin — branding

`/brand` `/brand-name` `/brand-tagline` `/brand-website` `/brand-discord` `/brand-support` `/brand-motd` `/brand-colors` `/brand-reset` `/brand-create` `/brand-use` `/brand-list` `/brand-template` `/brand-reinstall` `/brand-update-existing`

---

## VPS dashboard (`/manage_vps`)

`/manage_vps <vps_id>` opens an interactive button dashboard (ephemeral, owner/admin only):

| Button | Action |
|--------|--------|
| ▶ Start / ⏹ Stop / ↻ Restart | Lifecycle |
| 📊 Stats | Live CPU/memory/disk, plan, image, IP |
| 🌐 Network | Addresses, gateway, listening ports, public IP |
| 📁 Files | Web file manager (browse/upload/edit/delete) via **Pinggy** tunnel |
| 📋 Logs | Last 50 lines of container logs |
| 🎨 Rebrand | Push current MOTD + `/etc/issue` banners |
| 🔑 SSH | DM: normal SSH + password + **sshx** (+ tmate if sshx fails) |
| 🔐 Password | Modal — change SSH password in place |
| ⚡ Command | Modal — run a shell command, show exit code + output |
| 🔁 Reinstall | Two-step confirm — recreates container (same plan/owner), wipes data |
| 🗑 Delete | Two-step confirm — removes container + DB row |

### Login branding (MOTD + issue)

On SSH login, users see the provider name and host details:

```text
  VexDeploy — VPS Provider Hosting
  Instant VPS Hosting
  ─────────────────────────────
  Provider   VexDeploy
  Host / OS / CPU / Memory / Disk / IP …
  Website / Discord / Support
  Powered by VexDeploy
```

Also written to `/etc/issue` + `/etc/issue.net` (console/SSH pre-auth banner)
and `/etc/vexdeploy/brand`. Re-push anytime with dashboard **🎨 Rebrand** or
`/refresh-motd` / `/brand-reinstall`.

### Reverse SSH (sshx / tmate)

Started from the dashboard **SSH** button when the container has no public IP:

- Tools install on demand inside the container (curl + package manager)
- sshx is detached via PID file (never `pkill -f` — that killed the launcher)
- tmate polls `#{tmate_ssh}` / `#{tmate_ssh_ro}` with diagnostics on timeout
- Session lives while the container runs
- Normal `ssh user@ip -p 22` still works via `/connect_vps` / `/vps_shell`

### Web file manager (Pinggy)

From the dashboard **📁 Files** button or `/file_manager <vps_id>`:

- Python stdlib HTTP server on `127.0.0.1:8765` inside the VPS (no public IP needed)
- Reverse tunnel via free **Pinggy** (`ssh -p 443 … a.pinggy.io`) → HTTPS URL
- Token-protected: open `https://….pinggy…/?token=…` (token sent via DM spoiler)
- Features: browse, upload (multi-file), download, edit text files, mkdir, delete
- Stop with `/stop_file_manager <vps_id>` (PID-file kill only — never `pkill -f`)
- Requires `python3` + `openssh-client` (installed on demand via apt/apk)

---

## White-label branding

Default profile is **VexDeploy**. Change it without a redeploy:

```text
/brand-name My Hosting
/brand-website https://example.com
/brand-support support@example.com
/brand-colors cyan magenta
```

- Field changes bump `version` (e.g. `My Hosting v4`)
- New VPS stamp `brand_label`
- `/brand-update-existing` pushes current brand to all running instances
- `/brand-create` + `/brand-use` for multiple brands

### Custom MOTD template

```text
/brand-template Welcome to {brand_name}
CPU: {cpu} · Disk: {disk}
{website}
```

Dynamic vars: `{hostname} {os} {kernel} {uptime} {cpu} {ram_used} {ram_total} {ram_percent} {disk} {ip} {users} {processes}`  
Static vars: `{brand_name} {tagline} {footer} {website} {discord} {support}`

---

## MOTD installer

Idempotent by design:

1. One-time backups: `*.backup-brand`
2. Default `update-motd.d` scripts moved to `/etc/update-motd.d.disabled` once
3. Single marked `pam_motd` line (no duplicates)
4. Installer at `/etc/update-motd.d/00-brand-motd`
5. Reports `MOTD_OK` on success

Branding failure never fails a created VPS — retry with `/brand-reinstall`.

---

## Invite rules

- Credit only when invite use increase is unambiguous and inviter is known
- Duplicate joins do not double-credit
- Leave → inviter count decreases, attribution cleared
- Completion DM sent once until `/resetinvites`

---

## LXD provider

- Creates labeled instances on network `vexdeploy`
- Limits: `limits.memory`, `limits.cpu`, best-effort root `size=` disk override
- Bootstraps OpenSSH inside the instance and starts `sshd`
- Password generated per VPS, stored for DM delivery only
- Friendly image keys map to remotes: `ubuntu:22.04`, `images:debian/12`, `images:alpine/3.20/cloud`

```text
user.vexdeploy.managed=1
user.vexdeploy.owner=<user_id>
user.vexdeploy.vps_id=<id>
```

---

## Security

- Secrets only in `.env` (gitignored)
- No credentials in logs or public channels
- Admin gate: `ADMIN_IDS` ∪ `ADMIN_ROLE_ID` ∪ Discord Administrator
- Blacklist + cooldown + per-user/global caps before provision

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Invites not counting | Enable Server Members Intent; bot needs Manage Guild |
| Disk validation error | Use ≥5GB (`/createvps` defaults to 10GB) |
| LXD offline | Bot user needs `lxc`/`incus` access (set `LXD_CLI` if not on PATH) |
| MOTD missing | `/refresh-motd` or `/brand-reinstall` |
| Cannot DM credentials | User must allow DMs from server members |

---

## License

All rights reserved unless a license file is added later.
