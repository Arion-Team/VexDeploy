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
from branding import BrandingManager, render_brand_embed
from database import Database
from invites import InviteTracker
from motd import install_branding_files, run_installer
from provider import DockerProvider, ProviderError, generate_password, validate_resources

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
        self.provider: Optional[DockerProvider] = None
        self.invite_tracker = InviteTracker(self)
        self.branding = BrandingManager(self)

    async def setup_hook(self) -> None:
        try:
            self.provider = DockerProvider(config.DOCKER_NETWORK)
            logger.info("Docker provider initialized")
        except ProviderError as exc:
            logger.error("Docker unavailable: %s", exc)
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
            raise ProviderError("Docker provider unavailable")
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
        await edit("❌ Docker provider is unavailable. Contact an admin.")
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
    embed = discord.Embed(
        title=f"VPS Ready — {brand.get('brand_name', 'VexDeploy')}",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
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
    embed.add_field(name="Brand", value=str(vps_data.get("brand_label") or ""), inline=True)
    embed.set_footer(text=brand.get("footer") or brand.get("brand_name", "VexDeploy"))

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
    embed = discord.Embed(
        title=f"{brand.get('brand_name', 'VexDeploy')} — Commands",
        description=brand.get("brand_tagline", "Instant VPS Hosting"),
        color=discord.Color.teal(),
    )
    user_cmds = (
        "`/createvps` `/invites` `/leaderboard` `/vps` `/list` "
        "`/manage_vps` `/connect_vps` `/vps_stats` `/change_ssh_password` "
        "`/vps_shell` `/vps_console` "
        "`/vps_usage` `/transfer_vps` `/refresh-motd` `/help`"
    )
    admin_cmds = (
        "`/create_vps` `/vps_list` `/delete_vps` `/suspend_vps` `/unsuspend_vps` "
        "`/edit_vps` `/emergency_stop` `/emergency_remove` `/admin_stats` `/global_stats` "
        "`/system_info` `/cleanup_vps` `/backup_data` `/restore_data` "
        "`/setinvites` `/addinvites` `/removeinvites` `/resetinvites` "
        "`/blacklist` `/unblacklist` `/vps-enable` `/vps-disable` "
        "`/setlogchannel` `/setcompletionchannel` `/brand*` `/brand-reinstall` "
        "`/brand-update-existing` `/add_admin` `/remove_admin` `/list_admins` "
        "`/ban_user` `/unban_user` `/list_banned` `/container_limit`"
    )
    embed.add_field(name="User", value=user_cmds, inline=False)
    embed.add_field(name="Admin", value=admin_cmds, inline=False)
    embed.set_footer(text=brand.get("footer") or brand.get("brand_name", "VexDeploy"))
    await ctx.send(embed=embed)


# ── invites ──────────────────────────────────────────────────
@bot.hybrid_command(name="invites", description="Show your invite progress")
async def invites_cmd(ctx: commands.Context) -> None:
    status = bot.invite_tracker.eligibility_status(str(ctx.author.id))
    brand = bot.branding.active()
    pct = 100.0 if status["required"] <= 0 else min(100.0, status["valid"] / max(1, status["required"]) * 100)
    embed = discord.Embed(
        title="Invite progress",
        color=discord.Color.blurple() if status["eligible"] else discord.Color.orange(),
    )
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
@bot.hybrid_command(name="createvps", description="Create a VPS (requires enough invites)")
@app_commands.describe(
    memory_mb="RAM in MB",
    cpus="CPU cores",
    disk_gb="Disk in GB",
    os_image="Base OS image",
)
async def createvps_cmd(
    ctx: commands.Context,
    memory_mb: Optional[int] = None,
    cpus: Optional[int] = None,
    disk_gb: Optional[int] = None,
    os_image: Optional[str] = None,
) -> None:
    author_id = str(ctx.author.id)

    if bot.db.is_banned(author_id):
        await ctx.send("❌ You are blacklisted from creating VPS.", ephemeral=True)
        return
    if str(bot.db.get_setting("vps_enabled", "1")) not in {"1", "true", "True"}:
        await ctx.send("❌ VPS creation is disabled.", ephemeral=True)
        return

    status = bot.invite_tracker.eligibility_status(author_id)
    if not status["eligible"]:
        await ctx.send(
            f"❌ You need **{status['remaining']}** more invite(s) "
            f"(have {status['valid']}/{status['required']}).",
            ephemeral=True,
        )
        return

    max_user = int(bot.db.get_setting("max_vps_per_user", config.MAX_VPS_PER_USER))
    if bot.db.count_vps(author_id) >= max_user:
        await ctx.send(f"❌ VPS limit reached ({max_user}).", ephemeral=True)
        return

    max_total = int(bot.db.get_setting("max_total_vps", 20))
    if bot.db.count_vps() >= max_total:
        await ctx.send("❌ Global VPS capacity reached.", ephemeral=True)
        return

    ok_cd, cd_msg = cooldown_ok(author_id)
    if not ok_cd:
        await ctx.send(f"❌ {cd_msg}", ephemeral=True)
        return

    mem = memory_mb or int(bot.db.get_setting("default_vps_memory", config.DEFAULT_MEMORY_MB))
    cpu = cpus or int(bot.db.get_setting("default_vps_cpu", config.DEFAULT_CPUS))
    disk = disk_gb or int(bot.db.get_setting("default_vps_disk", config.DEFAULT_DISK_GB))
    image = (os_image or config.DEFAULT_OS_IMAGE).strip()

    # validate early so defaults never silently fail
    try:
        validate_resources(mem, cpu, disk)
    except ProviderError as exc:
        await ctx.send(f"❌ {exc}", ephemeral=True)
        return

    if not isinstance(ctx.author, discord.Member) or ctx.interaction:
        # prefer ephemeral interaction progress for slash
        status_msg = None
        if ctx.interaction:
            await ctx.send(
                f"🚀 Deploying VPS…\n`{human_mb(mem)} · {cpu} CPU · {disk}GB · {image}`",
                ephemeral=True,
            )
            status_msg = await ctx.interaction.original_response()
        else:
            status_msg = await ctx.send(f"🚀 Deploying…")
    else:
        status_msg = await ctx.send(
            f"🚀 Deploying VPS…\n`{human_mb(mem)} · {cpu} CPU · {disk}GB · {image}`"
        )

    bot.db.log_deployment(author_id, "", "start", "ok", f"{mem}MB/{cpu}C/{disk}GB")
    success, vps_data, err = await provision(
        ctx, ctx.author, mem, cpu, disk, image, status_msg=status_msg
    )
    if not success or not vps_data:
        logger.warning("/createvps failed for %s: %s", author_id, err)
        if status_msg:
            try:
                await status_msg.edit(content=f"❌ {err}", embed=None, view=None)
            except discord.HTTPException:
                pass
        return

    logger.info("VPS created for %s: %s", author_id, vps_data.get("vps_id"))
    await send_credentials_dm(ctx.author, vps_data, status_msg)
    await bot.send_log_channel(
        f"✅ VPS `{vps_data.get('vps_id')}` created for <@{author_id}> "
        f"({human_mb(mem)}/{cpu}C/{disk}GB)"
    )


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
    embed = discord.Embed(
        title=f"VPS {row['vps_id']}",
        color=discord.Color.green() if row["status"] == "running" else discord.Color.red(),
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
@app_commands.describe(vps_id="VPS identifier", token="Optional access token from panel")
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
        f"Console for `{vps_id}` — use Docker attach on the host:\n"
        f"```\ndocker exec -it {row['container_id'][:12]} bash\n```",
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

    @discord.ui.button(label="📊 Stats", style=discord.ButtonStyle.secondary, custom_id="manage_stats", row=1)
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
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="📋 Logs", style=discord.ButtonStyle.secondary, custom_id="manage_logs", row=1)
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
        await interaction.response.defer(ephemeral=True)
        lines = [
            f"**SSH — {self.vps_id}**",
            f"```\nssh {row['username']}@{row['ip_address']} -p {row['ssh_port'] or 22}\n```",
            f"Password: ||{row['password_plain']}||",
        ]
        try:
            link = await asyncio.to_thread(bot.provider.start_sshx, row["container_id"])
            lines.append(f"**sshx**\n```\n{link}\n```")
        except Exception as exc:
            lines.append(f"sshx: `{exc}`")
            # only bother with tmate when sshx failed
            try:
                cmd = await asyncio.to_thread(bot.provider.start_tmate, row["container_id"])
                lines.append(f"**tmate**\n```\n{cmd}\n```")
            except Exception as exc2:
                lines.append(f"tmate: `{exc2}`")
        body = "\n".join(lines)
        body = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", body)  # strip ANSI
        body = body.replace("\x1b", "")
        if len(body) > 3900:
            body = body[:3900] + "\n…[truncated]"
        try:
            await interaction.user.send(body)
            await interaction.followup.send("🔑 SSH details sent via DM.", ephemeral=True)
        except discord.HTTPException:
            embed = discord.Embed(
                title=f"SSH — {self.vps_id}", description=body[:3900], color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔁 Reinstall", style=discord.ButtonStyle.danger, custom_id="manage_reinstall", row=2)
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

    @discord.ui.button(label="🗑 Delete", style=discord.ButtonStyle.danger, custom_id="manage_delete", row=2)
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
    embed.add_field(name="Docker managed", value=str(managed), inline=True)
    embed.add_field(
        name="VPS enabled",
        value=str(bot.db.get_setting("vps_enabled", "1")),
        inline=True,
    )
    embed.add_field(
        name="Docker",
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
            name="Docker",
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
