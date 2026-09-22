# VexDeploy

Production-ready Discord VPS deployment bot with invite-gated access, multi-brand hosting identity, and automatic branded SSH MOTD.

Members earn invites, unlock VPS access, and deploy container-backed VPS instances that receive your brand automatically after creation.

---

## Features

- **Invite-gated VPS access** — users must reach a configurable invite goal before `/createvps`
- **Invite tracking** — detects which invite was used on join, credits the inviter, invalidates leaves, blocks duplicates
- **One-time completion alerts** — DM the user and optionally post to a configured channel the first time they qualify
- **VPS deployment** — Docker-based provisioning with resource limits, cooldowns, blacklists, and global caps
- **Post-deployment branding** — every new VPS receives brand files and a login MOTD automatically
- **Multi-brand profiles** — create, switch, version, and update branding without hardcoding a company name
- **Idempotent MOTD installer** — backups PAM/MOTD files once, never duplicates scripts or PAM entries
- **Secure credential delivery** — passwords go to DM only, never public channels or logs
- **SQLite storage** — lightweight, no external database required
- **Slash-command UI** — full Discord application command interface

---

## Architecture

```text
Discord Command
      |
Invite Requirement Check
      |
User Eligibility Check
      |
VPS Deployment Function
      |
VPS Created
      |
Post-Deployment Setup
      |
Branding / MOTD Installation
      |
Send VPS Information
```

### Project layout

```text
Vps-Deploy/
|
|-- bot.py                 # Discord bot, deployment core, commands
|-- config.py              # Defaults: settings, branding, ANSI colors
|-- invites.py             # Invite tracker + completion notifications
|-- branding.py            # Multi-profile branding manager
|-- motd.py                # MOTD generator + installer payloads
|-- templates/
|   `-- motd.sh            # Base MOTD shell template
|-- requirements.txt
|-- .env.example
`-- .gitignore
```

Runtime files created on first start:

```text
lexonodes.db               # SQLite database
lexonodes_bot.log          # Structured logs
```

---

## Requirements

- Python 3.10+
- Docker (engine reachable by the bot process)
- Discord bot token
- Linux host recommended for VPS container workloads

Python packages are listed in `requirements.txt`:

```text
discord.py
python-dotenv
docker
paramiko
psutil
aiohttp
Flask
Flask-SocketIO
```

---

## Installation

### 1. Clone

```bash
git clone https://github.com/Arion-Team/VexDeploy.git
cd VexDeploy
```

### 2. Create environment file

```bash
cp .env.example .env
```

Edit `.env`:

```env
DISCORD_TOKEN=your_bot_token_here
ADMIN_IDS=1210291131301101618
ADMIN_ROLE_ID=1376177459870961694
DATABASE_PATH=lexonodes.db
MAX_VPS_PER_USER=3
MAX_CONTAINERS=100
DEFAULT_OS_IMAGE=ubuntu:22.04
DOCKER_NETWORK=bridge
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Discord developer portal

Enable these privileged intents:

- **Server Members Intent** (required for invite tracking)
- **Message Content Intent**

Invite the bot with scopes `bot applications.commands` and permission to:

- Manage Guild (for reading invites)
- Send Messages / Embed Links
- Use Application Commands

### 5. Run

```bash
python bot.py
```

On startup the bot logs in, syncs slash commands, primes invite caches, and reconnects existing VPS containers.

---

## User flow

```text
User joins Discord
        |
Invite tracker attributes the join
        |
Inviter invite count increases
        |
Required count reached
        |
Bot DMs completion message (once)
        |
User runs /createvps
        |
Eligibility verified
        |
Existing VPS deployment runs
        |
VPS created
        |
Branding + MOTD installed
        |
Credentials sent via DM
        |
