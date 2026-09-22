import logging
import datetime
import discord

logger = logging.getLogger('LexoNodesBot')


class InviteTracker:
    """Tracks guild invites and attributes joins to inviters safely."""

    def __init__(self, db):
        self.db = db
        self._cache = {}

    async def prime_guild(self, guild):
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {i.code: (i.uses or 0) for i in invites}
            return True
        except Exception as e:
            logger.warning(f"Could not cache invites for guild {guild.id}: {e}")
            self._cache[guild.id] = None
            return False

    async def prime_all(self, bot):
        for guild in bot.guilds:
            await self.prime_guild(guild)

    async def handle_member_join(self, member):
        """Detect which invite was used and credit the inviter.

        Returns inviter_id (str) if an invite was counted, else None.
        Never awards an invite when attribution is ambiguous or unknown.
        """
        if self.db.is_member_invite_tracked(member.id):
            logger.info(f"Invite join already recorded for member {member.id}, skipping duplicate")
            return None

        guild = member.guild
        try:
            current = await guild.invites()
        except Exception as e:
            logger.warning(f"Could not fetch invites on join in guild {guild.id}: {e}")
            current = None

        old = self._cache.get(guild.id)
        if current is None:
            await self.prime_guild(guild)
            return None

        current_map = {i.code: i for i in current}
        old_map = old or {}

        increased = []
        for code, inv in current_map.items():
            prev_uses = old_map.get(code)
            if prev_uses is None:
                continue
            if (inv.uses or 0) > prev_uses:
                increased.append(inv)

        # Detect entirely new invite codes (possible first-time creation race):
        # only trust increases on known codes; unknown codes are not awarded.
        inviter_id = None
        used_code = None

        if len(increased) == 1:
            inv = increased[0]
            if inv.inviter is not None:
                inviter_id = str(inv.inviter.id)
                used_code = inv.code
            else:
                logger.info(f"Invite {inv.code} used but inviter unknown (vanity/unknown); not awarding")
        elif len(increased) == 0 and not old_map:
            # No usable baseline yet — record the join without awarding
            logger.info(f"No invite baseline for guild {guild.id}; join recorded without awarding")
        elif len(increased) > 1:
            # Ambiguous — do not guess
            logger.warning(f"Ambiguous invite increase in guild {guild.id}; not awarding")
        elif old is None:
            logger.info(f"Invite cache missing for guild {guild.id}; join recorded without awarding")

        self.db.record_invite_join(
            member_id=str(member.id),
            guild_id=str(guild.id),
            invite_code=used_code or '',
            inviter_id=inviter_id or '',
            counted=1 if inviter_id else 0,
        )
        self._cache[guild.id] = {c: (i.uses or 0) for c, i in current_map.items()}

        if not inviter_id:
            return None

        previous_valid = self.db.get_valid_invites(inviter_id)
        self.db.add_invites(inviter_id, 1)
        new_valid = previous_valid + 1

        inviter_name = str(getattr(inv.inviter, 'display_name', '') or getattr(inv.inviter, 'name', '')) if inviter_id else ''
        if not inviter_name:
            try:
                user = await member.guild.fetch_member(int(inviter_id))
                inviter_name = user.display_name
            except Exception:
                inviter_name = inviter_id

        self.db.update_username(inviter_id, inviter_name)
        logger.info(
            f"Invite detected: {inviter_name} ({inviter_id}) invited {member} "
            f"in guild {guild.id} via {used_code}; valid={new_valid}"
        )
        return inviter_id

    async def handle_member_leave(self, member):
        """Invalidate a tracked invite when the joined member leaves."""
        event = self.db.get_invite_event(member.id)
        if not event or not event.get('inviter_id') or not event.get('counted'):
            return None

        self.db.invalidate_invite_event(member.id)
        inviter_id = event['inviter_id']
        self.db.register_fake_invite(inviter_id)
        logger.info(
            f"Invite invalidated: member {member.id} left; inviter {inviter_id} marked fake/invalid"
        )
        return inviter_id

    async def notify_completion(self, bot, user_id, valid, required):
        """Send one-time DM + optional channel notification when goal reached."""
        already = self.db.get_completion_notified(user_id)
        if already:
            return False

        self.db.set_completion_notified(user_id, 1)
        self.db.set_eligible(user_id, 1)

        try:
            user = await bot.fetch_user(int(user_id))
        except Exception as e:
            logger.warning(f"Could not fetch user {user_id} for completion DM: {e}")
            return False

        dm_text = (
            "🎉 Invite Requirement Completed!\n\n"
            "Congratulations! You have completed the required\n"
            "invite goal.\n\n"
            f"Required Invites: {required}\n"
            f"Your Invites: {valid}\n\n"
            "You can now create your VPS.\n\n"
            "Use:\n/createvps"
        )
        try:
            await user.send(dm_text)
            logger.info(f"Invite requirement completed: DM sent to {user_id}")
        except discord.Forbidden:
            logger.warning(f"Could not DM user {user_id} (DMs disabled)")
        except Exception as e:
            logger.warning(f"Failed to DM completion to {user_id}: {e}")

        channel_id = bot.db.get_setting('completion_channel_id', 0)
        if channel_id:
            channel = bot.get_channel(int(channel_id))
            if channel:
                try:
                    embed = discord.Embed(title="🎉 VPS Requirement Completed", color=discord.Color.gold())
                    embed.add_field(name="User", value=f"<@{user_id}>", inline=True)
                    embed.add_field(name="Invites", value=f"{valid}/{required}", inline=True)
                    embed.add_field(name="Status", value="Eligible", inline=True)
                    embed.add_field(name="Note", value="The user can now create a VPS.", inline=False)
                    await channel.send(embed=embed)
                    logger.info(f"Invite completion notification sent to channel {channel_id}")
                except Exception as e:
                    logger.warning(f"Could not send completion channel message: {e}")
        return True


def eligibility_status(valid, required):
    eligible = valid >= required
    return eligible, ("✅ Eligible" if eligible else "❌ Not Eligible")
