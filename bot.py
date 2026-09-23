"""VexDeploy — white-label Discord VPS bot (clean rewrite)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

import config
from branding import (
    BrandingManager,
    brand_color,
    brand_embed,
    render_brand_embed,
    scrub_aytro,
)
from database import Database
from invites import InviteTracker
from motd import install_branding_files, run_installer
from provider import LXDProvider, ProviderError, generate_password, validate_resources

# ── logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("vexdeploy")

if not config.DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN is not set — copy .env.example to .env")
    sys.exit(1)

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.invites = True
intents.message_content = False


# ── helpers ──────────────────────────────────────────────────
def progress_bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100.0)
    return "█" * filled + "░" * (width - filled)


def human_mb(mb: int) -> str:
    if mb >= 1024:
        return f"{mb / 1024:g} GB"
    return f"{mb} MB"


class VexBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=config.COMMAND_PREFIX,
            intents=intents,
            help_command=None,
            application_id=None,
        )
        self.db = Database(config.DATABASE_PATH)
        self.provider: Optional[LXDProvider] = None
        self.invite_tracker = InviteTracker(self)
        self.branding = BrandingManager(self)

    async def setup_hook(self) -> None:
        try:
            self.provider = LXDProvider(config.LXD_NETWORK)
            logger.info("LXD provider initialized")
        except ProviderError as exc:
            logger.error("LXD unavailable: %s", exc)
            self.provider = None

        try:
            await self.invite_tracker.prime_all()
            logger.info("Invite caches primed")
        except Exception as exc:
            logger.warning("Invite priming failed: %s", exc)

        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash commands", len(synced))
        except discord.HTTPException as exc:
            logger.error("Command sync failed: %s", exc)

        self._install_shutdown_signals()

    def _install_shutdown_signals(self) -> None:
        """Stop all VPS when the process gets SIGINT/SIGTERM/SIGHUP."""
        import signal

        loop = asyncio.get_running_loop()

        def _make(signame: str):
            def _handler() -> None:
                logger.warning("received %s — stopping all VPS", signame)
                try:
                    asyncio.ensure_future(self.stop_all_vps(signame), loop=loop)
                except Exception:
                    logger.exception("signal stop failed")
                # then unwind the bot cleanly (close() is idempotent for stops)
                try:
                    asyncio.ensure_future(self.close(), loop=loop)
                except Exception:
                    logger.exception("signal close failed")

            return _handler

        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, _make(name))
            except (NotImplementedError, RuntimeError, OSError):
                # Windows / unsupported — fall back to sync handler
                try:
                    signal.signal(
                        sig,
                        lambda s, f, _h=_make(name): _h(),
                    )
                except (ValueError, OSError):
                    pass

    async def stop_all_vps(self, reason: str = "bot offline") -> int:
        """Gracefully stop every managed VPS container and mark DB stopped."""
        if self.provider is None:
            logger.warning("skip VPS shutdown — provider unavailable (%s)", reason)
            return 0
        stopped = 0
        rows = []
        try:
            rows = self.db.list_all_vps()
        except Exception:
            logger.exception("failed listing VPS for shutdown")
        for row in rows:
            cid = row["container_id"]
            vid = row["vps_id"]
            try:
                await asyncio.to_thread(self.provider.stop, cid, 5)
                self.db.update_vps_status(vid, "stopped")
                stopped += 1
                logger.info("stopped %s (%s) — %s", vid, cid[:12], reason)
            except Exception as exc:
                logger.warning("could not stop %s: %s", vid, exc)
                try:
                    self.db.update_vps_status(vid, "stopped")
                except Exception:
                    pass
        if stopped:
            try:
                await self.send_log_channel(
                    f"⏻ Bot offline ({reason}) — stopped **{stopped}** VPS instance(s)."
                )
            except Exception:
                pass
        return stopped

    async def close(self) -> None:
        # Called on clean shutdown / discord disconnect — stop VPS first.
        try:
            await self.stop_all_vps("bot closing")
        except Exception:
            logger.exception("VPS shutdown on close failed")
        try:
            self.db.close()
        except Exception:
            pass
        await super().close()

    async def on_ready(self) -> None:
        logger.info("%s has connected to Discord!", self.user)
        if not getattr(self, "_autostart_done", False):
            self._autostart_done = True
            try:
                await self.start_all_vps()
            except Exception:
                logger.exception("autostart failed")

    async def start_all_vps(self) -> int:
        """Start every managed VPS that is marked stopped (pairs with stop-on-offline)."""
        if self.provider is None:
            return 0
        flag = str(self.db.get_setting("autostart_on_ready", "1"))
        if flag not in {"1", "true", "True"}:
            logger.info("autostart disabled — skipping")
            return 0
        started = 0
        for row in self.db.list_all_vps():
            if row["status"] not in {"stopped", "suspended"}:
                continue
            try:
                await asyncio.to_thread(self.provider.start, row["container_id"])
                self.db.update_vps_status(row["vps_id"], "running")
                started += 1
                logger.info("autostarted %s", row["vps_id"])
            except Exception as exc:
                logger.warning("autostart failed for %s: %s", row["vps_id"], exc)
        if started:
            try:
                await self.send_log_channel(
                    f"▶ Bot online — started **{started}** VPS instance(s)."
                )
            except Exception:
                pass
        return started

    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await self.invite_tracker.handle_member_join(member)
        except Exception:
            logger.exception("join handler failed")

    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await self.invite_tracker.handle_member_remove(member)
        except Exception:
            logger.exception("leave handler failed")

    # access helpers
    def is_admin_user(self, user: discord.abc.User) -> bool:
        if user.id in config.ADMIN_IDS:
            return True
        row_id = str(user.id)
        if row_id in self.db.list_admins():
            return True
        return False

    def member_is_admin(self, member: discord.Member) -> bool:
        if self.is_admin_user(member):
            return True
        if member.guild_permissions.administrator:
            return True
        if config.ADMIN_ROLE_ID and any(r.id == config.ADMIN_ROLE_ID for r in member.roles):
            return True
        return False

    async def send_log_channel(self, message: str) -> None:
        channel_id = int(self.db.get_setting("log_channel_id", 0) or 0)
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass


bot = VexBot()


def admin_check():
    async def predicate(ctx: commands.Context) -> bool:
        if isinstance(ctx.author, discord.Member) and bot.member_is_admin(ctx.author):
            return True
        if bot.is_admin_user(ctx.author):
            return True
        raise commands.MissingPermissions(["administrator"])

    return commands.check(predicate)


def slash_admin_check(interaction: discord.Interaction) -> bool:
    if interaction.guild and isinstance(interaction.user, discord.Member):
        return bot.member_is_admin(interaction.user)
    return bot.is_admin_user(interaction.user)


async def ensure_slash_admin(interaction: discord.Interaction) -> None:
    if not slash_admin_check(interaction):
        if interaction.response.is_done():
            await interaction.followup.send("Admin only.", ephemeral=True)
        else:
            await interaction.response.send_message("Admin only.", ephemeral=True)
        raise app_commands.CheckFailure("admin only")


# ── post-deployment ──────────────────────────────────────────
def make_exec_fn(container_id: str):
    def exec_fn(payload: str) -> tuple[int, str]:
        if bot.provider is None:
            raise ProviderError("LXD provider unavailable")
        return bot.provider.exec_command(container_id, payload)

    return exec_fn


async def post_deployment_setup(
    container_id: str, status_msg: Optional[discord.Message], owner_id: str, vps_id: str
) -> None:
    brand = bot.branding.active()
    if not brand.get("motd_enabled", 1):
        logger.info("MOTD disabled in branding — skipping install")
        return

    def progress(text: str) -> None:
        if status_msg is None:
            return

    try:
        if status_msg:
            await status_msg.edit(
                content="🎨 Installing brand files…", view=None
            )
        exec_fn = make_exec_fn(container_id)
        ok_b, out_b = install_branding_files(exec_fn, brand)
        bot.db.log_deployment(
            owner_id, vps_id, "branding", "ok" if ok_b else "warn", out_b[:500]
        )

        if status_msg:
            await status_msg.edit(content="🧾 Installing MOTD…", view=None)
        ok_m, out_m = await asyncio.to_thread(run_installer, exec_fn, brand)
        bot.db.log_deployment(
            owner_id, vps_id, "motd", "ok" if ok_m else "warn", out_m[:500]
        )

        label = bot.branding.brand_label()
        version = bot.branding.active().get("version", 1)
        bot.db.update_vps_branding(vps_id, int(version), label)

        if not ok_m:
            logger.warning("MOTD install incomplete: %s", out_m)
    except Exception as exc:
        logger.exception("Post-deploy branding failed")
        bot.db.log_deployment(owner_id, vps_id, "branding", "failed", str(exc))


async def provision(
    ctx: commands.Context | discord.Interaction,
    owner: discord.abc.User,
    memory_mb: int,
    cpus: int,
    disk_gb: int,
    image: str,
    status_msg: Optional[discord.Message] = None,
) -> tuple[bool, dict | None, str | None]:
    """Create a VPS. Returns (success, vps_row_dict, error)."""
    author_id = str(owner.id)

    async def edit(text: str) -> None:
        if status_msg:
            try:
                await status_msg.edit(content=text, view=None)
            except discord.HTTPException:
                pass
        elif isinstance(ctx, commands.Context):
            try:
                await ctx.send(text)
            except discord.HTTPException:
                pass
        elif isinstance(ctx, discord.Interaction):
            try:
                if ctx.response.is_done():
                    await ctx.followup.send(text, ephemeral=True)
                else:
                    await ctx.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                pass

    try:
        validate_resources(memory_mb, cpus, disk_gb)
    except ProviderError as exc:
        await edit(f"❌ {exc}")
        return False, None, str(exc)

    if bot.db.is_banned(author_id):
        await edit("❌ You are blacklisted from creating VPS instances.")
        return False, None, "blacklisted"

    if bot.provider is None:
        await edit("❌ LXD provider is unavailable. Contact an admin.")
        return False, None, "provider down"

    if str(bot.db.get_setting("vps_enabled", "1")) not in {"1", "true", "True"}:
        await edit("❌ VPS creation is currently disabled.")
        return False, None, "disabled"

    bot.db.log_deployment(author_id, "", "validate", "ok", "prechecks passed")

    await edit(
        f"🚀 Deploying VPS…\n"
        f"`{human_mb(memory_mb)} RAM · {cpus} CPU · {disk_gb}GB · {image}`\n"
        f"{progress_bar(5)} 5%"
    )

    try:
        result = await asyncio.to_thread(
            bot.provider.create_vps,
            owner_id=author_id,
            memory_mb=memory_mb,
            cpus=cpus,
            disk_gb=disk_gb,
            image=image,
            max_containers=int(bot.db.get_setting("max_containers", config.MAX_CONTAINERS)),
        )
    except ProviderError as exc:
        await edit(f"❌ {exc}")
        bot.db.log_deployment(author_id, "", "create", "failed", str(exc))
        return False, None, str(exc)
    except Exception as exc:
        logger.exception("Unexpected deploy failure")
        await edit(f"❌ Deployment failed: {exc}")
        bot.db.log_deployment(author_id, "", "create", "failed", str(exc))
        return False, None, str(exc)

    for note in result.notes:
        logger.info("Provider note: %s", note)

    await edit(f"{progress_bar(55)} 55% — container up, configuring…")

    label = bot.branding.brand_label()
    vps_id = bot.db.create_vps_record(
        owner_id=author_id,
        container_id=result.container_id,
        container_name=result.container_name,
        memory_mb=result.memory_mb,
        cpus=result.cpus,
        disk_gb=result.disk_gb,
        os_image=result.image,
        ip_address=result.ip_address,
        ssh_port=result.ssh_port,
        username=result.username,
        password=result.password,
        status="running",
        brand_label=label,
        branding_version=int(bot.branding.active().get("version", 1)),
    )
    bot.db.log_deployment(author_id, vps_id, "create", "ok", result.container_id[:12])
    bot.db.delete_setting(f"cooldown_cleared_{author_id}")

    await edit(f"{progress_bar(80)} 80% — installing branding/MOTD…")
    await post_deployment_setup(result.container_id, status_msg, author_id, vps_id)
    await edit(f"{progress_bar(100)} 100% — done")

    row = bot.db.get_vps(vps_id)
    vps_data = dict(row) if row else {}
    vps_data["password"] = result.password
    vps_data["ip"] = result.ip_address
    return True, vps_data, None


async def send_credentials_dm(user: discord.abc.User, vps_data: dict, status_msg: Optional[discord.Message]) -> None:
    brand = bot.branding.active()
    embed = bot.branding.embed(
        title=f"VPS Ready — {scrub_aytro(brand.get('brand_name', 'VexDeploy'))}",
    )
    embed.color = discord.Color.green()
    embed.timestamp = datetime.now(timezone.utc)
    embed.add_field(name="VPS ID", value=str(vps_data.get("vps_id", "")), inline=True)
    embed.add_field(name="IP", value=str(vps_data.get("ip") or vps_data.get("ip_address") or "—"), inline=True)
    embed.add_field(name="SSH Port", value=str(vps_data.get("ssh_port") or 22), inline=True)
    embed.add_field(name="User", value=str(vps_data.get("username", "root")), inline=True)
    embed.add_field(
        name="Password",
        value=f"||{vps_data.get('password', '')}||",
        inline=False,
    )
    embed.add_field(
        name="Connect",
        value=f"```ssh root@{vps_data.get('ip') or vps_data.get('ip_address')} -p {vps_data.get('ssh_port') or 22}```",
        inline=False,
    )
    embed.add_field(
        name="Plan",
        value=f"{human_mb(int(vps_data.get('memory_mb', 0)))} / {vps_data.get('cpus')} CPU / {vps_data.get('disk_gb')}GB",
        inline=True,
    )
    embed.add_field(name="Brand", value=scrub_aytro(vps_data.get("brand_label") or ""), inline=True)
    embed.set_footer(
        text=scrub_aytro(brand.get("footer") or brand.get("brand_name") or "VexDeploy")
    )

    sent = False
    try:
        await user.send(embed=embed)
        sent = True
    except discord.HTTPException:
        sent = False

    if status_msg and sent:
        try:
            await status_msg.edit(
                content="✅ VPS created — credentials sent via DM.",
                embed=embed,
                view=None,
            )
        except discord.HTTPException:
            pass
    elif status_msg:
        try:
            await status_msg.edit(
                content="✅ VPS created — could not DM; open a DM and run `/vps`.",
                embed=embed,
                view=None,
            )
        except discord.HTTPException:
            pass


def cooldown_ok(owner_id: str) -> tuple[bool, str]:
    hours = int(bot.db.get_setting("vps_cooldown_hours", 24))
    if hours <= 0:
        return True, ""
    last_raw = bot.db.get_last_vps_created_at(owner_id)
    if not last_raw:
        return True, ""
    try:
        last = datetime.fromisoformat(last_raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return True, ""
    cleared_raw = str(bot.db.get_setting(f"cooldown_cleared_{owner_id}", "") or "")
    if cleared_raw:
        try:
            cleared = datetime.fromisoformat(cleared_raw)
            if cleared.tzinfo is None:
                cleared = cleared.replace(tzinfo=timezone.utc)
            if last <= cleared:
                return True, ""
        except ValueError:
            pass
    next_at = last + timedelta(hours=hours)
    now = datetime.now(timezone.utc)
    if now < next_at:
        remain = next_at - now
        h = int(remain.total_seconds() // 3600)
        m = int((remain.total_seconds() % 3600) // 60)
        return False, f"Cooldown active — {h}h {m}m remaining"
    return True, ""


# ── core commands ────────────────────────────────────────────
@bot.hybrid_command(name="help", description="Show all available commands")
async def help_cmd(ctx: commands.Context) -> None:
    brand = bot.branding.active()
    embed = bot.branding.embed(
        title=f"{scrub_aytro(brand.get('brand_name', 'VexDeploy'))} — Commands",
        description=str(brand.get("brand_tagline") or "Instant VPS Hosting"),
    )
    user_cmds = (
        "`/createvps` (plan + OS UI) `/plans` `/invites` `/leaderboard` `/vps` `/list` "
        "`/manage_vps` (dashboard) `/file_manager` `/stop_file_manager` `/connect_vps` `/vps_stats` "
        "`/change_ssh_password` `/vps_shell` `/vps_console` "
        "`/vps_usage` `/transfer_vps` `/refresh-motd` `/help`"
    )
    admin_cmds = (
        "`/create_vps` `/vps_list` `/delete_vps` `/suspend_vps` `/unsuspend_vps` "
        "`/edit_vps` `/emergency_stop` `/emergency_remove` `/admin_stats` `/global_stats` "
        "`/system_info` `/cleanup_vps` `/backup_data` `/restore_data` "
        "`/createplan` `/deleteplan` `/editplan` `/listplans` "
        "`/setinvites` `/addinvites` `/removeinvites` `/resetinvites` "
        "`/resetcooldown` "
        "`/blacklist` `/unblacklist` `/vps-enable` `/vps-disable` "
        "`/setlogchannel` `/setcompletionchannel` `/brand*` `/brand-reinstall` "
        "`/brand-update-existing` `/add_admin` `/remove_admin` `/list_admins` "
        "`/ban_user` `/unban_user` `/list_banned` `/container_limit`"
    )
    embed.add_field(name="User", value=user_cmds, inline=False)
    embed.add_field(name="Admin", value=admin_cmds, inline=False)
    website = scrub_aytro(brand.get("website") or "")
    if website and website not in {"-", "—"}:
        embed.add_field(name="Website", value=website, inline=True)
    await ctx.send(embed=embed)


# ── invites ──────────────────────────────────────────────────
@bot.hybrid_command(name="invites", description="Show your invite progress")
async def invites_cmd(ctx: commands.Context) -> None:
    status = bot.invite_tracker.eligibility_status(str(ctx.author.id))
    brand = bot.branding.active()
    pct = 100.0 if status["required"] <= 0 else min(100.0, status["valid"] / max(1, status["required"]) * 100)
    embed = bot.branding.embed(title="Invite progress")
    if not status["eligible"]:
        embed.color = discord.Color.orange()
    embed.add_field(name="Valid invites", value=str(status["valid"]), inline=True)
    embed.add_field(name="Required", value=str(status["required"]), inline=True)
    embed.add_field(
        name="Status",
        value="Eligible for VPS" if status["eligible"] else f"{status['remaining']} more needed",
        inline=False,
    )
    embed.add_field(
        name="Progress",
        value=f"`{progress_bar(pct)}` {pct:.0f}%",
        inline=False,
    )
    embed.set_footer(text=brand.get("brand_name", "VexDeploy"))
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="leaderboard", description="Show top inviters")
async def leaderboard_cmd(ctx: commands.Context) -> None:
    rows = bot.db.get_leaderboard(10)
    if not rows:
        await ctx.send("No invite data yet.", ephemeral=True)
        return
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"**{i + 1}.**"
        lines.append(f"{medal} <@{row['user_id']}> — **{row['valid_invites']}** invites")
    embed = discord.Embed(
        title="Invite leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await ctx.send(embed=embed)


# ── VPS user commands ───────────────────────────────────────
async def precheck_create(author_id: str) -> Optional[str]:
    """Return an error string if the user cannot create a VPS, else None."""
    if bot.db.is_banned(author_id):
        return "You are blacklisted from creating VPS."
    if str(bot.db.get_setting("vps_enabled", "1")) not in {"1", "true", "True"}:
        return "VPS creation is disabled."
    status = bot.invite_tracker.eligibility_status(author_id)
    if not status["eligible"]:
        return (
            f"You need **{status['remaining']}** more invite(s) "
            f"(have {status['valid']}/{status['required']})."
        )
    max_user = int(bot.db.get_setting("max_vps_per_user", config.MAX_VPS_PER_USER))
    if bot.db.count_vps(author_id) >= max_user:
        return f"VPS limit reached ({max_user})."
    max_total = int(bot.db.get_setting("max_total_vps", 20))
    if bot.db.count_vps() >= max_total:
        return "Global VPS capacity reached."
    ok_cd, cd_msg = cooldown_ok(author_id)
    if not ok_cd:
        return cd_msg
    return None


class CustomVPSModal(discord.ui.Modal, title="Custom VPS resources"):
    memory_mb = discord.ui.TextInput(
        label="RAM (MB)", default="1024", required=True, min_length=1, max_length=6
    )
    cpus = discord.ui.TextInput(
        label="CPU cores", default="1", required=True, min_length=1, max_length=2
    )
    disk_gb = discord.ui.TextInput(
        label="Disk (GB)", default="10", required=True, min_length=1, max_length=6
    )

    def __init__(self, view: "CreateVPSView") -> None:
        super().__init__()
        self._parent = view
        self.memory_mb.default = str(view.memory_mb)
        self.cpus.default = str(view.cpus)
        self.disk_gb.default = str(view.disk_gb)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self._parent.author_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return
        try:
            mem = int(str(self.memory_mb).strip())
            cpu = int(str(self.cpus).strip())
            disk = int(str(self.disk_gb).strip())
            validate_resources(mem, cpu, disk)
        except (ValueError, ProviderError) as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        self._parent.memory_mb = mem
        self._parent.cpus = cpu
        self._parent.disk_gb = disk
        self._parent.plan_name = ""
        embed = self._parent.build_embed()
        await interaction.response.edit_message(embed=embed, view=self._parent)


class CreateVPSView(discord.ui.View):
    def __init__(
        self,
        author_id: str,
        *,
        memory_mb: Optional[int] = None,
        cpus: Optional[int] = None,
        disk_gb: Optional[int] = None,
        os_image: Optional[str] = None,
        plan_name: str = "",
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.memory_mb = memory_mb or int(
            bot.db.get_setting("default_vps_memory", config.DEFAULT_MEMORY_MB)
        )
        self.cpus = cpus or int(bot.db.get_setting("default_vps_cpu", config.DEFAULT_CPUS))
        self.disk_gb = disk_gb or int(
            bot.db.get_setting("default_vps_disk", config.DEFAULT_DISK_GB)
        )
        self.os_image = (os_image or config.DEFAULT_OS_IMAGE).strip()
        if self.os_image not in config.OS_CHOICES:
            # map images:ubuntu/22.04 → ubuntu:22.04 if possible
            for k, v in config.OS_CHOICES.items():
                if self.os_image.endswith(k.split(":", 1)[-1]) and ":" in self.os_image:
                    if k.split(":", 1)[1] in self.os_image:
                        self.os_image = k
                        break
        self.plan_name = plan_name
        self.deploying = False

        plans = bot.db.list_plans(enabled_only=True)
        plan_row = 0
        if plans:
            plan_select = discord.ui.Select(
                placeholder="📦 Choose a plan…",
                min_values=1,
                max_values=1,
                row=0,
                custom_id="createvps_plan",
            )
            for p in plans[:25]:
                label = str(p["name"])[:100]
                badge = str(p["badge"] or "")
                if badge:
                    label = f"{badge} · {label}"[:100]
                desc = (
                    f"{human_mb(int(p['memory_mb']))} · {int(p['cpus'])} CPU · "
                    f"{int(p['disk_gb'])}GB"
                )
                if p["price"]:
                    desc = f"{desc} · {p['price']}"
                if p["description"]:
                    desc = f"{desc} — {p['description']}"
                plan_select.add_option(
                    label=label,
                    description=desc[:100],
                    value=str(p["name"]),
                    default=str(p["name"]) == self.plan_name,
                )
            plan_select.callback = self.on_plan
            self.add_item(plan_select)
            plan_row = 1

        os_select = discord.ui.Select(
            placeholder="🖥 Choose an OS…",
            min_values=1,
            max_values=1,
            row=plan_row,
            custom_id="createvps_os",
        )
        for key, label in list(config.OS_CHOICES.items())[:25]:
            os_select.add_option(
                label=label[:100],
                value=key,
                default=key == self.os_image,
            )
        os_select.callback = self.on_os
        self.add_item(os_select)

        # apply plan resources if a plan was preselected
        if self.plan_name:
            prow = bot.db.get_plan(self.plan_name)
            if prow:
                self.memory_mb = int(prow["memory_mb"])
                self.cpus = int(prow["cpus"])
                self.disk_gb = int(prow["disk_gb"])

    def build_embed(self) -> discord.Embed:
        brand = bot.branding.active()
        plan_line = self.plan_name or "Custom"
        embed = bot.branding.embed(
            title=f"Deploy VPS — {scrub_aytro(brand.get('brand_name', 'VexDeploy'))}",
            description=str(
                brand.get("brand_tagline")
                or "Pick a plan + OS, then hit Deploy."
            ),
        )
        embed.add_field(name="Plan", value=f"`{plan_line}`", inline=True)
        embed.add_field(
            name="Resources",
            value=f"`{human_mb(self.memory_mb)} · {self.cpus} CPU · {self.disk_gb}GB`",
            inline=True,
        )
        os_label = config.OS_CHOICES.get(self.os_image, self.os_image)
        embed.add_field(name="OS", value=f"`{os_label}`", inline=True)
        status = bot.invite_tracker.eligibility_status(self.author_id)
        embed.add_field(
            name="Invites",
            value=f"{status['valid']}/{status['required']}",
            inline=True,
        )
        embed.add_field(
            name="Your VPS",
            value=f"{bot.db.count_vps(self.author_id)}/{int(bot.db.get_setting('max_vps_per_user', config.MAX_VPS_PER_USER))}",
            inline=True,
        )
        embed.set_footer(
            text="Select plan & OS, or ⚙ Custom for free-form resources · expires in 3 min"
        )
        return embed

    def _authorized(self, user: discord.abc.User) -> bool:
        return str(user.id) == self.author_id

    async def on_plan(self, interaction: discord.Interaction) -> None:
        if not self._authorized(interaction.user):
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return
        value = interaction.data.get("values", [""])[0] if interaction.data else ""
        row = bot.db.get_plan(str(value))
        if not row:
            await interaction.response.send_message("Unknown plan.", ephemeral=True)
            return
        self.plan_name = str(row["name"])
        self.memory_mb = int(row["memory_mb"])
        self.cpus = int(row["cpus"])
        self.disk_gb = int(row["disk_gb"])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_os(self, interaction: discord.Interaction) -> None:
        if not self._authorized(interaction.user):
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return
        value = interaction.data.get("values", [""])[0] if interaction.data else ""
        if value not in config.OS_CHOICES and value not in config.OS_CHOICES.values():
            # allow label or key
            for k, v in config.OS_CHOICES.items():
                if value == v:
                    value = k
                    break
        if value not in config.OS_CHOICES:
            await interaction.response.send_message("Unknown OS.", ephemeral=True)
            return
        self.os_image = value
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(
        label="⚙ Custom resources",
        style=discord.ButtonStyle.secondary,
        row=3,
        custom_id="createvps_custom",
    )
    async def custom_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._authorized(interaction.user):
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return
        await interaction.response.send_modal(CustomVPSModal(self))

    @discord.ui.button(
        label="🚀 Deploy",
        style=discord.ButtonStyle.success,
        row=3,
        custom_id="createvps_deploy",
    )
    async def deploy_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._authorized(interaction.user):
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return
        if self.deploying:
            await interaction.response.send_message("Already deploying…", ephemeral=True)
            return
        err = await precheck_create(self.author_id)
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        try:
            validate_resources(self.memory_mb, self.cpus, self.disk_gb)
        except ProviderError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        self.deploying = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        embed = self.build_embed()
        embed.set_footer(text="Deploying…")
        await interaction.response.edit_message(embed=embed, view=self)

        status_msg = await interaction.original_response()
        author = interaction.user
        mem, cpu, disk, image = self.memory_mb, self.cpus, self.disk_gb, self.os_image
        success, vps_data, err = await provision(
            interaction, author, mem, cpu, disk, image, status_msg=status_msg
        )
        if not success or not vps_data:
            logger.warning("/createvps UI failed for %s: %s", self.author_id, err)
            self.deploying = False
            for child in self.children:
                child.disabled = False  # type: ignore[attr-defined]
            try:
                await status_msg.edit(
                    content=f"❌ {err}", embed=None, view=self
                )
            except discord.HTTPException:
                pass
            return

        logger.info("VPS created for %s: %s", self.author_id, vps_data.get("vps_id"))
        await send_credentials_dm(author, vps_data, status_msg)
        await bot.send_log_channel(
            f"✅ VPS `{vps_data.get('vps_id')}` created for <@{self.author_id}> "
            f"({human_mb(mem)}/{cpu}C/{disk}GB · {image}"
            + (f" · plan {self.plan_name}" if self.plan_name else "")
            + ")"
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]


@bot.hybrid_command(
    name="createvps",
    description="Create a VPS — pick a plan and OS in the UI",
)
@app_commands.describe(
    plan="Optional plan name",
    memory_mb="RAM in MB (custom)",
    cpus="CPU cores (custom)",
    disk_gb="Disk in GB (custom)",
    os_image="Base OS image key",
)
async def createvps_cmd(
    ctx: commands.Context,
    plan: Optional[str] = None,
    memory_mb: Optional[int] = None,
    cpus: Optional[int] = None,
    disk_gb: Optional[int] = None,
    os_image: Optional[str] = None,
) -> None:
    author_id = str(ctx.author.id)
    err = await precheck_create(author_id)
    if err:
        await ctx.send(f"❌ {err}", ephemeral=True)
        return

    plan_name = ""
    if plan:
        prow = bot.db.get_plan(plan)
        if not prow:
            await ctx.send(
                f"❌ Unknown plan `{plan}`. See `/plans`.",
                ephemeral=True,
            )
            return
        plan_name = str(prow["name"])
        memory_mb = int(prow["memory_mb"])
        cpus = int(prow["cpus"])
        disk_gb = int(prow["disk_gb"])

    view = CreateVPSView(
        author_id,
        memory_mb=memory_mb,
        cpus=cpus,
        disk_gb=disk_gb,
        os_image=os_image,
        plan_name=plan_name,
    )
    await ctx.send(embed=view.build_embed(), view=view, ephemeral=True)


@bot.hybrid_command(name="list", description="List your VPS instances")
async def list_cmd(ctx: commands.Context) -> None:
    rows = bot.db.list_user_vps(str(ctx.author.id))
    if not rows:
        await ctx.send("You have no VPS instances.", ephemeral=True)
        return
    embed = discord.Embed(title="Your VPS instances", color=discord.Color.blurple())
    for row in rows:
        embed.add_field(
            name=row["vps_id"],
            value=(
                f"`{row['status']}` · {human_mb(row['memory_mb'])}/{row['cpus']}C/{row['disk_gb']}GB\n"
                f"IP `{row['ip_address'] or '—'}` · {row['brand_label'] or ''}"
            ),
            inline=False,
        )
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="vps", description="Show your VPS information")
@app_commands.describe(vps_id="VPS identifier")
async def vps_cmd(ctx: commands.Context, vps_id: Optional[str] = None) -> None:
    rows = bot.db.list_user_vps(str(ctx.author.id))
    if vps_id:
        row = bot.db.get_vps(vps_id)
        if not row or row["owner_id"] != str(ctx.author.id):
            await ctx.send("VPS not found.", ephemeral=True)
            return
        rows = [row]
    if not rows:
        await ctx.send("No VPS found.", ephemeral=True)
        return
    row = rows[0]
    embed = bot.branding.embed(
        title=f"VPS {row['vps_id']}",
    )
    embed.color = (
        discord.Color.green() if row["status"] == "running" else discord.Color.red()
    )
    embed.add_field(name="Status", value=row["status"], inline=True)
    embed.add_field(name="IP", value=row["ip_address"] or "—", inline=True)
    embed.add_field(name="SSH", value=f"port {row['ssh_port'] or 22}", inline=True)
    embed.add_field(name="User", value=row["username"], inline=True)
    embed.add_field(
        name="Plan",
        value=f"{human_mb(row['memory_mb'])} / {row['cpus']} CPU / {row['disk_gb']}GB",
        inline=True,
    )
    embed.add_field(name="Image", value=row["os_image"], inline=True)
    embed.add_field(name="Brand", value=row["brand_label"] or "—", inline=True)
    embed.add_field(name="Created", value=row["created_at"][:19], inline=True)
    embed.set_footer(text="Credentials are only available via DM / connect_vps")
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="connect_vps", description="Get connection details by DM")
@app_commands.describe(vps_id="VPS identifier", token="Optional access token from dashboard")
async def connect_vps_cmd(ctx: commands.Context, vps_id: str, token: Optional[str] = None) -> None:
    row = bot.db.get_vps(vps_id)
    if not row or row["owner_id"] != str(ctx.author.id):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"Connect — {vps_id}",
        description=(
            f"```ssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}```"
        ),
        color=discord.Color.green(),
    )
    embed.add_field(name="Password", value=f"||{row['password_plain']}||", inline=False)
    try:
        await ctx.author.send(embed=embed)
        if ctx.interaction:
            await ctx.send("Sent via DM.", ephemeral=True)
        else:
            await ctx.send("Sent via DM.")
    except discord.HTTPException:
        await ctx.send("Could not DM you — enable DMs from server members.", ephemeral=True)


@bot.hybrid_command(name="vps_stats", description="Show resource usage for a VPS")
@app_commands.describe(vps_id="VPS identifier")
async def vps_stats_cmd(ctx: commands.Context, vps_id: str) -> None:
    row = bot.db.get_vps(vps_id)
    if not row:
        await ctx.send("VPS not found.", ephemeral=True)
        return
    if row["owner_id"] != str(ctx.author.id) and not slash_admin_check_safe(ctx):
        await ctx.send("Not your VPS.", ephemeral=True)
        return
    if bot.provider is None:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    try:
        stats = await asyncio.to_thread(bot.provider.stats, row["container_id"])
    except ProviderError as exc:
        await ctx.send(f"❌ {exc}", ephemeral=True)
        return
    embed = discord.Embed(title=f"Stats — {vps_id}", color=discord.Color.blurple())
    embed.add_field(name="Status", value=stats.get("status", "?"), inline=True)
    embed.add_field(name="CPU", value=f"{stats.get('cpu_percent', 0)}%", inline=True)
    embed.add_field(name="Memory", value=f"{stats.get('mem_used_mb', 0)} MB", inline=True)
    await ctx.send(embed=embed, ephemeral=True)


def slash_admin_check_safe(ctx: commands.Context) -> bool:
    if isinstance(ctx.author, discord.Member) and ctx.guild:
        return bot.member_is_admin(ctx.author)
    return bot.is_admin_user(ctx.author)


@bot.hybrid_command(name="change_ssh_password", description="Change SSH password for your VPS")
@app_commands.describe(vps_id="VPS identifier", new_password="New password (leave blank to generate)")
async def change_ssh_password_cmd(
    ctx: commands.Context, vps_id: str, new_password: Optional[str] = None
) -> None:
    row = bot.db.get_vps(vps_id)
    if not row or row["owner_id"] != str(ctx.author.id):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    password = (new_password or "").strip() or generate_password()
    if len(password) < 8:
        await ctx.send("Password must be at least 8 characters.", ephemeral=True)
        return
    if bot.provider is None:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    try:
        await asyncio.to_thread(bot.provider.set_password, row["container_id"], password)
    except ProviderError as exc:
        await ctx.send(f"❌ {exc}", ephemeral=True)
        return
    bot.db.update_vps_password(vps_id, password)
    try:
        await ctx.author.send(f"🔐 Password updated for `{vps_id}`:\n||{password}||")
        await ctx.send("Password updated and sent via DM.", ephemeral=True)
    except discord.HTTPException:
        await ctx.send(f"Could not DM — password: ||{password}||", ephemeral=True)


@bot.hybrid_command(name="vps_shell", description="Get shell access details")
@app_commands.describe(vps_id="VPS identifier")
async def vps_shell_cmd(ctx: commands.Context, vps_id: str) -> None:
    row = bot.db.get_vps(vps_id)
    if not row or row["owner_id"] != str(ctx.author.id):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    await ctx.send(
        f"SSH shell for `{vps_id}`:\n"
        f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```"
        f"Password via `/connect_vps`. No public IP? Open `/manage_vps` → **SSH**.",
        ephemeral=True,
    )


@bot.hybrid_command(name="vps_console", description="Get console access details")
@app_commands.describe(vps_id="VPS identifier")
async def vps_console_cmd(ctx: commands.Context, vps_id: str) -> None:
    row = bot.db.get_vps(vps_id)
    if not row or row["owner_id"] != str(ctx.author.id):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    await ctx.send(
        f"Console for `{vps_id}` — use LXC exec on the host:\n"
        f"```\nlxc exec {row['container_id']} -- bash\n```",
        ephemeral=True,
    )


def _require_user_vps(ctx: commands.Context, vps_id: str):
    row = bot.db.get_vps(vps_id)
    if not row or row["owner_id"] != str(ctx.author.id):
        return None
    if bot.provider is None:
        return None
    return row


@bot.hybrid_command(name="vps_usage", description="Show your VPS usage statistics")
async def vps_usage_cmd(ctx: commands.Context) -> None:
    rows = bot.db.list_user_vps(str(ctx.author.id))
    mem = sum(r["memory_mb"] for r in rows)
    cpu = sum(r["cpus"] for r in rows)
    disk = sum(r["disk_gb"] for r in rows)
    embed = discord.Embed(title="Your usage", color=discord.Color.blurple())
    embed.add_field(name="Instances", value=str(len(rows)), inline=True)
    embed.add_field(name="RAM", value=human_mb(mem), inline=True)
    embed.add_field(name="CPU", value=str(cpu), inline=True)
    embed.add_field(name="Disk", value=f"{disk} GB", inline=True)
    embed.add_field(
        name="Limit",
        value=str(bot.db.get_setting("max_vps_per_user", config.MAX_VPS_PER_USER)),
        inline=True,
    )
    await ctx.send(embed=embed, ephemeral=True)


def _manage_authorized(user: discord.abc.User, row) -> bool:
    if str(row["owner_id"]) == str(user.id):
        return True
    if isinstance(user, discord.Member) and user.guild and bot.member_is_admin(user):
        return True
    return bot.is_admin_user(user)


def build_manage_embed(row) -> discord.Embed:
    brand = bot.branding.active()
    status = str(row["status"])
    color = (
        discord.Color.green()
        if status == "running"
        else discord.Color.orange() if status in {"stopped", "suspended"} else discord.Color.red()
    )
    embed = discord.Embed(
        title=f"Dashboard — {row['vps_id']}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Status", value=f"`{status}`", inline=True)
    embed.add_field(name="IP", value=f"`{row['ip_address'] or '—'}`", inline=True)
    embed.add_field(name="SSH", value=f"port `{row['ssh_port'] or 22}`", inline=True)
    embed.add_field(name="User", value=f"`{row['username']}`", inline=True)
    embed.add_field(
        name="Plan",
        value=f"{human_mb(row['memory_mb'])} · {row['cpus']} CPU · {row['disk_gb']}GB",
        inline=True,
    )
    embed.add_field(name="Image", value=f"`{row['os_image']}`", inline=True)
    embed.add_field(name="Brand", value=str(row["brand_label"] or "—"), inline=True)
    embed.add_field(name="Owner", value=f"<@{row['owner_id']}>", inline=True)
    embed.add_field(name="Container", value=f"`{(row['container_id'] or '')[:12]}`", inline=True)
    embed.set_footer(
        text=brand.get("footer") or brand.get("brand_name", "VexDeploy")
    )
    return embed


class PasswordModal(discord.ui.Modal, title="Change SSH password"):
    new_password = discord.ui.TextInput(
        label="New password",
        style=discord.TextStyle.short,
        min_length=8,
        max_length=64,
        required=True,
    )

    def __init__(self, view: "ManageVPSView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        row = self.view.get_row()
        if not row or bot.provider is None:
            await interaction.response.send_message("VPS/provider unavailable.", ephemeral=True)
            return
        password = str(self.new_password.value).strip()
        if len(password) < 8:
            await interaction.response.send_message("Password must be ≥8 chars.", ephemeral=True)
            return
        try:
            await asyncio.to_thread(bot.provider.set_password, row["container_id"], password)
        except Exception as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        bot.db.update_vps_password(self.view.vps_id, password)
        await interaction.response.send_message(
            f"🔐 Password updated for `{self.view.vps_id}`.", ephemeral=True
        )


class CommandModal(discord.ui.Modal, title="Run command in VPS"):
    command = discord.ui.TextInput(
        label="Command",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1500,
        required=True,
        placeholder="e.g. apt update && apt upgrade -y",
    )

    def __init__(self, view: "ManageVPSView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        row = self.view.get_row()
        if not row or bot.provider is None:
            await interaction.response.send_message("VPS/provider unavailable.", ephemeral=True)
            return
        if row["status"] != "running":
            await interaction.response.send_message("VPS is not running.", ephemeral=True)
            return
        cmd = str(self.command.value)
        await interaction.response.defer(ephemeral=True)
        try:
            code, out = await asyncio.to_thread(
                bot.provider.exec_command, row["container_id"], cmd, 60
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        text = (out or "").strip() or "(no output)"
        if len(text) > 3800:
            text = text[:3800] + "\n…[truncated]"
        embed = discord.Embed(
            title=f"Command — {self.view.vps_id} (exit {code})",
            description=f"```\n$ {cmd}\n{text}\n```",
            color=discord.Color.green() if code == 0 else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class SSHMethodView(discord.ui.View):
    """Ask which reverse-access method to generate for a VPS."""

    def __init__(self, vps_id: str, owner_id: str, invoker_id: str) -> None:
        super().__init__(timeout=120)
        self.vps_id = vps_id
        self.owner_id = str(owner_id)
        self.invoker_id = str(invoker_id)

    def _row(self):
        return bot.db.get_vps(self.vps_id)

    def _ok(self, interaction: discord.Interaction) -> bool:
        row = self._row()
        if not row or not _manage_authorized(interaction.user, row):
            return False
        return True

    async def _deny(self, interaction: discord.Interaction) -> None:
        msg = "Not authorized for this VPS."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def _run_choice(self, interaction: discord.Interaction, method: str) -> None:
        if not self._ok(interaction):
            await self._deny(interaction)
            return
        row = self._row()
        assert row is not None
        if row["status"] != "running":
            await interaction.response.send_message("VPS is not running.", ephemeral=True)
            self.stop()
            return
        await interaction.response.defer(ephemeral=True)
        lines = [
            f"**SSH — {self.vps_id}** ({method})",
            f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```",
            f"Password: ||{row['password_plain']}||",
            "",
            f"_Generating {method}…_",
        ]
        body = "\n".join(lines)
        try:
            if method == "sshx":
                link = await asyncio.wait_for(
                    asyncio.to_thread(bot.provider.start_sshx, row["container_id"]),
                    timeout=75,
                )
                body = "\n".join(
                    [
                        f"**SSH — {self.vps_id}**",
                        f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```",
                        f"Password: ||{row['password_plain']}||",
                        "",
                        f"**sshx**\n```\n{link}\n```",
                    ]
                )
            elif method == "tmate":
                cmd = await asyncio.wait_for(
                    asyncio.to_thread(bot.provider.start_tmate, row["container_id"]),
                    timeout=45,
                )
                body = "\n".join(
                    [
                        f"**SSH — {self.vps_id}**",
                        f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```",
                        f"Password: ||{row['password_plain']}||",
                        "",
                        f"**tmate**\n```\n{cmd}\n```",
                    ]
                )
            else:  # web
                web = await asyncio.wait_for(
                    asyncio.to_thread(bot.provider.start_web_terminal, row["container_id"]),
                    timeout=80,
                )
                body = "\n".join(
                    [
                        f"**SSH — {self.vps_id}**",
                        f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```",
                        f"Password: ||{row['password_plain']}||",
                        "",
                        "**Web shell (browser)**",
                        f"URL: {web['url']}",
                        f"User: `vex` · Password: ||{web['token']}||",
                        "_Open URL → basic auth vex / token → root shell._",
                    ]
                )
        except asyncio.TimeoutError:
            body = (
                f"**SSH — {self.vps_id}**\n"
                f"`{method}` timed out.\n"
                f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```\n"
                f"Password: ||{row['password_plain']}||"
            )
        except Exception as exc:
            body = (
                f"**SSH — {self.vps_id}**\n"
                f"`{method}` failed: `{str(exc)[:500]}`\n"
                f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```\n"
                f"Password: ||{row['password_plain']}||"
            )

        body = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", body)
        body = body.replace("\x1b", "")
        if len(body) > 3900:
            body = body[:3900] + "\n…[truncated]"
        try:
            await interaction.user.send(body)
            await interaction.followup.send(
                f"🔑 {method} details sent via DM.", ephemeral=True
            )
        except discord.HTTPException:
            embed = discord.Embed(
                title=f"SSH — {self.vps_id} ({method})",
                description=body[:3900],
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        self.stop()

    @discord.ui.button(label="sshx", style=discord.ButtonStyle.primary, emoji="🔗", row=0)
    async def sshx_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_choice(interaction, "sshx")

    @discord.ui.button(label="Web terminal", style=discord.ButtonStyle.success, emoji="🌐", row=0)
    async def web_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_choice(interaction, "web")

    @discord.ui.button(label="tmate", style=discord.ButtonStyle.secondary, emoji="🖥️", row=0)
    async def tmate_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_choice(interaction, "tmate")


class ManageVPSView(discord.ui.View):
    """Interactive dashboard for a single VPS: lifecycle, stats, logs, SSH, reinstall, delete."""

    def __init__(self, vps_id: str, owner_id: str, invoker_id: str) -> None:
        super().__init__(timeout=600)
        self.vps_id = vps_id
        self.owner_id = str(owner_id)
        self.invoker_id = str(invoker_id)
        self._armed: dict[str, bool] = {}

    def get_row(self):
        return bot.db.get_vps(self.vps_id)

    async def authorized(self, interaction: discord.Interaction) -> bool:
        row = self.get_row()
        if not row:
            msg = "VPS not found."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        if not _manage_authorized(interaction.user, row):
            msg = "Not your VPS."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        if bot.provider is None:
            msg = "Provider unavailable."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    async def _lifecycle(self, interaction: discord.Interaction, action: str) -> None:
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        assert row is not None
        await interaction.response.defer(ephemeral=True)
        try:
            if action == "start":
                await asyncio.to_thread(bot.provider.start, row["container_id"])
                status = "running"
            elif action == "stop":
                await asyncio.to_thread(bot.provider.stop, row["container_id"])
                status = "stopped"
            else:
                await asyncio.to_thread(bot.provider.restart, row["container_id"])
                status = "running"
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        bot.db.update_vps_status(self.vps_id, status)
        fresh = self.get_row() or row
        embed = build_manage_embed(fresh)
        await interaction.followup.send(
            f"✅ `{self.vps_id}` → **{status}**",
            embed=embed,
            view=self,
            ephemeral=True,
        )

    async def do_delete(self) -> None:
        row = self.get_row()
        if row and bot.provider:
            try:
                await asyncio.to_thread(bot.provider.remove, row["container_id"])
            except ProviderError:
                pass
        bot.db.delete_vps(self.vps_id)

    async def do_reinstall(self):
        row = self.get_row()
        if not row:
            raise ProviderError("VPS not found")
        assert bot.provider is not None
        try:
            await asyncio.to_thread(bot.provider.remove, row["container_id"])
        except ProviderError:
            pass

        def _create():
            return bot.provider.create_vps(
                owner_id=str(row["owner_id"]),
                memory_mb=int(row["memory_mb"]),
                cpus=int(row["cpus"]),
                disk_gb=int(row["disk_gb"]),
                image=str(row["os_image"]),
                vps_id=self.vps_id,
                max_containers=int(bot.db.get_setting("max_containers", config.MAX_CONTAINERS)),
            )

        result = await asyncio.to_thread(_create)
        with bot.db._lock:
            bot.db.conn.execute(
                """
                UPDATE vps_instances
                SET container_id=?, container_name=?, ip_address=?, ssh_port=?,
                    password_plain=?, password_hash=?, status='running', last_seen=?
                WHERE vps_id=?
                """,
                (
                    result.container_id,
                    result.container_name,
                    result.ip_address,
                    result.ssh_port,
                    result.password,
                    result.password,
                    datetime.now(timezone.utc).isoformat(),
                    self.vps_id,
                ),
            )
            bot.db.conn.commit()
        bot.db.log_deployment(
            str(row["owner_id"]), self.vps_id, "reinstall", "ok", result.container_id[:12]
        )
        try:
            await post_deployment_setup(result.container_id, None, str(row["owner_id"]), self.vps_id)
        except Exception:
            pass
        try:
            user = await bot.fetch_user(int(row["owner_id"]))
            await send_credentials_dm(
                user,
                {"vps_id": self.vps_id, **dict(result), "password": result.password},
                None,
            )
        except Exception:
            pass
        return self.get_row()

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="▶ Start", style=discord.ButtonStyle.success, custom_id="manage_start", row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._lifecycle(interaction, "start")

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.secondary, custom_id="manage_stop", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._lifecycle(interaction, "stop")

    @discord.ui.button(label="↻ Restart", style=discord.ButtonStyle.primary, custom_id="manage_restart", row=0)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._lifecycle(interaction, "restart")

    @discord.ui.button(label="📊 Stats", style=discord.ButtonStyle.secondary, custom_id="manage_stats", row=0)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        assert row is not None
        await interaction.response.defer(ephemeral=True)
        try:
            stats = await asyncio.to_thread(bot.provider.stats, row["container_id"])
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        embed = discord.Embed(title=f"Stats — {self.vps_id}", color=discord.Color.blurple())
        embed.add_field(name="Status", value=str(stats.get("status", "?")), inline=True)
        embed.add_field(name="CPU", value=f"{stats.get('cpu_percent', 0)}%", inline=True)
        embed.add_field(name="Memory", value=f"{stats.get('mem_used_mb', 0)} MB", inline=True)
        row = self.get_row()
        if row:
            embed.add_field(
                name="Plan",
                value=f"{human_mb(row['memory_mb'])} · {row['cpus']}C · {row['disk_gb']}GB",
                inline=True,
            )
            embed.add_field(name="Image", value=str(row["os_image"]), inline=True)
            embed.add_field(name="IP", value=str(row["ip_address"] or "—"), inline=True)
        # live disk via df
        try:
            _, df_out = await asyncio.to_thread(
                bot.provider.exec_command,
                row["container_id"] if row else "",
                "df -h / | awk 'NR==2{print $3\"/\"$2\" (\"$5)\"}'",
                10,
            )
            if df_out and df_out.strip():
                embed.add_field(name="Disk used", value=df_out.strip(), inline=True)
        except Exception:
            pass
        embed.set_footer(text="Refresh via button again for live numbers")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🌐 Network", style=discord.ButtonStyle.secondary, custom_id="manage_network", row=1)
    async def network_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        assert row is not None
        if row["status"] != "running":
            await interaction.response.send_message("VPS is not running.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        script = (
            "echo '--- addrs ---'; ip -4 addr show 2>/dev/null | awk '/inet /{print $2}' || "
            "hostname -I; "
            "echo '--- gateway ---'; ip route 2>/dev/null | awk '/default/{print $3}'; "
            "echo '--- listening ---'; "
            "(ss -lntup 2>/dev/null || netstat -lntup 2>/dev/null || true) | head -n 25; "
            "echo '--- public ---'; "
            "timeout 5 curl -fsS ifconfig.me 2>/dev/null || true; echo"
        )
        try:
            code, out = await asyncio.to_thread(
                bot.provider.exec_command, row["container_id"], script, 20
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        text = (out or "").strip() or "(no output)"
        if len(text) > 3800:
            text = text[:3800] + "\n…[truncated]"
        embed = discord.Embed(
            title=f"Network — {self.vps_id}",
            description=f"```\n{text}\n```",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="DB IP", value=f"`{row['ip_address'] or '—'}`", inline=True)
        embed.add_field(name="SSH port", value=f"`{row['ssh_port'] or 22}`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="📁 Files", style=discord.ButtonStyle.secondary, custom_id="manage_files", row=2)
    async def files_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        if not row or row["status"] != "running":
            await interaction.response.send_message("VPS is not running.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            info = await asyncio.to_thread(bot.provider.start_file_manager, row["container_id"])
        except Exception as exc:
            await interaction.followup.send(f"❌ File manager failed: {exc}", ephemeral=True)
            return
        embed = bot.branding.embed(title=f"File Manager — {self.vps_id}")
        embed.add_field(name="URL", value=f"[Open](<{info['url']}>)", inline=False)
        embed.add_field(name="Token", value=f"||`{info['token']}`||", inline=True)
        embed.add_field(name="Port", value=f"`{info['port']}` (localhost only)", inline=True)
        embed.set_footer(text="Free localhost.run URL · token required · /stop_file_manager")
        try:
            await interaction.user.send(embed=embed)
            await interaction.followup.send("📁 File manager URL sent via DM.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔐 Password", style=discord.ButtonStyle.secondary, custom_id="manage_password", row=2)
    async def password_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        await interaction.response.send_modal(PasswordModal(self))

    @discord.ui.button(label="⚡ Command", style=discord.ButtonStyle.primary, custom_id="manage_command", row=2)
    async def command_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        if not row or row["status"] != "running":
            await interaction.response.send_message("VPS is not running.", ephemeral=True)
            return
        await interaction.response.send_modal(CommandModal(self))

    @discord.ui.button(label="📋 Logs", style=discord.ButtonStyle.secondary, custom_id="manage_logs", row=0)
    async def logs_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        assert row is not None
        await interaction.response.defer(ephemeral=True)
        try:
            text = await asyncio.to_thread(bot.provider.logs, row["container_id"], 50)
        except Exception as exc:
            await interaction.followup.send(f"❌ Logs failed: {exc}", ephemeral=True)
            return
        if not text.strip():
            text = "(no log output)"
        if len(text) > 3800:
            text = text[:3800] + "\n…[truncated]"
        embed = discord.Embed(
            title=f"Logs — {self.vps_id} (last 50)",
            description=f"```\n{text}\n```",
            color=discord.Color.dark_grey(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🎨 Rebrand", style=discord.ButtonStyle.secondary, custom_id="manage_rebrand", row=1)
    async def rebrand_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Push current brand MOTD/issue banners onto this VPS."""
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        assert row is not None
        if row["status"] != "running":
            await interaction.response.send_message("VPS is not running.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        brand = bot.branding.active()
        try:
            ok_b, out_b = await asyncio.to_thread(
                install_branding_files, make_exec_fn(row["container_id"]), brand
            )
            ok_m, out_m = await asyncio.to_thread(
                run_installer, make_exec_fn(row["container_id"]), brand
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Rebrand failed: {exc}", ephemeral=True)
            return
        if ok_m or ok_b:
            bot.db.update_vps_branding(
                self.vps_id, int(brand.get("version", 1)), bot.branding.brand_label()
            )
            await interaction.followup.send(
                f"🎨 Branding pushed to `{self.vps_id}` — reconnect SSH to see the banner.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ Rebrand failed.\n```\n{(out_m or out_b or '')[:1500]}\n```",
                ephemeral=True,
            )

    @discord.ui.button(label="🔑 SSH", style=discord.ButtonStyle.primary, custom_id="manage_ssh", row=1)
    async def ssh_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        row = self.get_row()
        assert row is not None
        if row["status"] != "running":
            await interaction.response.send_message("VPS is not running.", ephemeral=True)
            return
        view = SSHMethodView(self.vps_id, self.owner_id, str(interaction.user.id))
        await interaction.response.send_message(
            f"**SSH access for `{self.vps_id}`** — choose a method:\n"
            "• **sshx** — browser link (`sshx.io`)\n"
            "• **Web terminal** — browser shell via localhost.run\n"
            "• **tmate** — `ssh …@….tmate.io` (often blocked)\n\n"
            f"Direct: `ssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}`\n"
            f"Password: ||{row['password_plain']}||",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="🔁 Reinstall", style=discord.ButtonStyle.danger, custom_id="manage_reinstall", row=3)
    async def reinstall_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        if not self._armed.get("reinstall"):
            self._armed["reinstall"] = True
            await interaction.response.send_message(
                f"⚠️ Reinstall `{self.vps_id}`? Data wiped. Click **Reinstall** again to confirm.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            new_row = await self.do_reinstall()
        except Exception as exc:
            await interaction.followup.send(f"❌ Reinstall failed: {exc}", ephemeral=True)
            return
        embed = build_manage_embed(new_row) if new_row else None
        await interaction.followup.send(
            f"✅ `{self.vps_id}` reinstalled.", embed=embed, view=self, ephemeral=True
        )

    @discord.ui.button(label="🗑 Delete", style=discord.ButtonStyle.danger, custom_id="manage_delete", row=3)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorized(interaction):
            return
        if not self._armed.get("delete"):
            self._armed["delete"] = True
            await interaction.response.send_message(
                f"⚠️ Permanently delete `{self.vps_id}`? Click **Delete** again to confirm.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.do_delete()
        except Exception as exc:
            await interaction.followup.send(f"❌ Delete failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"🗑️ Deleted `{self.vps_id}`", ephemeral=True)
        self.stop()


@bot.hybrid_command(
    name="manage_vps",
    description="Open the interactive VPS dashboard (start/stop/SSH/logs/delete)",
)
@app_commands.describe(vps_id="VPS identifier")
async def manage_vps_cmd(ctx: commands.Context, vps_id: str) -> None:
    row = bot.db.get_vps(vps_id)
    is_admin = slash_admin_check_safe(ctx)
    if not row or (row["owner_id"] != str(ctx.author.id) and not is_admin):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    if bot.provider is None:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    embed = build_manage_embed(row)
    view = ManageVPSView(vps_id, owner_id=str(row["owner_id"]), invoker_id=str(ctx.author.id))
    await ctx.send(
        f"🎛 **Dashboard** for `{vps_id}` — use the buttons below.",
        embed=embed,
        view=view,
        ephemeral=True,
    )


@bot.hybrid_command(
    name="file_manager",
    description="Start the web file manager (upload/download/edit) on your VPS",
)
@app_commands.describe(vps_id="VPS identifier")
async def file_manager_cmd(ctx: commands.Context, vps_id: str) -> None:
    row = bot.db.get_vps(vps_id)
    is_admin = slash_admin_check_safe(ctx)
    if not row or (row["owner_id"] != str(ctx.author.id) and not is_admin):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    if bot.provider is None:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    if row["status"] != "running":
        await ctx.send("VPS is not running.", ephemeral=True)
        return
    await ctx.send("Starting file manager…", ephemeral=True)
    try:
        info = await asyncio.to_thread(bot.provider.start_file_manager, row["container_id"])
    except Exception as exc:
        await ctx.send(f"❌ File manager failed: {exc}", ephemeral=True)
        return
    embed = bot.branding.embed(title=f"File Manager — {vps_id}")
    embed.add_field(name="URL", value=f"[Open](<{info['url']}>)", inline=False)
    embed.add_field(name="Token", value=f"||`{info['token']}`||", inline=True)
    embed.add_field(name="Port", value=f"`{info['port']}` (localhost only)", inline=True)
    embed.set_footer(text="Free localhost.run URL · token required · dashboard 📁 Files")
    try:
        await ctx.author.send(embed=embed)
        await ctx.send("📁 File manager URL sent via DM.", ephemeral=True)
    except discord.HTTPException:
        await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="stop_file_manager", description="Stop the web file manager on your VPS")
@app_commands.describe(vps_id="VPS identifier")
async def stop_file_manager_cmd(ctx: commands.Context, vps_id: str) -> None:
    row = bot.db.get_vps(vps_id)
    is_admin = slash_admin_check_safe(ctx)
    if not row or (row["owner_id"] != str(ctx.author.id) and not is_admin):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    if bot.provider is None:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    try:
        await asyncio.to_thread(bot.provider.stop_file_manager, row["container_id"])
    except Exception as exc:
        await ctx.send(f"❌ Stop failed: {exc}", ephemeral=True)
        return
    await ctx.send(f"📁 File manager stopped for `{vps_id}`.", ephemeral=True)


@bot.hybrid_command(name="transfer_vps", description="Transfer a VPS to another user")
@app_commands.describe(vps_id="VPS identifier", user="New owner")
async def transfer_vps_cmd(
    ctx: commands.Context, vps_id: str, user: discord.User
) -> None:
    row = bot.db.get_vps(vps_id)
    if not row or row["owner_id"] != str(ctx.author.id):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    with bot.db._lock:
        bot.db.conn.execute(
            "UPDATE vps_instances SET owner_id = ? WHERE vps_id = ?",
            (str(user.id), vps_id),
        )
        bot.db.conn.commit()
    await ctx.send(f"✅ `{vps_id}` transferred to {user.mention}", ephemeral=True)


@bot.hybrid_command(name="refresh-motd", description="Refresh branded MOTD on your VPS")
@app_commands.describe(vps_id="VPS identifier")
async def refresh_motd_cmd(ctx: commands.Context, vps_id: str) -> None:
    row = bot.db.get_vps(vps_id)
    if not row or row["owner_id"] != str(ctx.author.id):
        await ctx.send("VPS not found.", ephemeral=True)
        return
    if bot.provider is None:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    await ctx.send("Refreshing MOTD…", ephemeral=True)
    brand = bot.branding.active()
    ok, out = await asyncio.to_thread(run_installer, make_exec_fn(row["container_id"]), brand)
    if ok:
        bot.db.update_vps_branding(
            vps_id, int(brand.get("version", 1)), bot.branding.brand_label()
        )
        await ctx.send("✅ MOTD refreshed.", ephemeral=True)
    else:
        await ctx.send(f"❌ MOTD refresh failed.\n```\n{out[:1500]}\n```", ephemeral=True)


# ── admin: invites / access ─────────────────────────────────
@bot.hybrid_command(name="setinvites", description="Set required invite count (Admin only)")
@app_commands.describe(amount="Required valid invites")
async def setinvites_cmd(ctx: commands.Context, amount: app_commands.Range[int, 0, 1000]) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.set_setting("required_invites", amount)
    logger.info("Admin set required invites to %s", amount)
    await ctx.send(f"✅ Required invites set to **{amount}**.", ephemeral=True)


@bot.hybrid_command(name="resetinvites", description="Reset a user's invite progress (Admin only)")
@app_commands.describe(user="User to reset")
async def resetinvites_cmd(ctx: commands.Context, user: discord.User) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.reset_invites(str(user.id))
    await ctx.send(f"✅ Reset invite progress for {user.mention}", ephemeral=True)


@bot.hybrid_command(
    name="resetcooldown",
    description="Reset a user's VPS creation cooldown (Admin only)",
)
@app_commands.describe(user="User to reset (defaults to you)")
async def resetcooldown_cmd(
    ctx: commands.Context, user: Optional[discord.User] = None
) -> None:
    await ensure_slash_admin_ctx(ctx)
    target = user or ctx.author
    bot.db.set_setting(
        f"cooldown_cleared_{target.id}", datetime.now(timezone.utc).isoformat()
    )
    logger.info("Admin reset cooldown for %s", target.id)
    await ctx.send(
        f"✅ Cooldown reset for {target.mention} — they can create a VPS now.",
        ephemeral=True,
    )


@bot.hybrid_command(name="plans", description="List available VPS plans")
async def plans_cmd(ctx: commands.Context) -> None:
    rows = bot.db.list_plans(enabled_only=True)
    brand = bot.branding.active()
    if not rows:
        embed = bot.branding.embed(
            title="VPS Plans",
            description="No plans yet. Admins can add one with `/createplan` or set `PLANS=` in `.env`.",
        )
        await ctx.send(embed=embed, ephemeral=True)
        return
    embed = bot.branding.embed(
        title=f"VPS Plans — {scrub_aytro(brand.get('brand_name', 'VexDeploy'))}",
        description=str(
            brand.get("brand_tagline")
            or "Choose a plan, then run `/createvps`."
        ),
    )
    for p in rows:
        name = str(p["name"])
        badge = str(p["badge"] or "")
        title = f"{badge} · {name}" if badge else name
        price = str(p["price"] or "")
        desc_lines = [
            f"**{human_mb(int(p['memory_mb']))}** RAM · **{int(p['cpus'])}** CPU · **{int(p['disk_gb'])}GB** NVMe",
        ]
        if price:
            desc_lines.append(f"Price: `{price}`")
        if p["description"]:
            desc_lines.append(str(p["description"]))
        if int(p["min_invites"] or 0) > 0:
            desc_lines.append(f"Requires **{int(p['min_invites'])}** invites")
        desc_lines.append(f"Deploy: `/createvps plan:{name}`")
        embed.add_field(
            name=title[:256],
            value="\n".join(desc_lines)[:1024],
            inline=False,
        )
    embed.set_footer(
        text="Pick a plan in `/createvps` · Custom resources via ⚙ button"
    )
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(
    name="createplan",
    description="Create a VPS plan (Admin only)",
)
@app_commands.describe(
    name="Plan name",
    memory_mb="RAM MB",
    cpus="CPU cores",
    disk_gb="Disk GB",
    badge="Short badge (e.g. Popular)",
    price="Price label (e.g. $4/mo or Free)",
    description="Long description shown in /plans",
    min_invites="Extra invites required for this plan (0 = use global)",
)
async def createplan_cmd(
    ctx: commands.Context,
    name: str,
    memory_mb: app_commands.Range[int, 512, 65536],
    cpus: app_commands.Range[int, 1, 32],
    disk_gb: app_commands.Range[int, 5, 1000],
    badge: str = "",
    price: str = "",
    description: str = "",
    min_invites: app_commands.Range[int, 0, 10000] = 0,
) -> None:
    await ensure_slash_admin_ctx(ctx)
    name = scrub_aytro(name.strip())[:32]
    if not name:
        await ctx.send("❌ Plan name required.", ephemeral=True)
        return
    if bot.db.get_plan(name):
        await ctx.send(f"❌ Plan `{name}` already exists.", ephemeral=True)
        return
    try:
        validate_resources(memory_mb, cpus, disk_gb)
    except ProviderError as exc:
        await ctx.send(f"❌ {exc}", ephemeral=True)
        return
    plan_id = bot.db.create_plan(
        name=name,
        memory_mb=memory_mb,
        cpus=cpus,
        disk_gb=disk_gb,
        badge=scrub_aytro(badge.strip())[:32],
        description=scrub_aytro(description.strip())[:200],
        price=scrub_aytro(price.strip())[:32],
        min_invites=min_invites,
        source="manual",
    )
    embed = bot.branding.embed(
        title="✅ Plan created",
        description=f"**{name}** → {human_mb(memory_mb)} / {cpus} CPU / {disk_gb}GB",
    )
    embed.add_field(name="Plan ID", value=f"`{plan_id}`", inline=True)
    if badge:
        embed.add_field(name="Badge", value=badge, inline=True)
    if price:
        embed.add_field(name="Price", value=price, inline=True)
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="deleteplan", description="Delete a VPS plan (Admin only)")
@app_commands.describe(name="Plan name")
async def deleteplan_cmd(ctx: commands.Context, name: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    if bot.db.delete_plan(name):
        await ctx.send(f"🗑️ Deleted plan `{name}`.", ephemeral=True)
    else:
        await ctx.send(f"❌ Plan `{name}` not found.", ephemeral=True)


@bot.hybrid_command(name="listplans", description="List all plans including disabled (Admin only)")
async def listplans_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    rows = bot.db.list_plans(enabled_only=False)
    if not rows:
        await ctx.send("No plans.", ephemeral=True)
        return
    embed = bot.branding.embed(title=f"All plans ({len(rows)})")
    for p in rows:
        state = "on" if int(p["enabled"] or 0) else "off"
        embed.add_field(
            name=f"{p['name']} · {state}",
            value=(
                f"`{human_mb(int(p['memory_mb']))}/{int(p['cpus'])}C/"
                f"{int(p['disk_gb'])}GB` · {p['price'] or '—'} · "
                f"badge `{p['badge'] or '—'}` · src `{p['source']}`"
            ),
            inline=False,
        )
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="editplan", description="Edit a VPS plan (Admin only)")
@app_commands.describe(
    name="Plan name",
    memory_mb="New RAM MB (omit to keep)",
    cpus="New CPU (omit to keep)",
    disk_gb="New disk GB (omit to keep)",
    badge="Badge (empty string clears)",
    price="Price label",
    description="Description",
    min_invites="Extra invites",
    enabled="Enable or disable the plan",
    sort_order="Sort position",
)
async def editplan_cmd(
    ctx: commands.Context,
    name: str,
    memory_mb: Optional[app_commands.Range[int, 512, 65536]] = None,
    cpus: Optional[app_commands.Range[int, 1, 32]] = None,
    disk_gb: Optional[app_commands.Range[int, 5, 1000]] = None,
    badge: Optional[str] = None,
    price: Optional[str] = None,
    description: Optional[str] = None,
    min_invites: Optional[app_commands.Range[int, 0, 10000]] = None,
    enabled: Optional[bool] = None,
    sort_order: Optional[app_commands.Range[int, 1, 1000]] = None,
) -> None:
    await ensure_slash_admin_ctx(ctx)
    if not bot.db.get_plan(name):
        await ctx.send(f"❌ Plan `{name}` not found.", ephemeral=True)
        return
    fields: dict = {}
    if memory_mb is not None:
        fields["memory_mb"] = memory_mb
    if cpus is not None:
        fields["cpus"] = cpus
    if disk_gb is not None:
        fields["disk_gb"] = disk_gb
    if badge is not None:
        fields["badge"] = scrub_aytro(badge.strip())[:32]
    if price is not None:
        fields["price"] = scrub_aytro(price.strip())[:32]
    if description is not None:
        fields["description"] = scrub_aytro(description.strip())[:200]
    if min_invites is not None:
        fields["min_invites"] = min_invites
    if enabled is not None:
        fields["enabled"] = 1 if enabled else 0
    if sort_order is not None:
        fields["sort_order"] = sort_order
    if not fields:
        await ctx.send("Nothing to update.", ephemeral=True)
        return
    bot.db.update_plan(name, **fields)
    await ctx.send(f"✅ Updated plan `{name}`.", ephemeral=True)


@bot.hybrid_command(name="addinvites", description="Manually add valid invites (Admin only)")
@app_commands.describe(user="User", amount="Amount to add")
async def addinvites_cmd(
    ctx: commands.Context, user: discord.User, amount: app_commands.Range[int, 1, 10000]
) -> None:
    await ensure_slash_admin_ctx(ctx)
    total = bot.db.add_invites(str(user.id), amount)
    bot.db.recompute_eligibility(str(user.id))
    await ctx.send(f"✅ {user.mention} now has **{total}** invites.", ephemeral=True)


@bot.hybrid_command(name="removeinvites", description="Remove invites (Admin only)")
@app_commands.describe(user="User", amount="Amount to remove")
async def removeinvites_cmd(
    ctx: commands.Context, user: discord.User, amount: app_commands.Range[int, 1, 10000]
) -> None:
    await ensure_slash_admin_ctx(ctx)
    total = bot.db.remove_invites(str(user.id), amount)
    bot.db.recompute_eligibility(str(user.id))
    await ctx.send(f"✅ {user.mention} now has **{total}** invites.", ephemeral=True)


@bot.hybrid_command(name="blacklist", description="Blacklist a user from VPS creation (Admin only)")
@app_commands.describe(user="User")
async def blacklist_cmd(ctx: commands.Context, user: discord.User) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.ban_user(str(user.id), str(ctx.author.id))
    await ctx.send(f"⬛ Blacklisted {user.mention}", ephemeral=True)


@bot.hybrid_command(name="unblacklist", description="Remove a user from the blacklist (Admin only)")
@app_commands.describe(user="User")
async def unblacklist_cmd(ctx: commands.Context, user: discord.User) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.unban_user(str(user.id))
    await ctx.send(f"⬜ Unblacklisted {user.mention}", ephemeral=True)


@bot.hybrid_command(name="ban_user", description="Ban a user from creating VPS (Admin only)")
async def ban_user_cmd(ctx: commands.Context, user: discord.User) -> None:
    await blacklist_cmd.callback(ctx, user)  # type: ignore


@bot.hybrid_command(name="unban_user", description="Unban a user (Admin only)")
async def unban_user_cmd(ctx: commands.Context, user: discord.User) -> None:
    await unblacklist_cmd.callback(ctx, user)  # type: ignore


@bot.hybrid_command(name="list_banned", description="List banned users (Admin only)")
async def list_banned_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    rows = bot.db.list_banned()
    if not rows:
        await ctx.send("No banned users.", ephemeral=True)
        return
    lines = [f"<@{r['user_id']}> — {r['banned_at'][:19]}" for r in rows[:50]]
    await ctx.send("\n".join(lines), ephemeral=True)


@bot.hybrid_command(name="vps-enable", description="Enable VPS creation (Admin only)")
async def vps_enable_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.set_setting("vps_enabled", "1")
    await ctx.send("✅ VPS creation enabled.", ephemeral=True)


@bot.hybrid_command(name="vps-disable", description="Disable VPS creation (Admin only)")
async def vps_disable_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.set_setting("vps_enabled", "0")
    await ctx.send("✅ VPS creation disabled.", ephemeral=True)


@bot.hybrid_command(name="setlogchannel", description="Set deployment/log channel (Admin only)")
@app_commands.describe(channel="Text channel")
async def setlogchannel_cmd(
    ctx: commands.Context, channel: Optional[discord.TextChannel] = None
) -> None:
    await ensure_slash_admin_ctx(ctx)
    channel = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if not channel:
        await ctx.send("Provide a text channel.", ephemeral=True)
        return
    bot.db.set_setting("log_channel_id", channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}", ephemeral=True)


@bot.hybrid_command(
    name="setcompletionchannel",
    description="Set invite-completion notification channel (Admin only)",
)
@app_commands.describe(channel="Text channel")
async def setcompletionchannel_cmd(
    ctx: commands.Context, channel: Optional[discord.TextChannel] = None
) -> None:
    await ensure_slash_admin_ctx(ctx)
    channel = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if not channel:
        await ctx.send("Provide a text channel.", ephemeral=True)
        return
    bot.db.set_setting("completion_channel_id", channel.id)
    await ctx.send(f"✅ Completion channel set to {channel.mention}", ephemeral=True)


@bot.hybrid_command(name="add_admin", description="Add a new admin (Admin only)")
async def add_admin_cmd(ctx: commands.Context, user: discord.User) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.add_admin(str(user.id), str(ctx.author.id))
    await ctx.send(f"✅ {user.mention} is now an admin.", ephemeral=True)


@bot.hybrid_command(name="remove_admin", description="Remove an admin (Owner only)")
async def remove_admin_cmd(ctx: commands.Context, user: discord.User) -> None:
    if ctx.author.id not in config.ADMIN_IDS:
        await ctx.send("Owner only.", ephemeral=True)
        return
    bot.db.remove_admin(str(user.id))
    await ctx.send(f"✅ Removed admin {user.mention}", ephemeral=True)


@bot.hybrid_command(name="list_admins", description="List all admin users")
async def list_admins_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    ids = sorted(set(str(i) for i in config.ADMIN_IDS) | set(bot.db.list_admins()))
    if not ids:
        await ctx.send("No admins.", ephemeral=True)
        return
    await ctx.send("\n".join(f"- <@{i}>" for i in ids), ephemeral=True)


# ── admin: VPS lifecycle ────────────────────────────────────
async def ensure_slash_admin_ctx(ctx: commands.Context) -> None:
    ok = False
    if isinstance(ctx.author, discord.Member) and ctx.guild:
        ok = bot.member_is_admin(ctx.author)
    if not ok:
        ok = bot.is_admin_user(ctx.author)
    if not ok:
        raise commands.MissingPermissions(["administrator"])


@bot.hybrid_command(name="create_vps", description="Create a new VPS (Admin only)")
@app_commands.describe(
    owner="Owner of the VPS",
    memory_mb="RAM MB",
    cpus="CPU cores",
    disk_gb="Disk GB",
    os_image="OS image",
)
async def create_vps_admin_cmd(
    ctx: commands.Context,
    owner: discord.User,
    memory_mb: Optional[int] = None,
    cpus: Optional[int] = None,
    disk_gb: Optional[int] = None,
    os_image: Optional[str] = None,
) -> None:
    await ensure_slash_admin_ctx(ctx)
    mem = memory_mb or int(bot.db.get_setting("default_vps_memory", config.DEFAULT_MEMORY_MB))
    cpu = cpus or int(bot.db.get_setting("default_vps_cpu", config.DEFAULT_CPUS))
    disk = disk_gb or int(bot.db.get_setting("default_vps_disk", config.DEFAULT_DISK_GB))
    image = (os_image or config.DEFAULT_OS_IMAGE).strip()

    status_msg = await ctx.send(
        f"🚀 Admin deploy for {owner.mention}…\n`{human_mb(mem)} · {cpu}C · {disk}GB · {image}`"
    )
    success, vps_data, err = await provision(
        ctx, owner, mem, cpu, disk, image, status_msg=status_msg
    )
    if not success or not vps_data:
        await status_msg.edit(content=f"❌ {err}")
        return
    await send_credentials_dm(owner, vps_data, status_msg)


@bot.hybrid_command(name="vps_list", description="List all VPS instances (Admin only)")
async def vps_list_admin_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    rows = bot.db.list_all_vps()
    if not rows:
        await ctx.send("No VPS instances.", ephemeral=True)
        return
    embed = discord.Embed(title=f"All VPS ({len(rows)})", color=discord.Color.blurple())
    for row in rows[:25]:
        embed.add_field(
            name=f"{row['vps_id']} · {row['status']}",
            value=f"<@{row['owner_id']}> · {row['ip_address'] or '—'}",
            inline=True,
        )
    if len(rows) > 25:
        embed.set_footer(text=f"+{len(rows) - 25} more")
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="delete_vps", description="Delete a VPS instance (Admin only)")
@app_commands.describe(vps_id="VPS identifier")
async def delete_vps_cmd(ctx: commands.Context, vps_id: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    row = bot.db.get_vps(vps_id)
    if not row:
        await ctx.send("Not found.", ephemeral=True)
        return
    if bot.provider:
        try:
            await asyncio.to_thread(bot.provider.remove, row["container_id"])
        except ProviderError:
            pass
    bot.db.delete_vps(vps_id)
    await ctx.send(f"🗑️ Deleted `{vps_id}`", ephemeral=True)


@bot.hybrid_command(name="emergency_stop", description="Force stop a problematic VPS (Admin only)")
@app_commands.describe(vps_id="VPS identifier")
async def emergency_stop_cmd(ctx: commands.Context, vps_id: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    row = bot.db.get_vps(vps_id)
    if not row or not bot.provider:
        await ctx.send("Not found / provider down.", ephemeral=True)
        return
    await asyncio.to_thread(bot.provider.stop, row["container_id"], 0)
    bot.db.update_vps_status(vps_id, "stopped")
    await ctx.send(f"🛑 Stopped `{vps_id}`", ephemeral=True)


@bot.hybrid_command(name="emergency_remove", description="Force remove a problematic VPS (Admin only)")
@app_commands.describe(vps_id="VPS identifier")
async def emergency_remove_cmd(ctx: commands.Context, vps_id: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    await delete_vps_cmd.callback(ctx, vps_id)  # type: ignore


@bot.hybrid_command(name="suspend_vps", description="Suspend a VPS instance (Admin only)")
@app_commands.describe(vps_id="VPS identifier")
async def suspend_vps_cmd(ctx: commands.Context, vps_id: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    row = bot.db.get_vps(vps_id)
    if not row or not bot.provider:
        await ctx.send("Not found.", ephemeral=True)
        return
    await asyncio.to_thread(bot.provider.stop, row["container_id"])
    bot.db.update_vps_status(vps_id, "suspended")
    await ctx.send(f"⏸ Suspended `{vps_id}`", ephemeral=True)


@bot.hybrid_command(name="unsuspend_vps", description="Unsuspend a VPS instance (Admin only)")
@app_commands.describe(vps_id="VPS identifier")
async def unsuspend_vps_cmd(ctx: commands.Context, vps_id: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    row = bot.db.get_vps(vps_id)
    if not row or not bot.provider:
        await ctx.send("Not found.", ephemeral=True)
        return
    await asyncio.to_thread(bot.provider.start, row["container_id"])
    bot.db.update_vps_status(vps_id, "running")
    await ctx.send(f"▶ Resumed `{vps_id}`", ephemeral=True)


@bot.hybrid_command(name="edit_vps", description="Edit VPS specifications (Admin only)")
@app_commands.describe(vps_id="VPS identifier", memory_mb="RAM", cpus="CPU", disk_gb="Disk")
async def edit_vps_cmd(
    ctx: commands.Context,
    vps_id: str,
    memory_mb: Optional[int] = None,
    cpus: Optional[int] = None,
    disk_gb: Optional[int] = None,
) -> None:
    await ensure_slash_admin_ctx(ctx)
    row = bot.db.get_vps(vps_id)
    if not row:
        await ctx.send("Not found.", ephemeral=True)
        return
    mem = memory_mb or row["memory_mb"]
    cpu = cpus or row["cpus"]
    disk = disk_gb or row["disk_gb"]
    try:
        validate_resources(mem, cpu, disk)
    except ProviderError as exc:
        await ctx.send(f"❌ {exc}", ephemeral=True)
        return
    with bot.db._lock:
        bot.db.conn.execute(
            "UPDATE vps_instances SET memory_mb=?, cpus=?, disk_gb=? WHERE vps_id=?",
            (mem, cpu, disk, vps_id),
        )
        bot.db.conn.commit()
    await ctx.send(f"✅ Updated `{vps_id}` → {human_mb(mem)}/{cpu}C/{disk}GB", ephemeral=True)


@bot.hybrid_command(name="admin_stats", description="Show system statistics (Admin only)")
async def admin_stats_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    total = bot.db.count_vps()
    running = len([r for r in bot.db.list_all_vps() if r["status"] == "running"])
    managed = bot.provider.managed_count() if bot.provider else 0
    embed = discord.Embed(title="Admin statistics", color=discord.Color.blurple())
    embed.add_field(name="DB instances", value=str(total), inline=True)
    embed.add_field(name="Running", value=str(running), inline=True)
    embed.add_field(name="LXD managed", value=str(managed), inline=True)
    embed.add_field(
        name="VPS enabled",
        value=str(bot.db.get_setting("vps_enabled", "1")),
        inline=True,
    )
    embed.add_field(
        name="LXD",
        value="online" if bot.provider and bot.provider.ping() else "offline",
        inline=True,
    )
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="global_stats", description="Show global usage statistics (Admin only)")
async def global_stats_cmd(ctx: commands.Context) -> None:
    await admin_stats_cmd.callback(ctx)  # type: ignore


@bot.hybrid_command(name="system_info", description="Show detailed system information (Admin only)")
async def system_info_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        embed = discord.Embed(title="Host system", color=discord.Color.blurple())
        embed.add_field(name="CPU", value=f"{cpu}%", inline=True)
        embed.add_field(name="RAM", value=f"{mem.percent}% ({mem.total // (1<<30)} GB)", inline=True)
        embed.add_field(name="Disk", value=f"{disk.percent}% ({disk.total // (1<<30)} GB)", inline=True)
        embed.add_field(
            name="LXD",
            value="up" if bot.provider and bot.provider.ping() else "down",
            inline=True,
        )
        await ctx.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await ctx.send(f"❌ {exc}", ephemeral=True)


@bot.hybrid_command(name="container_limit", description="Set maximum container limit (Owner only)")
async def container_limit_cmd(
    ctx: commands.Context, limit: app_commands.Range[int, 1, 100000]
) -> None:
    if ctx.author.id not in config.ADMIN_IDS:
        await ctx.send("Owner only.", ephemeral=True)
        return
    bot.db.set_setting("max_containers", limit)
    await ctx.send(f"✅ Max containers set to {limit}", ephemeral=True)


@bot.hybrid_command(name="cleanup_vps", description="Cleanup inactive VPS instances (Admin only)")
async def cleanup_vps_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    removed = 0
    if not bot.provider:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    for row in bot.db.list_all_vps():
        try:
            status = await asyncio.to_thread(bot.provider.status, row["container_id"])
        except ProviderError:
            status = "missing"
        if status == "missing":
            bot.db.delete_vps(row["vps_id"])
            removed += 1
    await ctx.send(f"✅ Cleaned {removed} missing instance(s).", ephemeral=True)


@bot.hybrid_command(name="backup_data", description="Backup all bot data (Admin only)")
async def backup_data_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    data = bot.db.export_backup()
    payload = json.dumps(data, indent=2)
    if len(payload) > 6_000_000:
        await ctx.send("Backup too large for Discord DM.", ephemeral=True)
        return
    path = config.BASE_DIR / "vexdeploy_backup.json"
    path.write_text(payload, encoding="utf-8")
    try:
        await ctx.author.send(
            file=discord.File(str(path), filename="vexdeploy_backup.json")
        )
        await ctx.send("Backup sent via DM.", ephemeral=True)
    except discord.HTTPException:
        await ctx.send("Could not DM backup — enable DMs.", ephemeral=True)


@bot.hybrid_command(name="restore_data", description="Restore from backup (Admin only)")
async def restore_data_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    await ctx.send(
        "Upload the `vexdeploy_backup.json` file as an attachment after this command "
        "in a follow-up message (or place it in the working directory and re-run "
        "with a maintenance script).",
        ephemeral=True,
    )
    if not ctx.interaction:
        return

    def check(m: discord.Message) -> bool:
        return m.author.id == ctx.author.id and bool(m.attachments)

    try:
        msg = await bot.wait_for("message", check=check, timeout=120)
    except asyncio.TimeoutError:
        await ctx.send("Timed out.", ephemeral=True)
        return
    attachment = msg.attachments[0]
    raw = await attachment.read()
    data = json.loads(raw.decode("utf-8"))
    bot.db.import_backup(data)
    await ctx.send("✅ Restore complete.", ephemeral=True)


@bot.hybrid_command(name="reinstall_bot", description="Reinstall the bot (Owner only)")
async def reinstall_bot_cmd(ctx: commands.Context) -> None:
    if ctx.author.id not in config.ADMIN_IDS:
        await ctx.send("Owner only.", ephemeral=True)
        return
    await ctx.send(
        "Reinstall is a host operation. Pull latest code and restart the process:\n"
        "```bash\ngit pull\nsystemctl restart vexdeploy\n```",
        ephemeral=True,
    )


# ── branding ────────────────────────────────────────────────
@bot.hybrid_command(name="brand", description="Show current VPS branding (Admin only)")
async def brand_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    await ctx.send(embed=render_brand_embed(bot.branding.active()), ephemeral=True)


@bot.hybrid_command(name="brand-name", description="Set brand name (Admin only)")
async def brand_name_cmd(ctx: commands.Context, name: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    v = bot.branding.set_field("brand_name", name[:64])
    await ctx.send(f"✅ Brand name → **{name}** (v{v})", ephemeral=True)


@bot.hybrid_command(name="brand-tagline", description="Set tagline (Admin only)")
async def brand_tagline_cmd(ctx: commands.Context, text: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    v = bot.branding.set_field("brand_tagline", text[:128])
    await ctx.send(f"✅ Tagline updated (v{v})", ephemeral=True)


@bot.hybrid_command(name="brand-website", description="Set website (Admin only)")
async def brand_website_cmd(ctx: commands.Context, url: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    v = bot.branding.set_field("website", url[:256])
    await ctx.send(f"✅ Website updated (v{v})", ephemeral=True)


@bot.hybrid_command(name="brand-discord", description="Set Discord invite (Admin only)")
async def brand_discord_cmd(ctx: commands.Context, url: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    v = bot.branding.set_field("discord", url[:256])
    await ctx.send(f"✅ Discord link updated (v{v})", ephemeral=True)


@bot.hybrid_command(name="brand-support", description="Set support email (Admin only)")
async def brand_support_cmd(ctx: commands.Context, email: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    v = bot.branding.set_field("support_email", email[:128])
    await ctx.send(f"✅ Support email updated (v{v})", ephemeral=True)


@bot.hybrid_command(name="brand-motd", description="Enable/disable branded MOTD (Admin only)")
@app_commands.describe(state="on or off")
@app_commands.choices(
    state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def brand_motd_cmd(ctx: commands.Context, state: app_commands.Choice[str]) -> None:
    await ensure_slash_admin_ctx(ctx)
    enabled = state.value == "on"
    v = bot.branding.set_field("motd_enabled", enabled)
    await ctx.send(f"✅ MOTD **{'on' if enabled else 'off'}** (v{v})", ephemeral=True)


@bot.hybrid_command(name="brand-colors", description="Set MOTD colors (Admin only)")
@app_commands.describe(primary="Primary ANSI color name", secondary="Secondary ANSI color name")
async def brand_colors_cmd(ctx: commands.Context, primary: str, secondary: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    from config import ANSI_COLORS

    p = primary.lower().strip()
    s = secondary.lower().strip()
    if p not in ANSI_COLORS or s not in ANSI_COLORS:
        await ctx.send(
            "Valid colors: " + ", ".join(sorted(ANSI_COLORS)), ephemeral=True
        )
        return
    bot.branding.set_field("primary_color", p)
    v = bot.branding.set_field("secondary_color", s)
    await ctx.send(f"✅ Colors updated (v{v})", ephemeral=True)


@bot.hybrid_command(name="brand-reset", description="Restore default branding (Admin only)")
async def brand_reset_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.reset_active_branding()
    await ctx.send("✅ Branding reset to VexDeploy defaults.", ephemeral=True)


@bot.hybrid_command(name="brand-create", description="Create a branding profile (Admin only)")
async def brand_create_cmd(ctx: commands.Context, name: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    bot.db.create_branding_profile(name.strip().lower())
    await ctx.send(f"✅ Created profile `{name}`", ephemeral=True)


@bot.hybrid_command(name="brand-use", description="Activate a branding profile (Admin only)")
async def brand_use_cmd(ctx: commands.Context, name: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    if not bot.db.activate_branding(name.strip().lower()):
        await ctx.send("Profile not found.", ephemeral=True)
        return
    await ctx.send(f"✅ Active profile → `{name}`", ephemeral=True)


@bot.hybrid_command(name="brand-list", description="List branding profiles (Admin only)")
async def brand_list_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    profiles = bot.db.list_branding_profiles()
    if not profiles:
        await ctx.send("No profiles.", ephemeral=True)
        return
    lines = [
        f"{'**' + p['profile_name'] + '** (active)' if p['is_active'] else p['profile_name']} "
        f"— {p['brand_name']} v{p['version']}"
        for p in profiles
    ]
    await ctx.send("\n".join(lines), ephemeral=True)


@bot.hybrid_command(name="brand-template", description="Set custom MOTD template (Admin only)")
@app_commands.describe(text="Template with {vars}, or 'clear' to reset")
async def brand_template_cmd(ctx: commands.Context, text: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    value = "" if text.strip().lower() in {"clear", "reset", "none"} else text[:4000]
    v = bot.branding.set_field("motd_template", value)
    await ctx.send(
        f"✅ Template {'cleared' if not value else 'updated'} (v{v})", ephemeral=True
    )


@bot.hybrid_command(
    name="brand-reinstall",
    description="Reinstall branding/MOTD on a VPS (Admin only)",
)
@app_commands.describe(vps_id="VPS identifier")
async def brand_reinstall_cmd(ctx: commands.Context, vps_id: str) -> None:
    await ensure_slash_admin_ctx(ctx)
    row = bot.db.get_vps(vps_id)
    if not row or not bot.provider:
        await ctx.send("Not found / provider down.", ephemeral=True)
        return
    await ctx.send("Reinstalling branding…", ephemeral=True)
    brand = bot.branding.active()
    ok1, out1 = await asyncio.to_thread(
        install_branding_files, make_exec_fn(row["container_id"]), brand
    )
    ok2, out2 = await asyncio.to_thread(
        run_installer, make_exec_fn(row["container_id"]), brand
    )
    if ok2:
        bot.db.update_vps_branding(
            vps_id, int(brand.get("version", 1)), bot.branding.brand_label()
        )
        await ctx.send(f"✅ Reinstalled on `{vps_id}`", ephemeral=True)
    else:
        await ctx.send(
            f"❌ Failed.\n```\n{(out2 or out1)[:1500]}\n```", ephemeral=True
        )


@bot.hybrid_command(
    name="brand-update-existing",
    description="Push current branding to running VPS instances (Admin only)",
)
async def brand_update_existing_cmd(ctx: commands.Context) -> None:
    await ensure_slash_admin_ctx(ctx)
    if not bot.provider:
        await ctx.send("Provider unavailable.", ephemeral=True)
        return
    await ctx.send("Pushing branding to all instances…", ephemeral=True)
    brand = bot.branding.active()
    version = int(brand.get("version", 1))
    label = bot.branding.brand_label()
    ok = 0
    fail = 0
    for row in bot.db.list_all_vps():
        if row["status"] not in {"running", "suspended"}:
            continue
        try:
            exec_fn = make_exec_fn(row["container_id"])
            await asyncio.to_thread(install_branding_files, exec_fn, brand)
            success, _ = await asyncio.to_thread(run_installer, exec_fn, brand)
            if success:
                bot.db.update_vps_branding(row["vps_id"], version, label)
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
    await ctx.send(f"✅ Updated **{ok}** VPS · failed **{fail}**", ephemeral=True)


# ── entry ────────────────────────────────────────────────────
def main() -> None:
    logger.info("Starting VexDeploy…")
    # Shutdown signals are registered in setup_hook (running loop required).
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
