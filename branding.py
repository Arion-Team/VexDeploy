"""Branding manager for VexDeploy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from bot import VexBot


class BrandingManager:
    def __init__(self, bot: "VexBot") -> None:
        self.bot = bot

    def active(self) -> dict[str, Any]:
        brand = self.bot.db.get_active_branding()
        if not brand:
            from config import DEFAULT_BRAND

            brand = {"profile_name": "default", "version": 1, **DEFAULT_BRAND}
        return brand

    def set_field(self, field: str, value: Any) -> int:
        brand = self.active()
        return self.bot.db.update_branding_field(
            str(brand["profile_name"]), field, value
        )

    def brand_label(self) -> str:
        return self.bot.db.active_brand_label()


def render_brand_embed(
    brand: dict[str, Any],
    *,
    title: str | None = None,
    version: int | None = None,
) -> discord.Embed:
    ver = version if version is not None else int(brand.get("version", 1))
    embed = discord.Embed(
        title=title or f"{brand.get('brand_name', 'VexDeploy')} — branding",
        color=discord.Color.teal(),
    )
    embed.add_field(
        name="Profile", value=str(brand.get("profile_name", "default")), inline=True
    )
    embed.add_field(name="Name", value=str(brand.get("brand_name", "")), inline=True)
    embed.add_field(name="Version", value=str(ver), inline=True)
    embed.add_field(
        name="Tagline", value=str(brand.get("brand_tagline") or "—"), inline=False
    )
    embed.add_field(
        name="Website", value=str(brand.get("website") or "—"), inline=True
    )
    embed.add_field(
        name="Discord", value=str(brand.get("discord") or "—"), inline=True
    )
    embed.add_field(
        name="Support", value=str(brand.get("support_email") or "—"), inline=True
    )
    embed.add_field(
        name="MOTD",
        value="on" if brand.get("motd_enabled") in (1, True, "1") else "off",
        inline=True,
    )
    embed.add_field(
        name="Colors",
        value=(
            f"primary=`{brand.get('primary_color')}` "
            f"secondary=`{brand.get('secondary_color')}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Footer", value=str(brand.get("footer") or "—"), inline=True
    )
    embed.set_footer(
        text="Changes bump version; new VPS get the latest brand automatically."
    )
    return embed
