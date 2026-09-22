import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DATABASE_PATH = os.getenv('DATABASE_PATH', 'lexonodes.db')

DEFAULT_BRAND = {
    "brand_name": "Atyro Cloud",
    "brand_tagline": "Premium Hosting Experience",
    "website": "https://atyro.cloud",
    "discord": "https://discord.gg/RTcr3gmQFr",
    "support_email": "support@atyro.cloud",
    "motd_enabled": 1,
    "motd_type": "premium",
    "primary_color": "magenta",
    "secondary_color": "cyan",
    "logo": "AUTO",
    "footer": "Premium Hosting Experience",
    "motd_template": "",
}

DEFAULT_SETTINGS = {
    'required_invites': '5',
    'vps_enabled': '1',
    'vps_cooldown_hours': '24',
    'max_total_vps': '20',
    'default_vps_memory': '2',
    'default_vps_cpu': '1',
    'default_vps_disk': '20',
    'log_channel_id': '0',
    'completion_channel_id': '0',
}

ANSI_COLORS = {
    'black': '30', 'red': '31', 'green': '32', 'yellow': '33',
    'blue': '34', 'magenta': '35', 'cyan': '36', 'white': '37',
    'bright_black': '90', 'bright_red': '91', 'bright_green': '92',
    'bright_yellow': '93', 'bright_blue': '94', 'bright_magenta': '95',
    'bright_cyan': '96', 'bright_white': '97',
}

BRAND_FIELDS = {
    'brand_name', 'brand_tagline', 'website', 'discord', 'support_email',
    'motd_type', 'primary_color', 'secondary_color', 'logo', 'footer',
    'motd_template',
}