User SSHs in and sees branded MOTD
```

---

## Commands

### User commands

| Command | Description |
|---------|-------------|
| `/createvps` | Deploy a VPS if invites, limits, cooldown, and resources allow |
| `/invites` | Show valid invites, requirement, and VPS access status |
| `/leaderboard` | Top inviters |
| `/vps` | Your VPS status, IP, username, brand (no secrets) |
| `/refresh-motd` | Re-run branding/MOTD on your running VPS |
| `/list` | List your VPS instances |
| `/manage_vps <id>` | Start/stop/restart/manage panel |
| `/connect_vps <token>` | Receive connection details by DM |
| `/vps_stats <id>` | Resource usage |
| `/change_ssh_password <id>` | Rotate SSH password |
| `/vps_shell <id>` / `/vps_console <id>` | Shell/console access helpers |
| `/vps_usage` | Your allocated resources summary |
| `/transfer_vps <id> <user>` | Transfer ownership |
| `/help` | Command list |

### Admin commands — invites and access

| Command | Description |
|---------|-------------|
| `/setinvites <amount>` | Global required invite count |
| `/addinvites <user> <amount>` | Manually grant invites |
| `/removeinvites <user> <amount>` | Remove invites |
| `/resetinvites <user>` | Reset progress and completion flag |
| `/blacklist <user>` | Block VPS creation |
| `/unblacklist <user>` | Lift block |
| `/vps-enable` / `/vps-disable` | Toggle deployment globally |
| `/setlogchannel <channel>` | Deployment and join log channel |
| `/setcompletionchannel <channel>` | Invite milestone channel |

### Admin commands — VPS lifecycle

| Command | Description |
|---------|-------------|
| `/create_vps` | Admin deploy with explicit resources and owner |
| `/vps_list` | All instances |
| `/delete_vps <id>` | Delete instance |
| `/suspend_vps` / `/unsuspend_vps` | Suspend control |
| `/edit_vps <id> <mem> <cpu> <disk>` | Change specs |
| `/emergency_stop` / `/emergency_remove` | Force stop/remove |
| `/admin_stats` / `/global_stats` / `/system_info` | Monitoring |
| `/cleanup_vps` | Remove inactive instances |
| `/backup_data` / `/restore_data` | Backup and restore |
| `/ban_user` / `/unban_user` / `/list_banned` | Original ban tools |
| `/add_admin` / `/remove_admin` / `/list_admins` | Admin management |

### Admin commands — branding

| Command | Description |
|---------|-------------|
| `/brand` | Show active branding |
| `/brand-name <name>` | Set brand name |
| `/brand-tagline <text>` | Set tagline |
| `/brand-website <url>` | Set website |
| `/brand-discord <url>` | Set Discord invite |
| `/brand-support <email>` | Set support email |
| `/brand-motd on\|off` | Toggle MOTD for new deploys |
| `/brand-colors <primary> <secondary>` | MOTD colors |
| `/brand-reset` | Restore defaults |
| `/brand-create <name>` | New branding profile |
| `/brand-use <name>` | Activate a profile |
| `/brand-list` | List profiles |
| `/brand-template <text>` | Custom MOTD template (`clear` to reset) |
| `/brand-reinstall <vps_id>` | Reinstall branding on one VPS |
| `/brand-update-existing` | Push branding to all running VPS instances |

---

## Configuration

### Environment variables

| Variable | Purpose |
|----------|---------|
| `DISCORD_TOKEN` | Bot token (secret) |
| `ADMIN_IDS` | Comma-separated owner/admin user IDs |
| `ADMIN_ROLE_ID` | Discord role treated as admin |
| `DATABASE_PATH` | SQLite file path |
| `MAX_VPS_PER_USER` | Default per-user VPS cap |
| `MAX_CONTAINERS` | Default container cap |
| `DEFAULT_OS_IMAGE` | Base image for deploys |
| `DOCKER_NETWORK` | Docker network name |

Never commit `.env`. It is ignored by `.gitignore`.

### Runtime settings (stored in SQLite)

These are changed with commands, not by editing code:

| Key | Default | Control |
|-----|---------|---------|
| `required_invites` | 5 | `/setinvites` |
| `vps_enabled` | 1 | `/vps-enable`, `/vps-disable` |
| `vps_cooldown_hours` | 24 | DB settings |
| `max_total_vps` | 20 | DB settings |
| `max_vps_per_user` | from env | DB settings |
| `default_vps_memory` | 2 | DB settings |
| `default_vps_cpu` | 1 | DB settings |
| `default_vps_disk` | 20 | DB settings |
| `log_channel_id` | 0 | `/setlogchannel` |
| `completion_channel_id` | 0 | `/setcompletionchannel` |

Default plan resources used by `/createvps` can be changed in the `system_settings` table:

```sql
UPDATE system_settings SET value = '4' WHERE key = 'default_vps_memory';
UPDATE system_settings SET value = '2' WHERE key = 'default_vps_cpu';
UPDATE system_settings SET value = '40' WHERE key = 'default_vps_disk';
UPDATE system_settings SET value = '48' WHERE key = 'vps_cooldown_hours';
```

---

## Branding

Branding is stored in SQLite, not hardcoded. The default profile can be replaced entirely.

Example profile values:

```json
{
  "brand_name": "Atyro Cloud",
  "brand_tagline": "Premium Hosting Experience",
  "website": "https://atyro.cloud",
  "discord": "https://discord.gg/RTcr3gmQFr",
  "support_email": "support@atyro.cloud",
  "motd_enabled": true,
  "motd_type": "premium",
  "primary_color": "magenta",
  "secondary_color": "cyan",
  "logo": "AUTO",
  "footer": "Premium Hosting Experience"
}
```

Switch identity without a redeploy:

```text
/brand-name Subhan Cloud
/brand-website https://example.com
/brand-support support@example.com
```

### Branding versions

- Field changes bump `version`
- New deploys stamp `branding_version` (example: `Atyro Cloud v4`)
- Existing VPS instances keep their stamp until you run `/brand-reinstall` or `/brand-update-existing`

### Custom MOTD template

```text
/brand-template Welcome to {brand_name}

