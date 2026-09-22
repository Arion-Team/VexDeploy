import logging
import datetime
from config import DEFAULT_BRAND, BRAND_FIELDS

logger = logging.getLogger('LexoNodesBot')


class BrandingManager:
    """Multi-profile branding store on top of the bot SQLite database."""

    def __init__(self, db):
        self.db = db

    def get_active(self):
        brand = self.db.get_active_branding()
        if not brand:
            self.db.create_branding_profile('default', DEFAULT_BRAND)
            self.db.activate_branding('default')
            brand = self.db.get_active_branding()
        return brand

    def get(self, profile_name):
        return self.db.get_branding(profile_name)

    def list_profiles(self):
        return self.db.list_branding_profiles()

    def create_profile(self, profile_name):
        name = profile_name.strip()
        if not name:
            raise ValueError("Profile name cannot be empty")
        if self.db.get_branding(name):
            raise ValueError(f"Branding profile '{name}' already exists")
        defaults = dict(DEFAULT_BRAND)
        defaults['brand_name'] = name
        self.db.create_branding_profile(name, defaults)
        logger.info(f"Admin created branding profile: {name}")
        return True

    def use_profile(self, profile_name):
        if not self.db.get_branding(profile_name):
            raise ValueError(f"Branding profile '{profile_name}' not found")
        self.db.activate_branding(profile_name)
        logger.info(f"Admin switched active branding to: {profile_name}")

    def update_field(self, profile_name, field, value):
        if field not in BRAND_FIELDS and field != 'motd_enabled':
            raise ValueError(f"Invalid branding field: {field}")
        if not self.db.get_branding(profile_name):
            raise ValueError(f"Branding profile '{profile_name}' not found")
        self.db.update_branding_field(profile_name, field, value, bump_version=True)
        logger.info(f"Admin changed branding ({profile_name}): {field} -> {value!r}")

    def set_motd_enabled(self, profile_name, enabled):
        self.update_field(profile_name, 'motd_enabled', 1 if enabled else 0)

    def reset(self, profile_name='default'):
        self.db.reset_branding(profile_name, DEFAULT_BRAND)
        logger.info(f"Admin reset branding profile: {profile_name}")

    def active_version_label(self):
        brand = self.get_active()
        if not brand:
            return "None"
        return f"{brand.get('brand_name', 'Unknown')} v{brand.get('version', 1)}"


def render_brand_embed(brand):
    import discord
    embed = discord.Embed(title="🎨 Current VPS Branding", color=discord.Color.magenta())
    embed.add_field(name="Name", value=brand.get('brand_name') or '-', inline=False)
    embed.add_field(name="Tagline", value=brand.get('brand_tagline') or '-', inline=False)
    embed.add_field(name="Website", value=brand.get('website') or '-', inline=False)
    embed.add_field(name="Discord", value=brand.get('discord') or '-', inline=False)
    embed.add_field(name="Support", value=brand.get('support_email') or '-', inline=False)
    motd_on = bool(int(brand.get('motd_enabled') or 0))
    embed.add_field(name="MOTD", value="Enabled" if motd_on else "Disabled", inline=True)
    embed.add_field(name="Profile", value=brand.get('profile_name') or 'default', inline=True)
    embed.add_field(name="Version", value=str(brand.get('version') or 1), inline=True)
    embed.add_field(name="Colors", value=f"{brand.get('primary_color') or '-'} / {brand.get('secondary_color') or '-'}", inline=True)
    footer = brand.get('footer') or brand.get('brand_tagline')
    if footer:
        embed.set_footer(text=footer)
    return embed