{tagline}

Hostname: {hostname}
CPU: {cpu}
RAM: {ram_percent}
Disk: {disk}

Website: {website}
Discord: {discord}
Support: {support}
```

Supported variables include:

```text
{brand_name} {tagline} {footer}
{hostname} {os} {kernel} {uptime}
{cpu} {ram_used} {ram_total} {ram_percent}
{disk} {ip} {users} {processes}
{website} {discord} {support}
```

Dynamic values are evaluated on the VPS at login, so CPU/RAM/disk/uptime are always live.

---

## MOTD system

After a successful deploy the bot:

1. Connects through the provider adapter (`docker exec`, paramiko SSH fallback)
2. Backs up `/etc/pam.d/sshd`, `/etc/pam.d/login`, `/etc/motd` once as `*.backup-brand`
3. Disables default `update-motd.d` scripts into `/etc/update-motd.d.disabled` once
4. Ensures `pam_motd` exists exactly once (no duplicate lines)
5. Writes `/etc/update-motd.d/00-brand-motd` (overwrite = idempotent)
6. Prefills `/run/motd.dynamic`

Re-running the installer never creates duplicate PAM entries, scripts, or MOTD output.

Default premium layout shows brand header, hostname, OS, kernel, uptime, CPU, memory, disk, processes, users, IP, and support links.

ASCII logo is generated from the brand name, with a clean framed fallback when needed.

---

## Invite tracking rules

- Joins are recorded once per member (`invite_events.member_id` primary key)
- Inviter is credited only when invite use increase is unambiguous and inviter is known
- Unknown vanity/ambiguous invites are logged and **not** awarded
- If a tracked member leaves, the inviter's valid count decreases and a fake/invalid invite is recorded
- Completion DM is sent once until an admin resets the user with `/resetinvites`

---

## Logging

Logs go to stdout and `lexonodes_bot.log`:

```text
Bot started
Invite detected
Invite requirement completed
VPS deployment started
VPS created
VPS deployment failed
Branding install
MOTD installation started
MOTD installation completed
MOTD installation failed
Admin changed branding
```

Passwords, tokens, and private keys are never written to logs or public channels.

---

## Error handling model

Branding or MOTD failure does **not** fail a created VPS:

```text
✅ VPS Created

⚠️ Branding installation failed.

The VPS is still available.

You can retry branding later using:
/brand-reinstall
```

Deployment stages are logged individually in `deployment_logs`.

---

## Security notes

- Put secrets only in `.env`
- Do not enable repository secrets in source files
- Admin commands accept `ADMIN_IDS`, `ADMIN_ROLE_ID`, or Discord Administrator permission
- VPS passwords are delivered in ephemeral/DM embeds with spoiler formatting
- Blacklist checks run before every deploy
- Cooldown, per-user cap, container cap, and global cap run before provisioning

---

## Database

SQLite tables:

| Table | Purpose |
|-------|---------|
| `vps_instances` | Deployed VPS records + `branding_version` |
| `user_invites` | Invite totals, eligibility, completion flag |
| `invite_events` | Join attribution and dedup |
| `branding` | Branding profiles and versions |
| `deployment_logs` | Stage/status log |
| `system_settings` | Runtime configuration |
| `usage_stats` | Counters |
| `banned_users` | Blacklist |
| `admin_users` | Admin grants |

Backup with `/backup_data` (writes `lexonodes_backup.pkl`).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Invites not counting | Enable Server Members Intent; ensure bot has Manage Guild; check logs for ambiguous invite warnings |
| Commands missing | Run `/help` after restart; confirm `applications.commands` scope on invite |
| Docker errors | Run bot as a user that can access the Docker socket |
| MOTD not showing | Confirm SSH login runs `pam_motd`; run `/brand-reinstall <vps_id>` |
| DMs failing | User must allow DMs from server members |
| Deployment disabled | Run `/vps-enable` |

---

## Development

```bash
# syntax check
python -m py_compile bot.py config.py invites.py branding.py motd.py

# run
python bot.py
```

Conventions:

- Keep provider-specific deploy logic inside `provision_vps_core`
- Keep branding/MOTD provider-agnostic through the exec adapter
- Do not commit `.env`, `*.db`, or `*.log`

---

## License

All rights reserved unless a license file is added later.
