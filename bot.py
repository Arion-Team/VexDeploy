import discord
from discord.ext import commands
from discord import ui, app_commands
import os
import random
import string
import json
import subprocess
from dotenv import load_dotenv
import asyncio
import datetime
import docker
import time
import logging
import traceback
import aiohttp
import socket
import re
import psutil
import platform
import shutil
from typing import Optional, Literal
import sqlite3
import pickle
import base64
import threading
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
import docker
import paramiko
import os
from dotenv import load_dotenv
from config import DEFAULT_SETTINGS, DEFAULT_BRAND, DATABASE_PATH
from invites import InviteTracker, eligibility_status
from branding import BrandingManager, render_brand_embed
from motd import run_installer, install_branding_files

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('lexonodes_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('LexoNodesBot')

# Load environment variables
load_dotenv()

# Bot configuration
TOKEN = os.getenv('DISCORD_TOKEN')
ADMIN_IDS = {int(id_) for id_ in os.getenv('ADMIN_IDS', '1210291131301101618').split(',') if id_.strip()}
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID', '1376177459870961694'))
WATERMARK = "LexoNodes VPS Service"
WELCOME_MESSAGE = "Welcome To LexoNodes! Get Started With Us!"
MAX_VPS_PER_USER = int(os.getenv('MAX_VPS_PER_USER', '3'))
DEFAULT_OS_IMAGE = os.getenv('DEFAULT_OS_IMAGE', 'ubuntu:22.04')
DOCKER_NETWORK = os.getenv('DOCKER_NETWORK', 'bridge')
MAX_CONTAINERS = int(os.getenv('MAX_CONTAINERS', '100'))
DB_FILE = DATABASE_PATH or 'lexonodes.db'
BACKUP_FILE = 'lexonodes_backup.pkl'

# Known miner process names/patterns
MINER_PATTERNS = [
    'xmrig', 'ethminer', 'cgminer', 'sgminer', 'bfgminer',
    'minerd', 'cpuminer', 'cryptonight', 'stratum', 'pool'
]

# Dockerfile template for custom images
DOCKERFILE_TEMPLATE = """
FROM {base_image}

# Prevent prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install systemd, sudo, SSH, Docker and other essential packages
RUN apt-get update && \\
    apt-get install -y systemd systemd-sysv dbus sudo \\
                       curl gnupg2 apt-transport-https ca-certificates \\
                       software-properties-common \\
                       docker.io openssh-server tmate && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Root password
RUN echo "root:{root_password}" | chpasswd

# Create user and set password
RUN useradd -m -s /bin/bash {username} && \\
    echo "{username}:{user_password}" | chpasswd && \\
    usermod -aG sudo {username}

# Enable SSH login
RUN mkdir /var/run/sshd && \\
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config

# Enable services on boot
RUN systemctl enable ssh && \\
    systemctl enable docker

# LexoNodes customization
RUN echo '{welcome_message}' > /etc/motd && \\
    echo 'echo "{welcome_message}"' >> /home/{username}/.bashrc && \\
    echo '{watermark}' > /etc/machine-info && \\
    echo 'lexonodes-{vps_id}' > /etc/hostname

# Install additional useful packages
RUN apt-get update && \\
    apt-get install -y neofetch htop nano vim wget git tmux net-tools dnsutils iputils-ping && \\
    apt-get clean && \\
    rm -rf /var/lib/apt/lists/*

# Fix systemd inside container
STOPSIGNAL SIGRTMIN+3

# Boot into systemd (like a VM)
CMD ["/sbin/init"]
"""

class Database:
    """Handles all data persistence using SQLite3"""
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()
        self._initialize_settings()

    def _create_tables(self):
        """Create necessary tables"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS vps_instances (
                token TEXT PRIMARY KEY,
                vps_id TEXT UNIQUE,
                container_id TEXT,
                memory INTEGER,
                cpu INTEGER,
                disk INTEGER,
                username TEXT,
                password TEXT,
                root_password TEXT,
                created_by TEXT,
                created_at TEXT,
                tmate_session TEXT,
                watermark TEXT,
                os_image TEXT,
                restart_count INTEGER DEFAULT 0,
                last_restart TEXT,
                status TEXT DEFAULT 'running',
                use_custom_image BOOLEAN DEFAULT 1
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id TEXT PRIMARY KEY
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_users (
                user_id TEXT PRIMARY KEY
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_invites (
                user_id TEXT PRIMARY KEY,
                guild_id TEXT,
                username TEXT,
                invites INTEGER DEFAULT 0,
                valid_invites INTEGER DEFAULT 0,
                fake_invites INTEGER DEFAULT 0,
                eligible INTEGER DEFAULT 0,
                completion_notified INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS invite_events (
                member_id TEXT PRIMARY KEY,
                guild_id TEXT,
                invite_code TEXT,
                inviter_id TEXT,
                counted INTEGER DEFAULT 1,
                created_at TEXT
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS branding (
                profile_name TEXT PRIMARY KEY,
                brand_name TEXT,
                brand_tagline TEXT,
                website TEXT,
                discord TEXT,
                support_email TEXT,
                motd_enabled INTEGER DEFAULT 1,
                motd_type TEXT DEFAULT 'premium',
                primary_color TEXT DEFAULT 'magenta',
                secondary_color TEXT DEFAULT 'cyan',
                logo TEXT DEFAULT 'AUTO',
                footer TEXT DEFAULT '',
                motd_template TEXT DEFAULT '',
                version INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 0,
                updated_at TEXT
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS deployment_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                vps_id TEXT,
                stage TEXT,
                status TEXT,
                message TEXT,
                created_at TEXT
            )
        ''')

        # Migration: branding version stamp on each VPS
        try:
            self.cursor.execute('ALTER TABLE vps_instances ADD COLUMN branding_version TEXT')
        except sqlite3.OperationalError:
            pass

        self.conn.commit()

    def _initialize_settings(self):
        """Initialize default settings"""
        defaults = {
            'max_containers': str(MAX_CONTAINERS),
            'max_vps_per_user': str(MAX_VPS_PER_USER),
        }
        defaults.update(DEFAULT_SETTINGS)
        for key, value in defaults.items():
            self.cursor.execute('INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)', (key, value))

        # Seed default branding profile
        self.cursor.execute('SELECT COUNT(*) FROM branding')
        if self.cursor.fetchone()[0] == 0:
            cols = ', '.join(DEFAULT_BRAND.keys())
            placeholders = ', '.join('?' for _ in DEFAULT_BRAND)
            vals = tuple(
                int(v) if isinstance(v, bool) else v
                for v in DEFAULT_BRAND.values()
            )
            self.cursor.execute(
                f"INSERT OR IGNORE INTO branding (profile_name, {cols}, version, is_active, updated_at) "
                f"VALUES (?, {placeholders}, 1, 1, ?)",
                ('default', *vals, str(datetime.datetime.now())),
            )

        # Load admin users from database
        self.cursor.execute('SELECT user_id FROM admin_users')
        for row in self.cursor.fetchall():
            ADMIN_IDS.add(int(row[0]))

        self.conn.commit()

    def get_setting_str(self, key, default=None):
        self.cursor.execute('SELECT value FROM system_settings WHERE key = ?', (key,))
        result = self.cursor.fetchone()
        return result[0] if result else default

    # ------------------------------------------------------------------
    # Invite tracking
    # ------------------------------------------------------------------
    def _ensure_invite_user(self, user_id):
        now = str(datetime.datetime.now())
        self.cursor.execute(
            'INSERT OR IGNORE INTO user_invites (user_id, valid_invites, invites, fake_invites, created_at, updated_at) '
            'VALUES (?, 0, 0, 0, ?, ?)',
            (str(user_id), now, now),
        )

    def get_user_invites_row(self, user_id):
        self._ensure_invite_user(user_id)
        self.cursor.execute('SELECT * FROM user_invites WHERE user_id = ?', (str(user_id),))
        row = self.cursor.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in self.cursor.description]
        return dict(zip(cols, row))

    def get_valid_invites(self, user_id):
        row = self.get_user_invites_row(user_id)
        return row['valid_invites'] if row else 0

    def update_username(self, user_id, username):
        self._ensure_invite_user(user_id)
        self.cursor.execute(
            'UPDATE user_invites SET username = ?, updated_at = ? WHERE user_id = ?',
            (str(username), str(datetime.datetime.now()), str(user_id)),
        )
        self.conn.commit()

    def add_invites(self, user_id, amount=1):
        self._ensure_invite_user(user_id)
        self.cursor.execute(
            'UPDATE user_invites SET invites = invites + ?, valid_invites = valid_invites + ?, updated_at = ? '
            'WHERE user_id = ?',
            (int(amount), int(amount), str(datetime.datetime.now()), str(user_id)),
        )
        self.conn.commit()

    def remove_invites(self, user_id, amount=1):
        self._ensure_invite_user(user_id)
        self.cursor.execute(
            'UPDATE user_invites SET '
            'valid_invites = MAX(0, valid_invites - ?), '
            'invites = MAX(0, invites - ?), '
            'updated_at = ? '
            'WHERE user_id = ?',
            (int(amount), int(amount), str(datetime.datetime.now()), str(user_id)),
        )
        self.conn.commit()

    def register_fake_invite(self, user_id):
        """A joined member left: valid invite becomes fake/invalid."""
        self._ensure_invite_user(user_id)
        self.cursor.execute(
            'UPDATE user_invites SET '
            'valid_invites = MAX(0, valid_invites - 1), '
            'fake_invites = fake_invites + 1, '
            'updated_at = ? '
            'WHERE user_id = ?',
            (str(datetime.datetime.now()), str(user_id)),
        )
        self.conn.commit()

    def reset_invites(self, user_id):
        self._ensure_invite_user(user_id)
        now = str(datetime.datetime.now())
        self.cursor.execute(
            'UPDATE user_invites SET invites = 0, valid_invites = 0, fake_invites = 0, '
            'eligible = 0, completion_notified = 0, updated_at = ? WHERE user_id = ?',
            (now, str(user_id)),
        )
        self.cursor.execute('DELETE FROM invite_events WHERE inviter_id = ?', (str(user_id),))
        self.conn.commit()

    def set_eligible(self, user_id, eligible):
        self._ensure_invite_user(user_id)
        self.cursor.execute(
            'UPDATE user_invites SET eligible = ?, updated_at = ? WHERE user_id = ?',
            (1 if eligible else 0, str(datetime.datetime.now()), str(user_id)),
        )
        self.conn.commit()

    def set_completion_notified(self, user_id, value):
        self._ensure_invite_user(user_id)
        self.cursor.execute(
            'UPDATE user_invites SET completion_notified = ?, updated_at = ? WHERE user_id = ?',
            (1 if value else 0, str(datetime.datetime.now()), str(user_id)),
        )
        self.conn.commit()

    def get_completion_notified(self, user_id):
        row = self.get_user_invites_row(user_id)
        return bool(row and row.get('completion_notified'))

    def get_leaderboard(self, limit=5):
        self.cursor.execute(
            'SELECT user_id, username, valid_invites FROM user_invites '
            'ORDER BY valid_invites DESC LIMIT ?',
            (int(limit),),
        )
        return [
            {'user_id': r[0], 'username': r[1] or 'Unknown', 'valid_invites': r[2] or 0}
            for r in self.cursor.fetchall()
        ]

    def is_member_invite_tracked(self, member_id):
        self.cursor.execute('SELECT 1 FROM invite_events WHERE member_id = ?', (str(member_id),))
        return self.cursor.fetchone() is not None

    def record_invite_join(self, member_id, guild_id, invite_code, inviter_id, counted):
        self.cursor.execute(
            'INSERT OR IGNORE INTO invite_events (member_id, guild_id, invite_code, inviter_id, counted, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (str(member_id), str(guild_id), invite_code, inviter_id, int(counted), str(datetime.datetime.now())),
        )
        self.conn.commit()

    def get_invite_event(self, member_id):
        self.cursor.execute('SELECT * FROM invite_events WHERE member_id = ?', (str(member_id),))
        row = self.cursor.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in self.cursor.description]
        return dict(zip(cols, row))

    def invalidate_invite_event(self, member_id):
        self.cursor.execute(
            'UPDATE invite_events SET counted = 0 WHERE member_id = ?',
            (str(member_id),),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Branding
    # ------------------------------------------------------------------
    def _brand_row_to_dict(self, row):
        if not row:
            return None
        cols = [desc[0] for desc in self.cursor.description]
        return dict(zip(cols, row))

    def get_active_branding(self):
        self.cursor.execute('SELECT * FROM branding WHERE is_active = 1 LIMIT 1')
        brand = self._brand_row_to_dict(self.cursor.fetchone())
        if not brand:
            self.cursor.execute('SELECT * FROM branding LIMIT 1')
            brand = self._brand_row_to_dict(self.cursor.fetchone())
        return brand

    def get_branding(self, profile_name):
        self.cursor.execute('SELECT * FROM branding WHERE profile_name = ?', (str(profile_name),))
        return self._brand_row_to_dict(self.cursor.fetchone())

    def list_branding_profiles(self):
        self.cursor.execute('SELECT profile_name, brand_name, version, is_active FROM branding ORDER BY profile_name')
        return [
            {'profile_name': r[0], 'brand_name': r[1], 'version': r[2], 'is_active': r[3]}
            for r in self.cursor.fetchall()
        ]

    def create_branding_profile(self, profile_name, values: dict):
        now = str(datetime.datetime.now())
        fields = dict(values)
        fields.pop('profile_name', None)
        cols = ', '.join(fields.keys())
        placeholders = ', '.join('?' for _ in fields)
        self.cursor.execute(
            f'INSERT OR IGNORE INTO branding (profile_name, {cols}, version, is_active, updated_at) '
            f'VALUES (?, {placeholders}, 1, 0, ?)',
            (str(profile_name), *fields.values(), now),
        )
        self.conn.commit()

    def activate_branding(self, profile_name):
        self.cursor.execute('UPDATE branding SET is_active = 0')
        self.cursor.execute(
            'UPDATE branding SET is_active = 1, updated_at = ? WHERE profile_name = ?',
            (str(datetime.datetime.now()), str(profile_name)),
        )
        self.conn.commit()

    def update_branding_field(self, profile_name, field, value, bump_version=True):
        if field not in DEFAULT_BRAND and field != 'motd_template':
            raise ValueError(f"Invalid branding field: {field}")
        if bump_version:
            self.cursor.execute(
                f'UPDATE branding SET {field} = ?, version = version + 1, updated_at = ? WHERE profile_name = ?',
                (value, str(datetime.datetime.now()), str(profile_name)),
            )
        else:
            self.cursor.execute(
                f'UPDATE branding SET {field} = ?, updated_at = ? WHERE profile_name = ?',
                (value, str(datetime.datetime.now()), str(profile_name)),
            )
        self.conn.commit()

    def reset_branding(self, profile_name, defaults: dict):
        fields = dict(defaults)
        fields.pop('profile_name', None)
        sets = ', '.join(f'{k} = ?' for k in fields)
        self.cursor.execute(
            f'UPDATE branding SET {sets}, version = version + 1, updated_at = ? WHERE profile_name = ?',
            (*fields.values(), str(datetime.datetime.now()), str(profile_name)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Deployment logs / helpers
    # ------------------------------------------------------------------
    def log_deployment(self, user_id, vps_id, stage, status, message=''):
        self.cursor.execute(
            'INSERT INTO deployment_logs (user_id, vps_id, stage, status, message, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (str(user_id), str(vps_id or ''), str(stage), str(status), str(message)[:900], str(datetime.datetime.now())),
        )
        self.conn.commit()

    def get_last_vps_created_at(self, user_id):
        self.cursor.execute(
            'SELECT MAX(created_at) FROM vps_instances WHERE created_by = ?',
            (str(user_id),),
        )
        row = self.cursor.fetchone()
        return row[0] if row and row[0] else None

    def get_setting(self, key, default=None):
        self.cursor.execute('SELECT value FROM system_settings WHERE key = ?', (key,))
        result = self.cursor.fetchone()
        return int(result[0]) if result else default

    def set_setting(self, key, value):
        self.cursor.execute('INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)', (key, str(value)))
        self.conn.commit()

    def get_stat(self, key, default=0):
        self.cursor.execute('SELECT value FROM usage_stats WHERE key = ?', (key,))
        result = self.cursor.fetchone()
        return result[0] if result else default

    def increment_stat(self, key, amount=1):
        current = self.get_stat(key)
        self.cursor.execute('INSERT OR REPLACE INTO usage_stats (key, value) VALUES (?, ?)', (key, current + amount))
        self.conn.commit()

    def get_vps_by_id(self, vps_id):
        self.cursor.execute('SELECT * FROM vps_instances WHERE vps_id = ?', (vps_id,))
        row = self.cursor.fetchone()
        if not row:
            return None, None
        columns = [desc[0] for desc in self.cursor.description]
        vps = dict(zip(columns, row))
        return vps['token'], vps

    def get_vps_by_token(self, token):
        self.cursor.execute('SELECT * FROM vps_instances WHERE token = ?', (token,))
        row = self.cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in self.cursor.description]
        return dict(zip(columns, row))

    def get_user_vps_count(self, user_id):
        self.cursor.execute('SELECT COUNT(*) FROM vps_instances WHERE created_by = ?', (str(user_id),))
        return self.cursor.fetchone()[0]

    def get_user_vps(self, user_id):
        self.cursor.execute('SELECT * FROM vps_instances WHERE created_by = ?', (str(user_id),))
        columns = [desc[0] for desc in self.cursor.description]
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

    def get_all_vps(self):
        self.cursor.execute('SELECT * FROM vps_instances')
        columns = [desc[0] for desc in self.cursor.description]
        return {row[0]: dict(zip(columns, row)) for row in self.cursor.fetchall()}

    def add_vps(self, vps_data):
        columns = ', '.join(vps_data.keys())
        placeholders = ', '.join('?' for _ in vps_data)
        self.cursor.execute(f'INSERT INTO vps_instances ({columns}) VALUES ({placeholders})', tuple(vps_data.values()))
        self.conn.commit()
        self.increment_stat('total_vps_created')

    def remove_vps(self, token):
        self.cursor.execute('DELETE FROM vps_instances WHERE token = ?', (token,))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def update_vps(self, token, updates):
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [token]
        self.cursor.execute(f'UPDATE vps_instances SET {set_clause} WHERE token = ?', values)
        self.conn.commit()
        return self.cursor.rowcount > 0

    def is_user_banned(self, user_id):
        self.cursor.execute('SELECT 1 FROM banned_users WHERE user_id = ?', (str(user_id),))
        return self.cursor.fetchone() is not None

    def ban_user(self, user_id):
        self.cursor.execute('INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)', (str(user_id),))
        self.conn.commit()

    def unban_user(self, user_id):
        self.cursor.execute('DELETE FROM banned_users WHERE user_id = ?', (str(user_id),))
        self.conn.commit()

    def get_banned_users(self):
        self.cursor.execute('SELECT user_id FROM banned_users')
        return [row[0] for row in self.cursor.fetchall()]

    def add_admin(self, user_id):
        self.cursor.execute('INSERT OR IGNORE INTO admin_users (user_id) VALUES (?)', (str(user_id),))
        self.conn.commit()
        ADMIN_IDS.add(int(user_id))

    def remove_admin(self, user_id):
        self.cursor.execute('DELETE FROM admin_users WHERE user_id = ?', (str(user_id),))
        self.conn.commit()
        if int(user_id) in ADMIN_IDS:
            ADMIN_IDS.remove(int(user_id))

    def get_admins(self):
        self.cursor.execute('SELECT user_id FROM admin_users')
        return [row[0] for row in self.cursor.fetchall()]

    def backup_data(self):
        """Backup all data to a file"""
        data = {
            'vps_instances': self.get_all_vps(),
            'usage_stats': {},
            'system_settings': {},
            'banned_users': self.get_banned_users(),
            'admin_users': self.get_admins()
        }
        
        # Get usage stats
        self.cursor.execute('SELECT * FROM usage_stats')
        for row in self.cursor.fetchall():
            data['usage_stats'][row[0]] = row[1]
            
        # Get system settings
        self.cursor.execute('SELECT * FROM system_settings')
        for row in self.cursor.fetchall():
            data['system_settings'][row[0]] = row[1]
            
        with open(BACKUP_FILE, 'wb') as f:
            pickle.dump(data, f)
            
        return True

    def restore_data(self):
        """Restore data from backup file"""
        if not os.path.exists(BACKUP_FILE):
            return False
            
        try:
            with open(BACKUP_FILE, 'rb') as f:
                data = pickle.load(f)
                
            # Clear all tables
            self.cursor.execute('DELETE FROM vps_instances')
            self.cursor.execute('DELETE FROM usage_stats')
            self.cursor.execute('DELETE FROM system_settings')
            self.cursor.execute('DELETE FROM banned_users')
            self.cursor.execute('DELETE FROM admin_users')
            
            # Restore VPS instances
            for token, vps in data['vps_instances'].items():
                columns = ', '.join(vps.keys())
                placeholders = ', '.join('?' for _ in vps)
                self.cursor.execute(f'INSERT INTO vps_instances ({columns}) VALUES ({placeholders})', tuple(vps.values()))
            
            # Restore usage stats
            for key, value in data['usage_stats'].items():
                self.cursor.execute('INSERT INTO usage_stats (key, value) VALUES (?, ?)', (key, value))
                
            # Restore system settings
            for key, value in data['system_settings'].items():
                self.cursor.execute('INSERT INTO system_settings (key, value) VALUES (?, ?)', (key, value))
                
            # Restore banned users
            for user_id in data['banned_users']:
                self.cursor.execute('INSERT INTO banned_users (user_id) VALUES (?)', (user_id,))
                
            # Restore admin users
            for user_id in data['admin_users']:
                self.cursor.execute('INSERT INTO admin_users (user_id) VALUES (?)', (user_id,))
                ADMIN_IDS.add(int(user_id))
                
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error restoring data: {e}")
            return False

    def close(self):
        self.conn.close()

# Initialize bot with command prefix '/'
class LexoNodesBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = Database(DB_FILE)
        self.invite_tracker = InviteTracker(self.db)
        self.branding = BrandingManager(self.db)
        self.session = None
        self.docker_client = None
        self.system_stats = {
            'cpu_usage': 0,
            'memory_usage': 0,
            'disk_usage': 0,
            'network_io': (0, 0),
            'last_updated': 0
        }
        self.my_persistent_views = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        logger.info("Bot started — initializing services")
        try:
            self.docker_client = docker.from_env()
            logger.info("Docker client initialized successfully")
            self.loop.create_task(self.update_system_stats())
            self.loop.create_task(self.anti_miner_monitor())
            # Reconnect to existing containers
            await self.reconnect_containers()
            # Restore persistent views
            await self.restore_persistent_views()
            # Prime invite caches for invite tracking
            try:
                await self.invite_tracker.prime_all(self)
                logger.info("Invite caches primed for all guilds")
            except Exception as e:
                logger.warning(f"Could not prime invite caches: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            self.docker_client = None

    async def reconnect_containers(self):
        """Reconnect to existing containers on startup"""
        if not self.docker_client:
            return
            
        for token, vps in list(self.db.get_all_vps().items()):
            if vps['status'] == 'running':
                try:
                    container = self.docker_client.containers.get(vps['container_id'])
                    if container.status != 'running':
                        container.start()
                    logger.info(f"Reconnected and started container for VPS {vps['vps_id']}")
                except docker.errors.NotFound:
                    logger.warning(f"Container {vps['container_id']} not found, removing from data")
                    self.db.remove_vps(token)
                except Exception as e:
                    logger.error(f"Error reconnecting container {vps['vps_id']}: {e}")

    async def restore_persistent_views(self):
        """Restore persistent views after restart"""
        # This would be implemented to restore any persistent UI components
        pass

    async def anti_miner_monitor(self):
        """Periodically check for mining activities"""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for token, vps in self.db.get_all_vps().items():
                    if vps['status'] != 'running':
                        continue
                    try:
                        container = self.docker_client.containers.get(vps['container_id'])
                        if container.status != 'running':
                            continue
                        
                        # Check processes
                        exec_result = container.exec_run("ps aux")
                        output = exec_result.output.decode().lower()
                        
                        for pattern in MINER_PATTERNS:
                            if pattern in output:
                                logger.warning(f"Mining detected in VPS {vps['vps_id']}, suspending...")
                                container.stop()
                                self.db.update_vps(token, {'status': 'suspended'})
                                # Notify owner
                                try:
                                    owner = await self.fetch_user(int(vps['created_by']))
                                    await owner.send(f"⚠️ Your VPS {vps['vps_id']} has been suspended due to detected mining activity. Contact admin to unsuspend.")
                                except:
                                    pass
                                break
                    except Exception as e:
                        logger.error(f"Error checking VPS {vps['vps_id']} for mining: {e}")
            except Exception as e:
                logger.error(f"Error in anti_miner_monitor: {e}")
            await asyncio.sleep(300)  # Check every 5 minutes

    async def update_system_stats(self):
        """Update system statistics periodically"""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                # CPU usage
                cpu_percent = psutil.cpu_percent(interval=1)
                
                # Memory usage
                mem = psutil.virtual_memory()
                
                # Disk usage
                disk = psutil.disk_usage('/')
                
                # Network IO
                net_io = psutil.net_io_counters()
                
                self.system_stats = {
                    'cpu_usage': cpu_percent,
                    'memory_usage': mem.percent,
                    'memory_used': mem.used / (1024 ** 3),  # GB
                    'memory_total': mem.total / (1024 ** 3),  # GB
                    'disk_usage': disk.percent,
                    'disk_used': disk.used / (1024 ** 3),  # GB
                    'disk_total': disk.total / (1024 ** 3),  # GB
                    'network_sent': net_io.bytes_sent / (1024 ** 2),  # MB
                    'network_recv': net_io.bytes_recv / (1024 ** 2),  # MB
                    'last_updated': time.time()
                }
            except Exception as e:
                logger.error(f"Error updating system stats: {e}")
            await asyncio.sleep(30)

    async def close(self):
        await super().close()
        if self.session:
            await self.session.close()
        if self.docker_client:
            self.docker_client.close()
        self.db.close()

def generate_token():
    """Generate a random token for VPS access"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=24))

def generate_vps_id():
    """Generate a unique VPS ID"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

def generate_ssh_password():
    """Generate a random SSH password"""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choices(chars, k=16))

def has_admin_role(ctx):
    """Check if user has admin role or is in ADMIN_IDS"""
    if isinstance(ctx, discord.Interaction):
        user_id = ctx.user.id
        roles = ctx.user.roles
    else:
        user_id = ctx.author.id
        roles = ctx.author.roles

    if user_id in ADMIN_IDS:
        return True

    return any(role.id == ADMIN_ROLE_ID for role in roles)


def is_admin(ctx):
    """Admin check: existing ADMIN_IDS/role OR Discord Administrator permission."""
    if has_admin_role(ctx):
        return True
    try:
        if isinstance(ctx, discord.Interaction):
            member = ctx.user
            guild = ctx.guild
        else:
            member = ctx.author
            guild = ctx.guild
        if guild and isinstance(member, discord.Member) and member.guild_permissions.administrator:
            return True
    except Exception:
        pass
    return False


def parse_created_at(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        try:
            return datetime.datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')
        except Exception:
            return None


def progress_bar(pct, width=10):
    pct = max(0, min(100, int(pct)))
    filled = int(width * pct / 100)
    return '█' * filled + '░' * (width - filled)


async def edit_progress(status_msg, pct, label, extra=''):
    body = f"🚀 VPS Deployment\n\n[{progress_bar(pct)}] {pct}%\n\n{label}"
    if extra:
        body += f"\n{extra}"
    try:
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send(body, ephemeral=True)
        else:
            await status_msg.edit(content=body)
    except Exception as e:
        logger.debug(f"Progress update skipped: {e}")


async def send_log_channel(bot, content):
    channel_id = bot.db.get_setting('log_channel_id', 0)
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel:
        try:
            await channel.send(content)
        except Exception as e:
            logger.warning(f"Could not send log channel message: {e}")

async def capture_ssh_session_line(process):
    """Capture the SSH session line from tmate output"""
    try:
        while True:
            output = await process.stdout.readline()
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output:
                return output.split("ssh session:")[1].strip()
        return None
    except Exception as e:
        logger.error(f"Error capturing SSH session: {e}")
        return None

async def run_docker_command(container_id, command, timeout=120):
    """Run a Docker command asynchronously with timeout"""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode != 0:
                raise Exception(f"Command failed: {stderr.decode()}")
            return True, stdout.decode()
        except asyncio.TimeoutError:
            process.kill()
            raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        logger.error(f"Error running Docker command: {e}")
        return False, str(e)

async def kill_apt_processes(container_id):
    """Kill any running apt processes"""
    try:
        success, _ = await run_docker_command(container_id, ["bash", "-c", "killall apt apt-get dpkg || true"])
        await asyncio.sleep(2)
        success, _ = await run_docker_command(container_id, ["bash", "-c", "rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock*"])
        await asyncio.sleep(2)
        return success
    except Exception as e:
        logger.error(f"Error killing apt processes: {e}")
        return False

async def wait_for_apt_lock(container_id, status_msg):
    """Wait for apt lock to be released"""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            await kill_apt_processes(container_id)
            
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", container_id, "bash", "-c", "lsof /var/lib/dpkg/lock-frontend",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return True
                
            if isinstance(status_msg, discord.Interaction):
                await status_msg.followup.send(f"🔄 Waiting for package manager to be ready... (Attempt {attempt + 1}/{max_attempts})", ephemeral=True)
            else:
                await status_msg.edit(content=f"🔄 Waiting for package manager to be ready... (Attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error checking apt lock: {e}")
            await asyncio.sleep(5)
    
    return False

async def build_custom_image(vps_id, username, root_password, user_password, base_image=DEFAULT_OS_IMAGE):
    """Build a custom Docker image using our template"""
    try:
        # Create a temporary directory for the Dockerfile
        temp_dir = f"temp_dockerfiles/{vps_id}"
        os.makedirs(temp_dir, exist_ok=True)
        
        # Generate Dockerfile content
        dockerfile_content = DOCKERFILE_TEMPLATE.format(
            base_image=base_image,
            root_password=root_password,
            username=username,
            user_password=user_password,
            welcome_message=WELCOME_MESSAGE,
            watermark=WATERMARK,
            vps_id=vps_id
        )
        
        # Write Dockerfile
        dockerfile_path = os.path.join(temp_dir, "Dockerfile")
        with open(dockerfile_path, 'w') as f:
            f.write(dockerfile_content)
        
        # Build the image
        image_tag = f"lexonodes/{vps_id.lower()}:latest"
        build_process = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", image_tag, temp_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await build_process.communicate()
        
        if build_process.returncode != 0:
            raise Exception(f"Failed to build image: {stderr.decode()}")
        
        return image_tag
    except Exception as e:
        logger.error(f"Error building custom image: {e}")
        raise
    finally:
        # Clean up temporary directory
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.error(f"Error cleaning up temp directory: {e}")

async def setup_container(container_id, status_msg, memory, username, vps_id=None, use_custom_image=False):
    """Enhanced container setup with LexoNodes customization"""
    try:
        # Ensure container is running
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🔍 Checking container status...", ephemeral=True)
        else:
            await status_msg.edit(content="🔍 Checking container status...")
            
        container = bot.docker_client.containers.get(container_id)
        if container.status != "running":
            if isinstance(status_msg, discord.Interaction):
                await status_msg.followup.send("🚀 Starting container...", ephemeral=True)
            else:
                await status_msg.edit(content="🚀 Starting container...")
            container.start()
            await asyncio.sleep(5)

        # Generate SSH password
        ssh_password = generate_ssh_password()
        
        # Install tmate and other required packages
        if not use_custom_image:
            if isinstance(status_msg, discord.Interaction):
                await status_msg.followup.send("📦 Installing required packages...", ephemeral=True)
            else:
                await status_msg.edit(content="📦 Installing required packages...")
                
            # Update package list
            success, output = await run_docker_command(container_id, ["apt-get", "update"])
            if not success:
                raise Exception(f"Failed to update package list: {output}")

            # Install packages
            packages = [
                "tmate", "neofetch", "screen", "wget", "curl", "htop", "nano", "vim", 
                "openssh-server", "sudo", "ufw", "git", "docker.io", "systemd", "systemd-sysv"
            ]
            success, output = await run_docker_command(container_id, ["apt-get", "install", "-y"] + packages)
            if not success:
                raise Exception(f"Failed to install packages: {output}")

        # Setup SSH
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🔐 Configuring SSH access...", ephemeral=True)
        else:
            await status_msg.edit(content="🔐 Configuring SSH access...")
            
        # Create user and set password (if not using custom image)
        if not use_custom_image:
            user_setup_commands = [
                f"useradd -m -s /bin/bash {username}",
                f"echo '{username}:{ssh_password}' | chpasswd",
                f"usermod -aG sudo {username}",
                "sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config",
                "sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config",
                "service ssh restart"
            ]
            
            for cmd in user_setup_commands:
                success, output = await run_docker_command(container_id, ["bash", "-c", cmd])
                if not success:
                    raise Exception(f"Failed to setup user: {output}")

        # Set LexoNodes customization
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("🎨 Setting up LexoNodes customization...", ephemeral=True)
        else:
            await status_msg.edit(content="🎨 Setting up LexoNodes customization...")
            
        # Create welcome message file
        welcome_cmd = f"echo '{WELCOME_MESSAGE}' > /etc/motd && echo 'echo \"{WELCOME_MESSAGE}\"' >> /home/{username}/.bashrc"
        success, output = await run_docker_command(container_id, ["bash", "-c", welcome_cmd])
        if not success:
            logger.warning(f"Could not set welcome message: {output}")

        # Set hostname and watermark
        if not vps_id:
            vps_id = generate_vps_id()
        hostname_cmd = f"echo 'lexonodes-{vps_id}' > /etc/hostname && hostname lexonodes-{vps_id}"
        success, output = await run_docker_command(container_id, ["bash", "-c", hostname_cmd])
        if not success:
            raise Exception(f"Failed to set hostname: {output}")

        # Set memory limit in cgroup
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("⚙️ Setting resource limits...", ephemeral=True)
        else:
            await status_msg.edit(content="⚙️ Setting resource limits...")
            
        memory_bytes = memory * 1024 * 1024 * 1024
        success, output = await run_docker_command(container_id, ["bash", "-c", f"echo {memory_bytes} > /sys/fs/cgroup/memory.max"])
        if not success:
            logger.warning(f"Could not set memory limit in cgroup: {output}")

        # Set watermark in machine info
        success, output = await run_docker_command(container_id, ["bash", "-c", f"echo '{WATERMARK}' > /etc/machine-info"])
        if not success:
            logger.warning(f"Could not set machine info: {output}")

        # Basic security setup
        security_commands = [
            "ufw allow ssh",
            "ufw --force enable",
            "apt-get -y autoremove",
            "apt-get clean",
            f"chown -R {username}:{username} /home/{username}",
            f"chmod 700 /home/{username}"
        ]
        
        for cmd in security_commands:
            success, output = await run_docker_command(container_id, ["bash", "-c", cmd])
            if not success:
                logger.warning(f"Security setup command failed: {cmd} - {output}")

        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send("✅ LexoNodes VPS setup completed successfully!", ephemeral=True)
        else:
            await status_msg.edit(content="✅ LexoNodes VPS setup completed successfully!")
            
        return True, ssh_password, vps_id
    except Exception as e:
        error_msg = f"Setup failed: {str(e)}"
        logger.error(error_msg)
        if isinstance(status_msg, discord.Interaction):
            await status_msg.followup.send(f"❌ {error_msg}", ephemeral=True)
        else:
            await status_msg.edit(content=f"❌ {error_msg}")
        return False, None, None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = LexoNodesBot(command_prefix='/', intents=intents, help_command=None)

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    
    # Auto-start VPS containers based on status
    if bot.docker_client:
        for token, vps in bot.db.get_all_vps().items():
            if vps['status'] == 'running':
                try:
                    container = bot.docker_client.containers.get(vps["container_id"])
                    if container.status != "running":
                        container.start()
                        logger.info(f"Started container for VPS {vps['vps_id']}")
                except docker.errors.NotFound:
                    logger.warning(f"Container {vps['container_id']} not found")
                except Exception as e:
                    logger.error(f"Error starting container: {e}")
    
    try:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="LexoNodes VPS"))
        synced_commands = await bot.tree.sync()
        logger.info(f"Synced {len(synced_commands)} slash commands")
    except Exception as e:
        logger.error(f"Error syncing slash commands: {e}")


@bot.event
async def on_member_join(member):
    """Detect invite usage and credit the inviter."""
    try:
        inviter_id = await bot.invite_tracker.handle_member_join(member)
        if not inviter_id:
            return
        required = bot.db.get_setting('required_invites', 5)
        valid = bot.db.get_valid_invites(inviter_id)
        eligible, _ = eligibility_status(valid, required)
        bot.db.set_eligible(inviter_id, 1 if eligible else 0)
        if eligible:
            await bot.invite_tracker.notify_completion(bot, inviter_id, valid, required)
        # Optional live log channel
        log_id = bot.db.get_setting('log_channel_id', 0)
        if log_id:
            channel = bot.get_channel(int(log_id))
            if channel:
                try:
                    await channel.send(
                        f"🎟️ Invite detected: <@{inviter_id}> → {valid}/{required} "
                        f"(joined: {member})"
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error in on_member_join: {e}")


@bot.event
async def on_member_remove(member):
    """Invalidate invites when a previously tracked member leaves."""
    try:
        inviter_id = await bot.invite_tracker.handle_member_leave(member)
        if not inviter_id:
            return
        required = bot.db.get_setting('required_invites', 5)
        valid = bot.db.get_valid_invites(inviter_id)
        eligible, _ = eligibility_status(valid, required)
        bot.db.set_eligible(inviter_id, 1 if eligible else 0)
        if not eligible:
            # Goal no longer met — allow a fresh completion notice if they re-qualify
            bot.db.set_completion_notified(inviter_id, 0)
        logger.info(f"Invite count adjusted for {inviter_id}: valid={valid} eligible={eligible}")
    except Exception as e:
        logger.error(f"Error in on_member_remove: {e}")

@bot.hybrid_command(name='help', description='Show all available commands')
async def show_commands(ctx):
    """Show all available commands"""
    try:
        embed = discord.Embed(title="🤖 LexoNodes VPS Bot Commands", color=discord.Color.blue())
        
        # User commands
        embed.add_field(name="User Commands", value="""
`/create_vps` - Create a new VPS (Admin only)
`/createvps` - Create a VPS (invite-gated)
`/invites` - Show your invite progress
`/leaderboard` - Top inviters
`/vps` - Show your VPS information
`/refresh-motd` - Refresh branded MOTD on your VPS
`/connect_vps <token>` - Connect to your VPS
`/list` - List all your VPS instances
`/help` - Show this help message
`/manage_vps <vps_id>` - Manage your VPS
`/transfer_vps <vps_id> <user>` - Transfer VPS ownership
`/vps_stats <vps_id>` - Show VPS resource usage
`/change_ssh_password <vps_id>` - Change SSH password
`/vps_shell <vps_id>` - Get shell access to your VPS
`/vps_console <vps_id>` - Get direct console access to your VPS
`/vps_usage` - Show your VPS usage statistics
""", inline=False)
        
        # Admin commands
        if has_admin_role(ctx):
            embed.add_field(name="Admin Commands", value="""
`/vps_list` - List all VPS instances
`/delete_vps <vps_id>` - Delete a VPS
`/admin_stats` - Show system statistics
`/cleanup_vps` - Cleanup inactive VPS instances
`/add_admin <user>` - Add a new admin
`/remove_admin <user>` - Remove an admin (Owner only)
`/list_admins` - List all admin users
`/system_info` - Show detailed system information
`/container_limit <max>` - Set maximum container limit
`/global_stats` - Show global usage statistics
`/migrate_vps <vps_id>` - Migrate VPS to another host
`/emergency_stop <vps_id>` - Force stop a problematic VPS
`/emergency_remove <vps_id>` - Force remove a problematic VPS
`/suspend_vps <vps_id>` - Suspend a VPS
`/unsuspend_vps <vps_id>` - Unsuspend a VPS
`/edit_vps <vps_id> <memory> <cpu> <disk>` - Edit VPS specifications
`/ban_user <user>` - Ban a user from creating VPS
`/unban_user <user>` - Unban a user
`/list_banned` - List banned users
`/backup_data` - Backup all data
`/restore_data` - Restore from backup
`/reinstall_bot` - Reinstall the bot (Owner only)
`/setinvites <amount>` - Set required invite count
`/addinvites <user> <amount>` - Manually add invites
`/removeinvites <user> <amount>` - Remove invites
`/resetinvites <user>` - Reset invite progress
`/blacklist <user>` - Blacklist a user from VPS creation
`/unblacklist <user>` - Remove blacklist
`/vps-enable` / `/vps-disable` - Toggle VPS deployment
`/setlogchannel <channel>` - Set deployment log channel
`/setcompletionchannel <channel>` - Set invite completion channel
`/brand` - Show current branding
`/brand-name|tagline|website|discord|support|motd` - Edit branding
`/brand-create` / `/brand-use` / `/brand-reset` - Multi-brand profiles
`/brand-template` - Custom MOTD template
`/brand-reinstall <vps>` - Reinstall branding/MOTD
`/brand-update-existing` - Push branding to live VPS instances
""", inline=False)
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in show_commands: {e}")
        await ctx.send("❌ An error occurred while processing your request.")

@bot.hybrid_command(name='add_admin', description='Add a new admin (Admin only)')
@app_commands.describe(
    user="User to make admin"
)
async def add_admin(ctx, user: discord.User):
    """Add a new admin user"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    
    bot.db.add_admin(user.id)
    await ctx.send(f"✅ {user.mention} has been added as an admin!")

@bot.hybrid_command(name='remove_admin', description='Remove an admin (Owner only)')
@app_commands.describe(
    user="User to remove from admin"
)
async def remove_admin(ctx, user: discord.User):
    """Remove an admin user (Owner only)"""
    if ctx.author.id != 1210291131301101618:  # Only the owner can remove admins
        await ctx.send("❌ Only the owner can remove admins!", ephemeral=True)
        return
    
    bot.db.remove_admin(user.id)
    await ctx.send(f"✅ {user.mention} has been removed from admins!")

@bot.hybrid_command(name='list_admins', description='List all admin users')
async def list_admins(ctx):
    """List all admin users"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    
    embed = discord.Embed(title="Admin Users", color=discord.Color.blue())
    
    # List user IDs in ADMIN_IDS
    admin_list = []
    for admin_id in ADMIN_IDS:
        try:
            user = await bot.fetch_user(admin_id)
            admin_list.append(f"{user.name} ({user.id})")
        except:
            admin_list.append(f"Unknown User ({admin_id})")
    
    # List users with admin role
    if ctx.guild:
        admin_role = ctx.guild.get_role(ADMIN_ROLE_ID)
        if admin_role:
            role_admins = [f"{member.name} ({member.id})" for member in admin_role.members]
            admin_list.extend(role_admins)
    
    if not admin_list:
        embed.description = "No admins found"
    else:
        embed.description = "\n".join(sorted(set(admin_list)))  # Remove duplicates
    
    await ctx.send(embed=embed, ephemeral=True)

async def make_container_exec(container_id):
    """Provider adapter: base64 payload -> run bash inside the VPS container.

    Falls back to paramiko SSH when docker exec is unavailable.
    """
    async def exec_fn(payload):
        ok, output = await run_docker_command(
            container_id,
            ["bash", "-c", f"echo {payload} | base64 -d | bash"],
            timeout=120,
        )
        if ok:
            return True, output
        # Paramiko fallback via container IP (never logs credentials)
        try:
            container = bot.docker_client.containers.get(container_id)
            container.reload()
            ip = container.attrs.get('NetworkSettings', {}).get('IPAddress') or ''
            if not ip:
                for net in (container.attrs.get('NetworkSettings', {}) or {}).get('Networks', {}).values():
                    if net.get('IPAddress'):
                        ip = net['IPAddress']
                        break
            if not ip:
                return False, output
            # Derive credentials from stored VPS rows if present
            token, vps = None, None
            for t, v in bot.db.get_all_vps().items():
                if v.get('container_id') == container_id:
                    token, vps = t, v
                    break
            if not vps:
                return False, output
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                ip, 22, vps.get('username') or 'root',
                vps.get('password') or '',
                timeout=10, banner_timeout=10, auth_timeout=10,
            )
            # Decode payload and run via SSH
            import shlex
            cmd = f"echo {shlex.quote(payload)} | base64 -d | bash"
            _sin, stdout, stderr = client.exec_command(cmd, timeout=120)
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            client.close()
            code = stdout.channel.recv_exit_status()
            return (code == 0, out + err)
        except Exception as e:
            logger.warning(f"SSH fallback failed for container {container_id}: {e}")
            return False, output

    return exec_fn


async def post_deployment_setup(vps_data, status_msg=None):
    """VPS created -> branding -> MOTD -> finalize. Never fails the VPS itself.

    Returns (ok, message).
    """
    token = vps_data.get('token')
    vps_id = vps_data.get('vps_id')
    container_id = vps_data.get('container_id')
    brand = bot.branding.get_active()
    branding_ok = False
    motd_ok = False
    messages = []

    logger.info(f"Post-deployment setup started for VPS {vps_id}")
    bot.db.log_deployment(vps_data.get('created_by'), vps_id, 'post_deployment', 'started')

    if not bot.docker_client or not container_id:
        return False, "Docker unavailable — branding skipped"

    try:
        exec_fn = await make_container_exec(container_id)
    except Exception as e:
        logger.error(f"Could not create exec adapter: {e}")
        bot.db.log_deployment(vps_data.get('created_by'), vps_id, 'post_deployment', 'failed', str(e))
        return False, str(e)

    if status_msg:
        await edit_progress(status_msg, 80, "→ Installing VPS branding...")

    try:
        branding_ok, brand_msg = await install_branding_files(exec_fn, brand or {})
        messages.append(brand_msg)
        logger.info(f"Branding install for {vps_id}: {brand_msg}")
    except Exception as e:
        brand_msg = str(e)
        messages.append(brand_msg)
        logger.error(f"Branding installation failed for {vps_id}: {e}")
    bot.db.log_deployment(vps_data.get('created_by'), vps_id, 'branding',
                          'ok' if branding_ok else 'failed', brand_msg)

    if brand and int(brand.get('motd_enabled') or 0):
        if status_msg:
            await edit_progress(status_msg, 90, "→ Installing MOTD...")
        try:
            motd_ok, motd_msg = await run_installer(exec_fn, brand)
            messages.append(motd_msg)
            logger.info(f"MOTD install for {vps_id}: {motd_msg}")
        except Exception as e:
            motd_msg = str(e)
            messages.append(motd_msg)
            logger.error(f"MOTD installation failed for {vps_id}: {e}")
        bot.db.log_deployment(vps_data.get('created_by'), vps_id, 'motd',
                              'ok' if motd_ok else 'failed', motd_msg)
    else:
        motd_msg = "MOTD disabled"
        messages.append(motd_msg)

    # Finalize: stamp branding version on the VPS record
    brand_label = "None"
    if brand:
        brand_label = f"{brand.get('brand_name', 'Unknown')} v{brand.get('version', 1)}"
    try:
        bot.db.update_vps(token, {'branding_version': brand_label})
        expected_ok = branding_ok and (
            motd_ok or not int((brand or {}).get('motd_enabled') or 0)
        )
        bot.db.log_deployment(
            vps_data.get('created_by'), vps_id, 'post_deployment',
            'ok' if expected_ok else 'partial',
            '; '.join(messages),
        )
    except Exception as e:
        logger.warning(f"Could not stamp branding version on {vps_id}: {e}")

    vps_data['branding_ok'] = branding_ok
    vps_data['motd_ok'] = motd_ok
    vps_data['branding_version'] = brand_label
    vps_data['post_messages'] = messages

    all_ok = branding_ok and (motd_ok or not int((brand or {}).get('motd_enabled') or 0))
    result = "Branding and MOTD installed" if all_ok else "VPS created; branding/MOTD incomplete — retry with /brand-reinstall"
    logger.info(f"Post-deployment setup finished for VPS {vps_id}: {result}")
    return all_ok, result


async def provision_vps_core(ctx, owner, memory, cpu, disk,
                             os_image=DEFAULT_OS_IMAGE, use_custom_image=True,
                             status_msg=None):
    """Existing VPS provisioning logic, isolated for reuse by /create_vps and /createvps.

    Returns (success, vps_data_or_None, error_message_or_None).
    """
    container = None
    logger.info(f"VPS deployment started for user {owner.id} ({memory}GB/{cpu}C/{disk}GB)")

    async def _reject(msg, ephemeral=True):
        try:
            if status_msg is None:
                await ctx.send(f"❌ {msg}", ephemeral=ephemeral)
            else:
                await status_msg.edit(content=f"❌ {msg}")
        except Exception:
            pass
        return False, None, msg

    if bot.db.is_user_banned(owner.id):
        return await _reject("This user is banned/blacklisted from creating VPS!")

    if not ctx.guild:
        return await _reject("This command can only be used in a server!")

    if not bot.docker_client:
        return await _reject("Docker is not available. Please contact the administrator.")

    try:
        # Validate inputs
        if memory < 1 or memory > 5120000:
            return await _reject("Memory must be between 1GB and 512GB")
        if cpu < 1 or cpu > 320000:
            return await _reject("CPU cores must be between 1 and 32")
        if disk < 10 or disk > 10000000:
            return await _reject("Disk space must be between 10GB and 1000GB")

        # Check if we've reached container limit
        containers = bot.docker_client.containers.list(all=True)
        if len(containers) >= bot.db.get_setting('max_containers', MAX_CONTAINERS):
            return await _reject(
                f"Maximum container limit reached ({bot.db.get_setting('max_containers')}). "
                "Please delete some VPS instances first."
            )

        # Check if user already has maximum VPS instances
        if bot.db.get_user_vps_count(owner.id) >= bot.db.get_setting('max_vps_per_user', MAX_VPS_PER_USER):
            return await _reject(
                f"{owner.mention} already has the maximum number of VPS instances "
                f"({bot.db.get_setting('max_vps_per_user')})"
            )

        # Global total VPS cap
        max_total = bot.db.get_setting('max_total_vps', 0)
        if max_total and len(bot.db.get_all_vps()) >= max_total:
            return await _reject(f"Global VPS limit reached ({max_total}). Please try again later.")

        if status_msg is None:
            status_msg = await ctx.send("🚀 Creating LexoNodes VPS instance... This may take a few minutes.")
        else:
            await edit_progress(status_msg, 20, "✓ VPS request accepted\n→ Building resources...")

        bot.db.log_deployment(owner.id, '', 'deployment', 'started', f"{memory}GB/{cpu}C/{disk}GB")

        memory_bytes = memory * 1024 * 1024 * 1024
        vps_id = generate_vps_id()
        username = owner.name.lower().replace(" ", "_")[:20]
        root_password = generate_ssh_password()
        user_password = generate_ssh_password()
        token = generate_token()

        if use_custom_image:
            await status_msg.edit(content="🔨 Building custom Docker image...")
            try:
                image_tag = await build_custom_image(vps_id, username, root_password, user_password, os_image)
            except Exception as e:
                bot.db.log_deployment(owner.id, vps_id, 'deployment', 'failed', f"image build: {e}")
                logger.error(f"VPS deployment failed for {owner.id}: image build {e}")
                await status_msg.edit(content=f"❌ Failed to build Docker image: {str(e)}")
                return False, None, f"Failed to build Docker image: {str(e)}"

            await status_msg.edit(content="⚙️ Initializing container...")
            try:
                container = bot.docker_client.containers.run(
                    image_tag,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(cpu * 100000),
                    cap_add=["ALL"],
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{vps_id}': {'bind': '/data', 'mode': 'rw'}
                    },
                    restart_policy={"Name": "always"}
                )
            except Exception as e:
                bot.db.log_deployment(owner.id, vps_id, 'deployment', 'failed', f"container start: {e}")
                logger.error(f"VPS deployment failed for {owner.id}: container start {e}")
                await status_msg.edit(content=f"❌ Failed to start container: {str(e)}")
                return False, None, f"Failed to start container: {str(e)}"
        else:
            await status_msg.edit(content="⚙️ Initializing container...")
            try:
                container = bot.docker_client.containers.run(
                    os_image,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(cpu * 100000),
                    cap_add=["ALL"],
                    command="tail -f /dev/null",
                    tty=True,
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{vps_id}': {'bind': '/data', 'mode': 'rw'}
                    },
                    restart_policy={"Name": "always"}
                )
            except docker.errors.ImageNotFound:
                await status_msg.edit(content=f"❌ OS image {os_image} not found. Using default {DEFAULT_OS_IMAGE}")
                container = bot.docker_client.containers.run(
                    DEFAULT_OS_IMAGE,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(cpu * 100000),
                    cap_add=["ALL"],
                    command="tail -f /dev/null",
                    tty=True,
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{vps_id}': {'bind': '/data', 'mode': 'rw'}
                    },
                    restart_policy={"Name": "always"}
                )
                os_image = DEFAULT_OS_IMAGE

        await status_msg.edit(content="🔧 Container created. Setting up environment...")
        await asyncio.sleep(5)

        setup_success, ssh_password, _ = await setup_container(
            container.id,
            status_msg,
            memory,
            username,
            vps_id,
            use_custom_image=use_custom_image
        )
        if not setup_success:
            raise Exception("Failed to setup container")

        await status_msg.edit(content="🔐 Starting SSH session...")

        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container.id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        if not ssh_session_line:
            raise Exception("Failed to get tmate session")

        vps_data = {
            "token": token,
            "vps_id": vps_id,
            "container_id": container.id,
            "memory": memory,
            "cpu": cpu,
            "disk": disk,
            "username": username,
            "password": ssh_password,
            "root_password": root_password if use_custom_image else None,
            "created_by": str(owner.id),
            "created_at": str(datetime.datetime.now()),
            "tmate_session": ssh_session_line,
            "watermark": WATERMARK,
            "os_image": os_image,
            "restart_count": 0,
            "last_restart": None,
            "status": "running",
            "use_custom_image": use_custom_image
        }

        bot.db.add_vps(vps_data)
        logger.info(f"VPS created: {vps_id} for user {owner.id}")
        bot.db.log_deployment(owner.id, vps_id, 'deployment', 'created')
        await send_log_channel(bot, f"🚀 VPS `{vps_id}` created for <@{owner.id}>")

        # Post-deployment: branding + MOTD (failure does not fail the VPS)
        post_ok = True
        post_msg = ""
        try:
            await edit_progress(status_msg, 75, "✓ VPS created\n→ Installing VPS branding...")
            post_ok, post_msg = await post_deployment_setup(vps_data, status_msg)
        except Exception as e:
            post_ok = False
            post_msg = str(e)
            logger.error(f"Post-deployment setup error for {vps_id}: {e}")

        # Deliver credentials via DM only (never public)
        try:
            embed = discord.Embed(title="🎉 VPS Creation Successful", color=discord.Color.green())
            embed.add_field(name="🆔 VPS ID", value=vps_id, inline=True)
            embed.add_field(name="💾 Memory", value=f"{memory}GB", inline=True)
            embed.add_field(name="⚡ CPU", value=f"{cpu} cores", inline=True)
            embed.add_field(name="💿 Disk", value=f"{disk}GB", inline=True)
            embed.add_field(name="👤 Username", value=username, inline=True)
            embed.add_field(name="🔑 User Password", value=f"||{ssh_password}||", inline=False)
            if use_custom_image:
                embed.add_field(name="🔑 Root Password", value=f"||{root_password}||", inline=False)
            embed.add_field(name="🔒 Tmate Session", value=f"```{ssh_session_line}```", inline=False)
            embed.add_field(name="🔌 Direct SSH", value=f"```ssh {username}@<server-ip>```", inline=False)
            embed.add_field(name="🎨 Branding", value=vps_data.get('branding_version') or "None", inline=True)
            embed.add_field(name="MOTD", value="Installed" if vps_data.get('motd_ok') else "Not installed", inline=True)
            if not post_ok:
                embed.add_field(
                    name="⚠️ Note",
                    value="VPS is available. Branding/MOTD incomplete — retry with `/brand-reinstall`.",
                    inline=False,
                )
            embed.add_field(
                name="ℹ️ Info",
                value="This is a managed VPS instance. You can install and configure additional packages as needed.",
                inline=False,
            )

            await owner.send(embed=embed)
            if not post_ok:
                await status_msg.edit(
                    content=(
                        f"✅ VPS Created\n\n"
                        f"⚠️ Branding installation incomplete: {post_msg}\n"
                        f"The VPS is still available.\n\n"
                        f"Retry branding later with:\n/brand-reinstall {vps_id}"
                    )
                )
            else:
                await status_msg.edit(
                    content=(
                        f"✅ VPS Ready!\n\n"
                        f"VPS ID: `{vps_id}`\n"
                        f"Username: `{username}`\n"
                        f"Branding: {vps_data.get('branding_version') or 'None'}\n"
                        f"MOTD: {'Installed' if vps_data.get('motd_ok') else 'Disabled'}\n\n"
                        f"Connection details (including passwords) were sent to your DMs."
                    )
                )
        except discord.Forbidden:
            await status_msg.edit(
                content=f"❌ I couldn't send a DM to {owner.mention}. Please ask them to enable DMs from server members."
            )

        logger.info(f"VPS deployment completed: {vps_id}")
        return True, vps_data, None

    except Exception as e:
        error_msg = f"❌ An error occurred while creating the VPS: {str(e)}"
        logger.error(f"VPS deployment failed for {owner.id}: {e}")
        bot.db.log_deployment(owner.id, '', 'deployment', 'failed', str(e))
        await send_log_channel(bot, f"❌ VPS deployment failed for <@{owner.id}>: {e}")
        try:
            if status_msg is not None:
                await status_msg.edit(content=error_msg)
            else:
                await ctx.send(error_msg)
        except Exception:
            pass
        if container is not None:
            try:
                container.stop()
                container.remove()
            except Exception as ce:
                logger.error(f"Error cleaning up container: {ce}")
        return False, None, str(e)


@bot.hybrid_command(name='create_vps', description='Create a new VPS (Admin only)')
@app_commands.describe(
    memory="Memory in GB",
    cpu="CPU cores",
    disk="Disk space in GB",
    owner="User who will own the VPS",
    os_image="OS image to use",
    use_custom_image="Use custom LexoNodes image (recommended)"
)
async def create_vps_command(ctx, memory: int, cpu: int, disk: int, owner: discord.Member,
                           os_image: str = DEFAULT_OS_IMAGE, use_custom_image: bool = True):
    """Create a new VPS with specified parameters (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    if bot.db.is_user_banned(owner.id):
        await ctx.send("❌ This user is banned from creating VPS!", ephemeral=True)
        return

    if not ctx.guild:
        await ctx.send("❌ This command can only be used in a server!", ephemeral=True)
        return

    if not bot.docker_client:
        await ctx.send("❌ Docker is not available. Please contact the administrator.", ephemeral=True)
        return

    ok, _vps, err = await provision_vps_core(
        ctx, owner, memory, cpu, disk,
        os_image=os_image, use_custom_image=use_custom_image,
    )
    if not ok and err:
        logger.warning(f"Admin /create_vps failed for {owner.id}: {err}")


@bot.hybrid_command(name='createvps', description='Create a VPS (requires enough invites)')
@app_commands.describe(
    memory="Memory in GB (optional, defaults to plan)",
    cpu="CPU cores (optional, defaults to plan)",
    disk="Disk space in GB (optional, defaults to plan)",
)
async def createvps_command(ctx, memory: int = None, cpu: int = None, disk: int = None):
    """User-facing deployment gated by invite eligibility, cooldown and limits."""
    user = ctx.author

    # 1. Blacklist
    if bot.db.is_user_banned(user.id):
        await ctx.send("❌ You are blacklisted from VPS creation.", ephemeral=True)
        return

    # 2/3. Invites + active VPS limits
    required = bot.db.get_setting('required_invites', 5)
    row = bot.db.get_user_invites_row(user.id)
    valid = row['valid_invites'] if row else 0
    eligible, status_text = eligibility_status(valid, required)

    if not eligible:
        remaining = max(0, required - valid)
        await ctx.send(
            "❌ You cannot create a VPS yet.\n\n"
            f"Required Invites: {required}\n"
            f"Your Invites: {valid}\n"
            f"Remaining: {remaining}",
            ephemeral=True,
        )
        return

    max_per_user = bot.db.get_setting('max_vps_per_user', MAX_VPS_PER_USER)
    if bot.db.get_user_vps_count(user.id) >= max_per_user:
        await ctx.send(
            f"❌ You already have the maximum number of VPS instances ({max_per_user}).",
            ephemeral=True,
        )
        return

    # 4. Deployment enabled
    if not bot.db.get_setting('vps_enabled', 1):
        await ctx.send("❌ VPS deployment is currently disabled. Check back later.", ephemeral=True)
        return

    # 5. Cooldown
    cooldown_hours = bot.db.get_setting('vps_cooldown_hours', 24)
    if cooldown_hours:
        last = parse_created_at(bot.db.get_last_vps_created_at(user.id))
        if last:
            elapsed = (datetime.datetime.now() - last).total_seconds() / 3600.0
            if elapsed < cooldown_hours:
                remaining_h = cooldown_hours - elapsed
                hours = int(remaining_h)
                minutes = int((remaining_h - hours) * 60)
                await ctx.send(
                    f"⏳ You can create another VPS in {hours}h {minutes}m.",
                    ephemeral=True,
                )
                return

    # 6. Resources
    if not bot.docker_client:
        await ctx.send("❌ Deployment resources are unavailable. Please try again later.", ephemeral=True)
        return

    if not ctx.guild:
        await ctx.send("❌ This command can only be used in a server!", ephemeral=True)
        return

    plan_mem = memory if memory is not None else bot.db.get_setting('default_vps_memory', 2)
    plan_cpu = cpu if cpu is not None else bot.db.get_setting('default_vps_cpu', 1)
    plan_disk = disk if disk is not None else bot.db.get_setting('default_vps_disk', 20)

    status_msg = await ctx.send(
        f"🚀 Starting VPS Deployment...\n\n"
        f"[{progress_bar(5)}] 5%\n\n"
        f"✓ Invite requirement verified ({valid}/{required})\n"
        f"Please wait while your VPS is being created."
    )
    logger.info(f"/createvps invoked by {user.id} — eligibility verified")

    await edit_progress(status_msg, 10, "✓ Invite requirement verified\n✓ Eligibility confirmed")

    ok, vps_data, err = await provision_vps_core(
        ctx, user, plan_mem, plan_cpu, plan_disk,
        status_msg=status_msg,
    )

    if not ok and err:
        logger.warning(f"/createvps failed for {user.id}: {err}")
    else:
        logger.info(f"/createvps completed for {user.id}")


@bot.hybrid_command(name='list', description='List all your VPS instances')
async def list_vps(ctx):
    """List all VPS instances owned by the user"""
    try:
        user_vps = bot.db.get_user_vps(ctx.author.id)
        
        if not user_vps:
            await ctx.send("You don't have any VPS instances.", ephemeral=True)
            return

        embed = discord.Embed(title="Your LexoNodes VPS Instances", color=discord.Color.blue())
        
        for vps in user_vps:
            try:
                # Handle missing container ID gracefully
                container = bot.docker_client.containers.get(vps["container_id"]) if vps["container_id"] else None
                status = vps['status'].capitalize() if vps.get('status') else "Unknown"
            except Exception as e:
                status = "Not Found"
                logger.error(f"Error fetching container {vps['container_id']}: {e}")

            # Adding fields safely to prevent missing keys causing errors
            embed.add_field(
                name=f"VPS {vps['vps_id']}",
                value=f"""
Status: {status}
Memory: {vps.get('memory', 'Unknown')}GB
CPU: {vps.get('cpu', 'Unknown')} cores
Disk Allocated: {vps.get('disk', 'Unknown')}GB
Username: {vps.get('username', 'Unknown')}
OS: {vps.get('os_image', DEFAULT_OS_IMAGE)}
Created: {vps.get('created_at', 'Unknown')}
Restarts: {vps.get('restart_count', 0)}
""",
                inline=False
            )
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in list_vps: {e}")
        await ctx.send(f"❌ Error listing VPS instances: {str(e)}")

@bot.hybrid_command(name='vps_list', description='List all VPS instances (Admin only)')
async def admin_list_vps(ctx):
    """List all VPS instances (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        all_vps = bot.db.get_all_vps()
        if not all_vps:
            await ctx.send("No VPS instances found.", ephemeral=True)
            return

        embed = discord.Embed(title="All LexoNodes VPS Instances", color=discord.Color.blue())
        valid_vps_count = 0
        
        for token, vps in all_vps.items():
            try:
                # Fetch username of the owner with error handling
                user = await bot.fetch_user(int(vps.get("created_by", "0")))
                username = user.name if user else "Unknown User"
            except Exception as e:
                username = "Unknown User"
                logger.error(f"Error fetching user {vps.get('created_by')}: {e}")

            try:
                # Handle missing container ID gracefully
                container = bot.docker_client.containers.get(vps.get("container_id", "")) if vps.get("container_id") else None
                container_status = container.status if container else "Not Found"
            except Exception as e:
                container_status = "Not Found"
                logger.error(f"Error fetching container {vps.get('container_id')}: {e}")

            # Get status and other info with error fallback
            status = vps.get('status', "Unknown").capitalize()

            vps_info = f"""
Owner: {username}
Status: {status} (Container: {container_status})
Memory: {vps.get('memory', 'Unknown')}GB
CPU: {vps.get('cpu', 'Unknown')} cores
Disk: {vps.get('disk', 'Unknown')}GB
Username: {vps.get('username', 'Unknown')}
OS: {vps.get('os_image', DEFAULT_OS_IMAGE)}
Created: {vps.get('created_at', 'Unknown')}
Restarts: {vps.get('restart_count', 0)}
"""

            embed.add_field(
                name=f"VPS {vps.get('vps_id', 'Unknown')}",
                value=vps_info,
                inline=False
            )
            valid_vps_count += 1

        if valid_vps_count == 0:
            await ctx.send("No valid VPS instances found.", ephemeral=True)
            return

        embed.set_footer(text=f"Total VPS instances: {valid_vps_count}")
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in admin_list_vps: {e}")
        await ctx.send(f"❌ Error listing VPS instances: {str(e)}")

@bot.hybrid_command(name='delete_vps', description='Delete a VPS instance (Admin only)')
@app_commands.describe(
    vps_id="ID of the VPS to delete"
)
async def delete_vps(ctx, vps_id: str):
    """Delete a VPS instance (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps:
            await ctx.send("❌ VPS not found!", ephemeral=True)
            return
        
        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            container.stop()
            container.remove()
            logger.info(f"Deleted container {vps['container_id']} for VPS {vps_id}")
        except Exception as e:
            logger.error(f"Error removing container: {e}")
        
        bot.db.remove_vps(token)
        
        await ctx.send(f"✅ LexoNodes VPS {vps_id} has been deleted successfully!")
    except Exception as e:
        logger.error(f"Error in delete_vps: {e}")
        await ctx.send(f"❌ Error deleting VPS: {str(e)}")

@bot.hybrid_command(name='connect_vps', description='Connect to a VPS using the provided token')
@app_commands.describe(
    token="Access token for the VPS"
)
async def connect_vps(ctx, token: str):
    """Connect to a VPS using the provided token"""
    vps = bot.db.get_vps_by_token(token)
    if not vps:
        await ctx.send("❌ Invalid token!", ephemeral=True)
        return
        
    if str(ctx.author.id) != vps["created_by"] and not has_admin_role(ctx):
        await ctx.send("❌ You don't have permission to access this VPS!", ephemeral=True)
        return

    try:
        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            if container.status != "running":
                container.start()
                await asyncio.sleep(5)
        except:
            await ctx.send("❌ VPS instance not found or is no longer available.", ephemeral=True)
            return

        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", vps["container_id"], "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        if not ssh_session_line:
            raise Exception("Failed to get tmate session")

        bot.db.update_vps(token, {"tmate_session": ssh_session_line})
        
        embed = discord.Embed(title="LexoNodes VPS Connection Details", color=discord.Color.blue())
        embed.add_field(name="Username", value=vps["username"], inline=True)
        embed.add_field(name="SSH Password", value=f"||{vps.get('password', 'Not set')}||", inline=True)
        embed.add_field(name="Tmate Session", value=f"```{ssh_session_line}```", inline=False)
        embed.add_field(name="Connection Instructions", value="""
1. Copy the Tmate session command
2. Open your terminal
3. Paste and run the command
4. You will be connected to your LexoNodes VPS

Or use direct SSH:
```ssh {username}@<server-ip>```
""".format(username=vps["username"]), inline=False)
        
        await ctx.author.send(embed=embed)
        await ctx.send("✅ Connection details sent to your DMs! Use the Tmate command to connect to your LexoNodes VPS.", ephemeral=True)
        
    except discord.Forbidden:
        await ctx.send("❌ I couldn't send you a DM. Please enable DMs from server members.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in connect_vps: {e}")
        await ctx.send(f"❌ An error occurred while connecting to the VPS: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='vps_stats', description='Show resource usage for a VPS')
@app_commands.describe(
    vps_id="ID of the VPS to check"
)
async def vps_stats(ctx, vps_id: str):
    """Show resource usage for a VPS"""
    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps or (vps["created_by"] != str(ctx.author.id) and not has_admin_role(ctx)):
            await ctx.send("❌ VPS not found or you don't have access to it!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            if container.status != "running":
                await ctx.send("❌ VPS is not running!", ephemeral=True)
                return

            # Get memory stats
            mem_process = await asyncio.create_subprocess_exec(
                "docker", "exec", vps["container_id"], "free", "-m",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await mem_process.communicate()
            
            if mem_process.returncode != 0:
                raise Exception(f"Failed to get memory info: {stderr.decode()}")

            # Get CPU stats
            cpu_process = await asyncio.create_subprocess_exec(
                "docker", "exec", vps["container_id"], "top", "-bn1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            cpu_stdout, cpu_stderr = await cpu_process.communicate()

            # Get disk stats
            disk_process = await asyncio.create_subprocess_exec(
                "docker", "exec", vps["container_id"], "df", "-h",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            disk_stdout, disk_stderr = await disk_process.communicate()

            embed = discord.Embed(title=f"Resource Usage for VPS {vps_id}", color=discord.Color.blue())
            embed.add_field(name="Memory Info", value=f"```{stdout.decode()}```", inline=False)
            
            if disk_process.returncode == 0:
                embed.add_field(name="Disk Info", value=f"```{disk_stdout.decode()}```", inline=False)
            
            embed.add_field(name="Configured Limits", value=f"""
Memory: {vps['memory']}GB
CPU: {vps['cpu']} cores
Disk Allocated: {vps['disk']}GB
""", inline=True)
            
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Error checking VPS stats: {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in vps_stats: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='change_ssh_password', description='Change the SSH password for a VPS')
@app_commands.describe(
    vps_id="ID of the VPS to update"
)
async def change_ssh_password(ctx, vps_id: str):
    """Change the SSH password for a VPS"""
    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps or vps["created_by"] != str(ctx.author.id):
            await ctx.send("❌ VPS not found or you don't have access to it!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            if container.status != "running":
                await ctx.send("❌ VPS is not running!", ephemeral=True)
                return

            new_password = generate_ssh_password()
            
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", vps["container_id"], "bash", "-c", f"echo '{vps['username']}:{new_password}' | chpasswd",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                raise Exception(f"Failed to change password: {stderr.decode()}")

            bot.db.update_vps(token, {'password': new_password})
            
            embed = discord.Embed(title=f"SSH Password Updated for VPS {vps_id}", color=discord.Color.green())
            embed.add_field(name="Username", value=vps['username'], inline=True)
            embed.add_field(name="New Password", value=f"||{new_password}||", inline=False)
            
            await ctx.author.send(embed=embed)
            await ctx.send("✅ SSH password updated successfully! Check your DMs for the new password.", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Error changing SSH password: {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in change_ssh_password: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='admin_stats', description='Show system statistics (Admin only)')
async def admin_stats(ctx):
    """Show system statistics (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        # Get Docker stats
        containers = bot.docker_client.containers.list(all=True) if bot.docker_client else []
        
        # Get system stats
        stats = bot.system_stats
        
        embed = discord.Embed(title="LexoNodes System Statistics", color=discord.Color.blue())
        embed.add_field(name="VPS Instances", value=f"Total: {len(bot.db.get_all_vps())}\nRunning: {len([c for c in containers if c.status == 'running'])}", inline=True)
        embed.add_field(name="Docker Containers", value=f"Total: {len(containers)}\nRunning: {len([c for c in containers if c.status == 'running'])}", inline=True)
        embed.add_field(name="CPU Usage", value=f"{stats['cpu_usage']}%", inline=True)
        embed.add_field(name="Memory Usage", value=f"{stats['memory_usage']}% ({stats['memory_used']:.2f}GB / {stats['memory_total']:.2f}GB)", inline=True)
        embed.add_field(name="Disk Usage", value=f"{stats['disk_usage']}% ({stats['disk_used']:.2f}GB / {stats['disk_total']:.2f}GB)", inline=True)
        embed.add_field(name="Network", value=f"Sent: {stats['network_sent']:.2f}MB\nRecv: {stats['network_recv']:.2f}MB", inline=True)
        embed.add_field(name="Container Limit", value=f"{len(containers)}/{bot.db.get_setting('max_containers')}", inline=True)
        embed.add_field(name="Last Updated", value=f"<t:{int(stats['last_updated'])}:R>", inline=True)
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in admin_stats: {e}")
        await ctx.send(f"❌ Error getting system stats: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='system_info', description='Show detailed system information (Admin only)')
async def system_info(ctx):
    """Show detailed system information (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        # System information
        uname = platform.uname()
        boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
        
        # CPU information
        cpu_info = f"""
System: {uname.system}
Node Name: {uname.node}
Release: {uname.release}
Version: {uname.version}
Machine: {uname.machine}
Processor: {uname.processor}
Physical cores: {psutil.cpu_count(logical=False)}
Total cores: {psutil.cpu_count(logical=True)}
CPU Usage: {psutil.cpu_percent()}%
"""
        
        # Memory Information
        svmem = psutil.virtual_memory()
        mem_info = f"""
Total: {svmem.total / (1024**3):.2f}GB
Available: {svmem.available / (1024**3):.2f}GB
Used: {svmem.used / (1024**3):.2f}GB
Percentage: {svmem.percent}%
"""
        
        # Disk Information
        partitions = psutil.disk_partitions()
        disk_info = ""
        for partition in partitions:
            try:
                partition_usage = psutil.disk_usage(partition.mountpoint)
                disk_info += f"""
Device: {partition.device}
  Mountpoint: {partition.mountpoint}
  File system type: {partition.fstype}
  Total Size: {partition_usage.total / (1024**3):.2f}GB
  Used: {partition_usage.used / (1024**3):.2f}GB
  Free: {partition_usage.free / (1024**3):.2f}GB
  Percentage: {partition_usage.percent}%
"""
            except PermissionError:
                continue
        
        # Network information
        net_io = psutil.net_io_counters()
        net_info = f"""
Bytes Sent: {net_io.bytes_sent / (1024**2):.2f}MB
Bytes Received: {net_io.bytes_recv / (1024**2):.2f}MB
"""
        
        embed = discord.Embed(title="Detailed System Information", color=discord.Color.blue())
        embed.add_field(name="System", value=f"Boot Time: {boot_time}", inline=False)
        embed.add_field(name="CPU Info", value=f"```{cpu_info}```", inline=False)
        embed.add_field(name="Memory Info", value=f"```{mem_info}```", inline=False)
        embed.add_field(name="Disk Info", value=f"```{disk_info}```", inline=False)
        embed.add_field(name="Network Info", value=f"```{net_info}```", inline=False)
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in system_info: {e}")
        await ctx.send(f"❌ Error getting system info: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='container_limit', description='Set maximum container limit (Owner only)')
@app_commands.describe(
    max_limit="New maximum container limit"
)
async def set_container_limit(ctx, max_limit: int):
    """Set maximum container limit (Owner only)"""
    if ctx.author.id != 1210291131301101618:  # Only the owner can set limit
        await ctx.send("❌ Only the owner can set container limit!", ephemeral=True)
        return
    
    if max_limit < 1 or max_limit > 1000:
        await ctx.send("❌ Container limit must be between 1 and 1000", ephemeral=True)
        return
    
    bot.db.set_setting('max_containers', max_limit)
    await ctx.send(f"✅ Maximum container limit set to {max_limit}", ephemeral=True)

@bot.hybrid_command(name='cleanup_vps', description='Cleanup inactive VPS instances (Admin only)')
async def cleanup_vps(ctx):
    """Cleanup inactive VPS instances (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        cleanup_count = 0
        
        for token, vps in list(bot.db.get_all_vps().items()):
            try:
                container = bot.docker_client.containers.get(vps['container_id'])
                if container.status != 'running':
                    container.stop()
                    container.remove()
                    bot.db.remove_vps(token)
                    cleanup_count += 1
            except docker.errors.NotFound:
                bot.db.remove_vps(token)
                cleanup_count += 1
            except Exception as e:
                logger.error(f"Error cleaning up VPS {vps['vps_id']}: {e}")
                continue
        
        if cleanup_count > 0:
            await ctx.send(f"✅ Cleaned up {cleanup_count} inactive VPS instances!")
        else:
            await ctx.send("ℹ️ No inactive VPS instances found to clean up.")
    except Exception as e:
        logger.error(f"Error in cleanup_vps: {e}")
        await ctx.send(f"❌ Error during cleanup: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='vps_shell', description='Get shell access to your VPS')
@app_commands.describe(
    vps_id="ID of the VPS to access"
)
async def vps_shell(ctx, vps_id: str):
    """Get shell access to your VPS"""
    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps or (vps["created_by"] != str(ctx.author.id) and not has_admin_role(ctx)):
            await ctx.send("❌ VPS not found or you don't have access to it!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            if container.status != "running":
                await ctx.send("❌ VPS is not running!", ephemeral=True)
                return

            await ctx.send(f"✅ Shell access to VPS {vps_id}:\n"
                          f"```docker exec -it {vps['container_id']} bash```\n"
                          f"Username: {vps['username']}\n"
                          f"Password: ||{vps.get('password', 'Not set')}||", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Error accessing VPS shell: {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in vps_shell: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='vps_console', description='Get direct console access to your VPS')
@app_commands.describe(
    vps_id="ID of the VPS to access"
)
async def vps_console(ctx, vps_id: str):
    """Get direct console access to your VPS"""
    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps or (vps["created_by"] != str(ctx.author.id) and not has_admin_role(ctx)):
            await ctx.send("❌ VPS not found or you don't have access to it!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            if container.status != "running":
                await ctx.send("❌ VPS is not running!", ephemeral=True)
                return

            await ctx.send(f"✅ Console access to VPS {vps_id}:\n"
                          f"```docker attach {vps['container_id']}```\n"
                          f"Note: To detach from the console without stopping the container, use Ctrl+P followed by Ctrl+Q", 
                          ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Error accessing VPS console: {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in vps_console: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='vps_usage', description='Show your VPS usage statistics')
async def vps_usage(ctx):
    """Show your VPS usage statistics"""
    try:
        user_vps = bot.db.get_user_vps(ctx.author.id)
        
        total_memory = sum(vps['memory'] for vps in user_vps)
        total_cpu = sum(vps['cpu'] for vps in user_vps)
        total_disk = sum(vps['disk'] for vps in user_vps)
        total_restarts = sum(vps.get('restart_count', 0) for vps in user_vps)
        
        embed = discord.Embed(title="Your LexoNodes VPS Usage", color=discord.Color.blue())
        embed.add_field(name="Total VPS Instances", value=len(user_vps), inline=True)
        embed.add_field(name="Total Memory Allocated", value=f"{total_memory}GB", inline=True)
        embed.add_field(name="Total CPU Cores Allocated", value=total_cpu, inline=True)
        embed.add_field(name="Total Disk Allocated", value=f"{total_disk}GB", inline=True)
        embed.add_field(name="Total Restarts", value=total_restarts, inline=True)
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in vps_usage: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='global_stats', description='Show global usage statistics (Admin only)')
async def global_stats(ctx):
    """Show global usage statistics (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        all_vps = bot.db.get_all_vps()
        total_memory = sum(vps['memory'] for vps in all_vps.values())
        total_cpu = sum(vps['cpu'] for vps in all_vps.values())
        total_disk = sum(vps['disk'] for vps in all_vps.values())
        total_restarts = sum(vps.get('restart_count', 0) for vps in all_vps.values())
        
        embed = discord.Embed(title="LexoNodes Global Usage Statistics", color=discord.Color.blue())
        embed.add_field(name="Total VPS Created", value=bot.db.get_stat('total_vps_created'), inline=True)
        embed.add_field(name="Total Restarts", value=bot.db.get_stat('total_restarts'), inline=True)
        embed.add_field(name="Current VPS Instances", value=len(all_vps), inline=True)
        embed.add_field(name="Total Memory Allocated", value=f"{total_memory}GB", inline=True)
        embed.add_field(name="Total CPU Cores Allocated", value=total_cpu, inline=True)
        embed.add_field(name="Total Disk Allocated", value=f"{total_disk}GB", inline=True)
        embed.add_field(name="Total Restarts", value=total_restarts, inline=True)
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in global_stats: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='migrate_vps', description='Migrate a VPS to another host (Admin only)')
@app_commands.describe(
    vps_id="ID of the VPS to migrate"
)
async def migrate_vps(ctx, vps_id: str):
    """Migrate a VPS to another host (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps:
            await ctx.send("❌ VPS not found!", ephemeral=True)
            return

        status_msg = await ctx.send(f"🔄 Preparing to migrate VPS {vps_id}...")
        
        # Create a snapshot
        backup_id = generate_vps_id()[:8]
        backup_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        backup_dir = f"migrations/{vps_id}"
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = f"{backup_dir}/{backup_id}.tar"
        
        await status_msg.edit(content=f"🔄 Creating snapshot {backup_id} for migration...")
        
        process = await asyncio.create_subprocess_exec(
            "docker", "export", "-o", backup_file, vps["container_id"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            raise Exception(f"Snapshot failed: {stderr.decode()}")
        
        await status_msg.edit(content=f"✅ Snapshot {backup_id} created successfully. Please download this file and import it on the new host: {backup_file}")
        
    except Exception as e:
        logger.error(f"Error in migrate_vps: {e}")
        await ctx.send(f"❌ Error during migration: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='emergency_stop', description='Force stop a problematic VPS (Admin only)')
@app_commands.describe(
    vps_id="ID of the VPS to stop"
)
async def emergency_stop(ctx, vps_id: str):
    """Force stop a problematic VPS (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps:
            await ctx.send("❌ VPS not found!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            if container.status != "running":
                await ctx.send("VPS is already stopped!", ephemeral=True)
                return
            
            await ctx.send("⚠️ Attempting to force stop the VPS... This may take a moment.", ephemeral=True)
            
            # Try normal stop first
            try:
                container.stop(timeout=10)
                bot.db.update_vps(token, {'status': 'stopped'})
                await ctx.send("✅ VPS stopped successfully!", ephemeral=True)
                return
            except:
                pass
            
            # If normal stop failed, try killing the container
            try:
                subprocess.run(["docker", "kill", vps["container_id"]], check=True)
                bot.db.update_vps(token, {'status': 'stopped'})
                await ctx.send("✅ VPS killed forcefully!", ephemeral=True)
            except subprocess.CalledProcessError as e:
                raise Exception(f"Failed to kill container: {e}")
            
        except Exception as e:
            await ctx.send(f"❌ Error stopping VPS: {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in emergency_stop: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='emergency_remove', description='Force remove a problematic VPS (Admin only)')
@app_commands.describe(
    vps_id="ID of the VPS to remove"
)
async def emergency_remove(ctx, vps_id: str):
    """Force remove a problematic VPS (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps:
            await ctx.send("❌ VPS not found!", ephemeral=True)
            return

        try:
            # First try to stop the container normally
            try:
                container = bot.docker_client.containers.get(vps["container_id"])
                container.stop()
            except:
                pass
            
            # Then try to remove it forcefully
            try:
                subprocess.run(["docker", "rm", "-f", vps["container_id"]], check=True)
            except subprocess.CalledProcessError as e:
                raise Exception(f"Failed to remove container: {e}")
            
            # Remove from data
            bot.db.remove_vps(token)
            
            await ctx.send("✅ VPS removed forcefully!", ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Error removing VPS: {str(e)}", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in emergency_remove: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='suspend_vps', description='Suspend a VPS (Admin only)')
@app_commands.describe(
    vps_id="ID of the VPS to suspend"
)
async def suspend_vps(ctx, vps_id: str):
    """Suspend a VPS (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps:
            await ctx.send("❌ VPS not found!", ephemeral=True)
            return

        if vps['status'] == 'suspended':
            await ctx.send("❌ VPS is already suspended!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            container.stop()
        except Exception as e:
            logger.error(f"Error stopping container for suspend: {e}")

        bot.db.update_vps(token, {'status': 'suspended'})
        await ctx.send(f"✅ VPS {vps_id} has been suspended!")

        # Notify owner
        try:
            owner = await bot.fetch_user(int(vps['created_by']))
            await owner.send(f"⚠️ Your VPS {vps_id} has been suspended by an admin. Contact support for details.")
        except:
            pass

    except Exception as e:
        logger.error(f"Error in suspend_vps: {e}")
        await ctx.send(f"❌ Error suspending VPS: {str(e)}")

@bot.hybrid_command(name='unsuspend_vps', description='Unsuspend a VPS (Admin only)')
@app_commands.describe(
    vps_id="ID of the VPS to unsuspend"
)
async def unsuspend_vps(ctx, vps_id: str):
    """Unsuspend a VPS (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps:
            await ctx.send("❌ VPS not found!", ephemeral=True)
            return

        if vps['status'] != 'suspended':
            await ctx.send("❌ VPS is not suspended!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            container.start()
        except Exception as e:
            logger.error(f"Error starting container for unsuspend: {e}")
            await ctx.send(f"❌ Error starting container: {str(e)}")
            return

        bot.db.update_vps(token, {'status': 'running'})
        await ctx.send(f"✅ VPS {vps_id} has been unsuspended!")

        # Notify owner
        try:
            owner = await bot.fetch_user(int(vps['created_by']))
            await owner.send(f"✅ Your VPS {vps_id} has been unsuspended by an admin.")
        except:
            pass

    except Exception as e:
        logger.error(f"Error in unsuspend_vps: {e}")
        await ctx.send(f"❌ Error unsuspending VPS: {str(e)}")

@bot.hybrid_command(name='edit_vps', description='Edit VPS specifications (Admin only)')
@app_commands.describe(
    vps_id="ID of the VPS to edit",
    memory="New memory in GB (optional)",
    cpu="New CPU cores (optional)",
    disk="New disk space in GB (optional)"
)
async def edit_vps(ctx, vps_id: str, memory: Optional[int] = None, cpu: Optional[int] = None, disk: Optional[int] = None):
    """Edit VPS specifications (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    if memory is None and cpu is None and disk is None:
        await ctx.send("❌ At least one specification to edit must be provided!", ephemeral=True)
        return

    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps:
            await ctx.send("❌ VPS not found!", ephemeral=True)
            return

        updates = {}
        if memory is not None:
            if memory < 1 or memory > 512:
                await ctx.send("❌ Memory must be between 1GB and 512GB", ephemeral=True)
                return
            updates['memory'] = memory
        if cpu is not None:
            if cpu < 1 or cpu > 32:
                await ctx.send("❌ CPU cores must be between 1 and 32", ephemeral=True)
                return
            updates['cpu'] = cpu
        if disk is not None:
            if disk < 10 or disk > 1000:
                await ctx.send("❌ Disk space must be between 10GB and 1000GB", ephemeral=True)
                return
            updates['disk'] = disk

        # Restart container with new limits
        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            container.stop()
            container.remove()

            memory_bytes = (memory or vps['memory']) * 1024 * 1024 * 1024
            cpu_quota = int((cpu or vps['cpu']) * 100000)

            new_container = bot.docker_client.containers.run(
                vps['os_image'],
                detach=True,
                privileged=True,
                hostname=f"lexonodes-{vps_id}",
                mem_limit=memory_bytes,
                cpu_period=100000,
                cpu_quota=cpu_quota,
                cap_add=["ALL"],
                command="tail -f /dev/null",
                tty=True,
                network=DOCKER_NETWORK,
                volumes={
                    f'lexonodes-{vps_id}': {'bind': '/data', 'mode': 'rw'}
                },
                restart_policy={"Name": "always"}
            )

            updates['container_id'] = new_container.id
            await asyncio.sleep(5)
            setup_success, _, _ = await setup_container(
                new_container.id, 
                ctx, 
                memory or vps['memory'], 
                vps['username'], 
                vps_id=vps_id,
                use_custom_image=vps['use_custom_image']
            )
            if not setup_success:
                raise Exception("Failed to setup new container")
        except Exception as e:
            await ctx.send(f"❌ Error updating container: {str(e)}")
            return

        bot.db.update_vps(token, updates)
        await ctx.send(f"✅ VPS {vps_id} specifications updated successfully!")

    except Exception as e:
        logger.error(f"Error in edit_vps: {e}")
        await ctx.send(f"❌ Error editing VPS: {str(e)}")

@bot.hybrid_command(name='ban_user', description='Ban a user from creating VPS (Admin only)')
@app_commands.describe(
    user="User to ban"
)
async def ban_user(ctx, user: discord.User):
    """Ban a user from creating VPS (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    bot.db.ban_user(user.id)
    await ctx.send(f"✅ {user.mention} has been banned from creating VPS!")

@bot.hybrid_command(name='unban_user', description='Unban a user (Admin only)')
@app_commands.describe(
    user="User to unban"
)
async def unban_user(ctx, user: discord.User):
    """Unban a user (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    bot.db.unban_user(user.id)
    await ctx.send(f"✅ {user.mention} has been unbanned!")

@bot.hybrid_command(name='list_banned', description='List banned users (Admin only)')
async def list_banned(ctx):
    """List banned users (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    banned = bot.db.get_banned_users()
    if not banned:
        await ctx.send("No banned users.", ephemeral=True)
        return

    embed = discord.Embed(title="Banned Users", color=discord.Color.red())
    banned_list = []
    for user_id in banned:
        try:
            user = await bot.fetch_user(int(user_id))
            banned_list.append(f"{user.name} ({user_id})")
        except:
            banned_list.append(f"Unknown ({user_id})")
    embed.description = "\n".join(banned_list)
    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name='backup_data', description='Backup all bot data (Admin only)')
async def backup_data(ctx):
    """Backup all bot data (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        if bot.db.backup_data():
            await ctx.send("✅ Data backup completed successfully!", ephemeral=True)
        else:
            await ctx.send("❌ Failed to backup data!", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in backup_data: {e}")
        await ctx.send(f"❌ Error backing up data: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='restore_data', description='Restore from backup (Admin only)')
async def restore_data(ctx):
    """Restore from backup (Admin only)"""
    if not has_admin_role(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return

    try:
        if bot.db.restore_data():
            await ctx.send("✅ Data restore completed successfully!", ephemeral=True)
        else:
            await ctx.send("❌ Failed to restore data!", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in restore_data: {e}")
        await ctx.send(f"❌ Error restoring data: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='reinstall_bot', description='Reinstall the bot (Owner only)')
async def reinstall_bot(ctx):
    """Reinstall the bot (Owner only)"""
    if ctx.author.id != 1210291131301101618:  # Only the owner can reinstall
        await ctx.send("❌ Only the owner can reinstall the bot!", ephemeral=True)
        return

    try:
        await ctx.send("🔄 Reinstalling LexoNodes bot... This may take a few minutes.")
        
        # Create Dockerfile for bot reinstallation
        dockerfile_content = f"""
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY . .

# Start the bot
CMD ["python", "bot.py"]
"""
        
        with open("Dockerfile.bot", "w") as f:
            f.write(dockerfile_content)
        
        # Build and run the bot in a container
        process = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", "lexonodes-bot", "-f", "Dockerfile.bot", ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            raise Exception(f"Failed to build bot image: {stderr.decode()}")
        
        await ctx.send("✅ Bot reinstalled successfully! Restarting...")
        
        # Restart the bot
        os._exit(0)
        
    except Exception as e:
        logger.error(f"Error in reinstall_bot: {e}")
        await ctx.send(f"❌ Error reinstalling bot: {str(e)}", ephemeral=True)

class VPSManagementView(ui.View):
    def __init__(self, vps_id, container_id):
        super().__init__(timeout=300)
        self.vps_id = vps_id
        self.container_id = container_id
        self.original_message = None

    async def handle_missing_container(self, interaction: discord.Interaction):
        token, _ = bot.db.get_vps_by_id(self.vps_id)
        if token:
            bot.db.remove_vps(token)
        
        embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.red())
        embed.add_field(name="Status", value="🔴 Container Not Found", inline=True)
        embed.add_field(name="Note", value="This VPS instance is no longer available. Please create a new one.", inline=False)
        
        for item in self.children:
            item.disabled = True
        
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("❌ This VPS instance is no longer available. Please create a new one.", ephemeral=True)

    @discord.ui.button(label="Start VPS", style=discord.ButtonStyle.green)
    async def start_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if vps['status'] == 'suspended':
                await interaction.followup.send("❌ This VPS is suspended. Contact admin to unsuspend.", ephemeral=True)
                return

            if container.status == "running":
                await interaction.followup.send("VPS is already running!", ephemeral=True)
                return
            
            container.start()
            await asyncio.sleep(5)
            
            if token:
                bot.db.update_vps(token, {'status': 'running'})
            
            embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.green())
            embed.add_field(name="Status", value="🟢 Running", inline=True)
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
            
            await interaction.message.edit(embed=embed)
            await interaction.followup.send("✅ LexoNodes VPS started successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error starting VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Stop VPS", style=discord.ButtonStyle.red)
    async def stop_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            if container.status != "running":
                await interaction.followup.send("VPS is already stopped!", ephemeral=True)
                return
            
            container.stop()
            
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if token:
                bot.db.update_vps(token, {'status': 'stopped'})
            
            embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.orange())
            embed.add_field(name="Status", value="🔴 Stopped", inline=True)
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
            
            await interaction.message.edit(embed=embed)
            await interaction.followup.send("✅ LexoNodes VPS stopped successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error stopping VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Restart VPS", style=discord.ButtonStyle.blurple)
    async def restart_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if vps['status'] == 'suspended':
                await interaction.followup.send("❌ This VPS is suspended. Contact admin to unsuspend.", ephemeral=True)
                return

            container.restart()
            await asyncio.sleep(5)
            
            # Update restart count in VPS data
            if token:
                updates = {
                    'restart_count': vps.get('restart_count', 0) + 1,
                    'last_restart': str(datetime.datetime.now()),
                    'status': 'running'
                }
                bot.db.update_vps(token, updates)
                
                bot.db.increment_stat('total_restarts')
                
                # Get new SSH session
                try:
                    exec_cmd = await asyncio.create_subprocess_exec(
                        "docker", "exec", self.container_id, "tmate", "-F",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )

                    ssh_session_line = await capture_ssh_session_line(exec_cmd)
                    if ssh_session_line:
                        bot.db.update_vps(token, {'tmate_session': ssh_session_line})
                        
                        # Send new SSH details to owner
                        try:
                            owner = await bot.fetch_user(int(vps["created_by"]))
                            embed = discord.Embed(title=f"LexoNodes VPS Restarted - {self.vps_id}", color=discord.Color.blue())
                            embed.add_field(name="New SSH Session", value=f"```{ssh_session_line}```", inline=False)
                            await owner.send(embed=embed)
                        except:
                            pass
                except:
                    pass
            
            embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.green())
            embed.add_field(name="Status", value="🟢 Running", inline=True)
            
            if vps:
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
                embed.add_field(name="Restart Count", value=vps.get('restart_count', 0) + 1, inline=True)
            
            await interaction.message.edit(embed=embed, view=VPSManagementView(self.vps_id, container.id))
            await interaction.followup.send("✅ LexoNodes VPS restarted successfully! New SSH details sent to owner.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error restarting VPS: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Reinstall OS", style=discord.ButtonStyle.grey)
    async def reinstall_os(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            try:
                container = bot.docker_client.containers.get(self.container_id)
            except docker.errors.NotFound:
                await self.handle_missing_container(interaction)
                return
            
            view = OSSelectionView(self.vps_id, self.container_id, interaction.message)
            await interaction.response.send_message("Select new OS:", view=view, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Transfer VPS", style=discord.ButtonStyle.grey)
    async def transfer_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = TransferVPSModal(self.vps_id)
        await interaction.response.send_modal(modal)

class OSSelectionView(ui.View):
    def __init__(self, vps_id, container_id, original_message):
        super().__init__(timeout=300)
        self.vps_id = vps_id
        self.container_id = container_id
        self.original_message = original_message
        
        self.add_os_button("Ubuntu 22.04", "arindamvm/unvm")
        self.add_os_button("Debian 12", "debian:12")
        self.add_os_button("Arch Linux", "archlinux:latest")
        self.add_os_button("Alpine", "alpine:latest")
        self.add_os_button("CentOS 7", "centos:7")
        self.add_os_button("Fedora 38", "fedora:38")

    def add_os_button(self, label: str, image: str):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.grey)
        
        async def os_callback(interaction: discord.Interaction):
            await self.reinstall_os(interaction, image)
        
        button.callback = os_callback
        self.add_item(button)

    async def reinstall_os(self, interaction: discord.Interaction, image: str):
        try:
            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if not vps:
                await interaction.response.send_message("❌ VPS not found!", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            try:
                old_container = bot.docker_client.containers.get(self.container_id)
                old_container.stop()
                old_container.remove()
            except Exception as e:
                logger.error(f"Error removing old container: {e}")

            status_msg = await interaction.followup.send("🔄 Reinstalling LexoNodes VPS... This may take a few minutes.", ephemeral=True)
            
            memory_bytes = vps['memory'] * 1024 * 1024 * 1024

            try:
                container = bot.docker_client.containers.run(
                    image,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{self.vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(vps['cpu'] * 100000),
                    cap_add=["ALL"],
                    command="tail -f /dev/null",
                    tty=True,
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{self.vps_id}': {'bind': '/data', 'mode': 'rw'}
                    }
                )
            except docker.errors.ImageNotFound:
                await status_msg.edit(content=f"❌ OS image {image} not found. Using default {DEFAULT_OS_IMAGE}")
                container = bot.docker_client.containers.run(
                    DEFAULT_OS_IMAGE,
                    detach=True,
                    privileged=True,
                    hostname=f"lexonodes-{self.vps_id}",
                    mem_limit=memory_bytes,
                    cpu_period=100000,
                    cpu_quota=int(vps['cpu'] * 100000),
                    cap_add=["ALL"],
                    command="tail -f /dev/null",
                    tty=True,
                    network=DOCKER_NETWORK,
                    volumes={
                        f'lexonodes-{self.vps_id}': {'bind': '/data', 'mode': 'rw'}
                    }
                )
                image = DEFAULT_OS_IMAGE

            bot.db.update_vps(token, {
                'container_id': container.id,
                'os_image': image
            })

            try:
                setup_success, ssh_password, _ = await setup_container(
                    container.id, 
                    status_msg, 
                    vps['memory'], 
                    vps['username'], 
                    vps_id=self.vps_id
                )
                if not setup_success:
                    raise Exception("Failed to setup container")
                
                bot.db.update_vps(token, {'password': ssh_password})
            except Exception as e:
                await status_msg.edit(content=f"❌ Container setup failed: {str(e)}")
                return

            try:
                exec_cmd = await asyncio.create_subprocess_exec(
                    "docker", "exec", container.id, "tmate", "-F",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                ssh_session_line = await capture_ssh_session_line(exec_cmd)
                if ssh_session_line:
                    bot.db.update_vps(token, {'tmate_session': ssh_session_line})
                    
                    # Send new SSH details to owner
                    try:
                        owner = await bot.fetch_user(int(vps["created_by"]))
                        embed = discord.Embed(title=f"LexoNodes VPS Reinstalled - {self.vps_id}", color=discord.Color.blue())
                        embed.add_field(name="New OS", value=image, inline=True)
                        embed.add_field(name="New SSH Session", value=f"```{ssh_session_line}```", inline=False)
                        embed.add_field(name="New SSH Password", value=f"||{ssh_password}||", inline=False)
                        await owner.send(embed=embed)
                    except:
                        pass
            except Exception as e:
                logger.error(f"Warning: Failed to start tmate session: {e}")

            await status_msg.edit(content="✅ LexoNodes VPS reinstalled successfully!")
            
            try:
                embed = discord.Embed(title=f"LexoNodes VPS Management - {self.vps_id}", color=discord.Color.green())
                embed.add_field(name="Status", value="🟢 Running", inline=True)
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Created", value=vps['created_at'], inline=True)
                embed.add_field(name="OS", value=image, inline=True)
                
                await self.original_message.edit(embed=embed, view=VPSManagementView(self.vps_id, container.id))
            except Exception as e:
                logger.error(f"Warning: Failed to update original message: {e}")

        except Exception as e:
            try:
                await interaction.followup.send(f"❌ Error reinstalling VPS: {str(e)}", ephemeral=True)
            except:
                try:
                    channel = interaction.channel
                    await channel.send(f"❌ Error reinstalling LexoNodes VPS {self.vps_id}: {str(e)}")
                except:
                    logger.error(f"Failed to send error message: {e}")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.original_message.edit(view=self)
        except:
            pass

class TransferVPSModal(ui.Modal, title='Transfer VPS'):
    def __init__(self, vps_id: str):
        super().__init__()
        self.vps_id = vps_id
        self.new_owner = ui.TextInput(
            label='New Owner',
            placeholder='Enter user ID or @mention',
            required=True
        )
        self.add_item(self.new_owner)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_owner_input = self.new_owner.value.strip()
            
            # Extract user ID from mention if provided
            if new_owner_input.startswith('<@') and new_owner_input.endswith('>'):
                new_owner_id = new_owner_input[2:-1]
                if new_owner_id.startswith('!'):  # Handle nickname mentions
                    new_owner_id = new_owner_id[1:]
            else:
                # Validate it's a numeric ID
                if not new_owner_input.isdigit():
                    await interaction.response.send_message("❌ Please provide a valid user ID or @mention", ephemeral=True)
                    return
                new_owner_id = new_owner_input

            token, vps = bot.db.get_vps_by_id(self.vps_id)
            if not vps or vps["created_by"] != str(interaction.user.id):
                await interaction.response.send_message("❌ VPS not found or you don't have permission to transfer it!", ephemeral=True)
                return

            try:
                old_owner = await bot.fetch_user(int(vps["created_by"]))
                old_owner_name = old_owner.name
            except:
                old_owner_name = "Unknown User"

            try:
                new_owner = await bot.fetch_user(int(new_owner_id))
                new_owner_name = new_owner.name
                
                # Check if new owner is banned
                if bot.db.is_user_banned(new_owner.id):
                    await interaction.response.send_message(f"❌ {new_owner.mention} is banned!", ephemeral=True)
                    return

                # Check if new owner already has max VPS
                if bot.db.get_user_vps_count(new_owner.id) >= bot.db.get_setting('max_vps_per_user'):
                    await interaction.response.send_message(f"❌ {new_owner.mention} already has the maximum number of VPS instances ({bot.db.get_setting('max_vps_per_user')})", ephemeral=True)
                    return
            except:
                await interaction.response.send_message("❌ Invalid user ID or mention!", ephemeral=True)
                return

            bot.db.update_vps(token, {"created_by": str(new_owner.id)})

            await interaction.response.send_message(f"✅ LexoNodes VPS {self.vps_id} has been transferred from {old_owner_name} to {new_owner_name}!", ephemeral=True)
            
            try:
                embed = discord.Embed(title="LexoNodes VPS Transferred to You", color=discord.Color.green())
                embed.add_field(name="VPS ID", value=self.vps_id, inline=True)
                embed.add_field(name="Previous Owner", value=old_owner_name, inline=True)
                embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
                embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
                embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
                embed.add_field(name="Username", value=vps['username'], inline=True)
                embed.add_field(name="Access Token", value=token, inline=False)
                embed.add_field(name="SSH Password", value=f"||{vps.get('password', 'Not set')}||", inline=False)
                await new_owner.send(embed=embed)
            except:
                await interaction.followup.send("Note: Could not send DM to the new owner.", ephemeral=True)

        except Exception as e:
            logger.error(f"Error in TransferVPSModal: {e}")
            await interaction.response.send_message(f"❌ Error transferring VPS: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='manage_vps', description='Manage a VPS instance')
@app_commands.describe(
    vps_id="ID of the VPS to manage"
)
async def manage_vps(ctx, vps_id: str):
    """Manage a VPS instance"""
    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps or (vps["created_by"] != str(ctx.author.id) and not has_admin_role(ctx)):
            await ctx.send("❌ VPS not found or you don't have access to it!", ephemeral=True)
            return

        try:
            container = bot.docker_client.containers.get(vps["container_id"])
            container_status = container.status.capitalize()
        except:
            container_status = "Not Found"

        status = vps['status'].capitalize()

        embed = discord.Embed(title=f"LexoNodes VPS Management - {vps_id}", color=discord.Color.blue())
        embed.add_field(name="Status", value=f"{status} (Container: {container_status})", inline=True)
        embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
        embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
        embed.add_field(name="Disk Allocated", value=f"{vps['disk']}GB", inline=True)
        embed.add_field(name="Username", value=vps['username'], inline=True)
        embed.add_field(name="Created", value=vps['created_at'], inline=True)
        embed.add_field(name="OS", value=vps.get('os_image', DEFAULT_OS_IMAGE), inline=True)
        embed.add_field(name="Restart Count", value=vps.get('restart_count', 0), inline=True)

        view = VPSManagementView(vps_id, vps["container_id"])
        
        message = await ctx.send(embed=embed, view=view)
        view.original_message = message
    except Exception as e:
        logger.error(f"Error in manage_vps: {e}")
        await ctx.send(f"❌ Error managing VPS: {str(e)}", ephemeral=True)

@bot.hybrid_command(name='transfer_vps', description='Transfer a VPS to another user')
@app_commands.describe(
    vps_id="ID of the VPS to transfer",
    new_owner="User to transfer the VPS to"
)
async def transfer_vps_command(ctx, vps_id: str, new_owner: discord.Member):
    """Transfer a VPS to another user"""
    try:
        token, vps = bot.db.get_vps_by_id(vps_id)
        if not vps or vps["created_by"] != str(ctx.author.id):
            await ctx.send("❌ VPS not found or you don't have permission to transfer it!", ephemeral=True)
            return

        if bot.db.is_user_banned(new_owner.id):
            await ctx.send("❌ This user is banned!", ephemeral=True)
            return

        # Check if new owner already has max VPS
        if bot.db.get_user_vps_count(new_owner.id) >= bot.db.get_setting('max_vps_per_user'):
            await ctx.send(f"❌ {new_owner.mention} already has the maximum number of VPS instances ({bot.db.get_setting('max_vps_per_user')})", ephemeral=True)
            return

        bot.db.update_vps(token, {"created_by": str(new_owner.id)})

        await ctx.send(f"✅ LexoNodes VPS {vps_id} has been transferred from {ctx.author.name} to {new_owner.name}!")

        try:
            embed = discord.Embed(title="LexoNodes VPS Transferred to You", color=discord.Color.green())
            embed.add_field(name="VPS ID", value=vps_id, inline=True)
            embed.add_field(name="Previous Owner", value=ctx.author.name, inline=True)
            embed.add_field(name="Memory", value=f"{vps['memory']}GB", inline=True)
            embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
            embed.add_field(name="Disk", value=f"{vps['disk']}GB", inline=True)
            embed.add_field(name="Username", value=vps['username'], inline=True)
            embed.add_field(name="Access Token", value=token, inline=False)
            embed.add_field(name="SSH Password", value=f"||{vps.get('password', 'Not set')}||", inline=False)
            await new_owner.send(embed=embed)
        except:
            await ctx.send("Note: Could not send DM to the new owner.", ephemeral=True)

    except Exception as e:
        logger.error(f"Error in transfer_vps_command: {e}")
        await ctx.send(f"❌ Error transferring VPS: {str(e)}", ephemeral=True)

# ======================================================================
# User commands: invites / leaderboard / vps / refresh-motd
# ======================================================================

@bot.hybrid_command(name='invites', description='Show your invite progress')
async def invites_command(ctx):
    """Display invite counts and VPS eligibility for the caller."""
    try:
        required = bot.db.get_setting('required_invites', 5)
        row = bot.db.get_user_invites_row(ctx.author.id)
        valid = row['valid_invites'] if row else 0
        fake = row['fake_invites'] if row else 0
        eligible, status_text = eligibility_status(valid, required)
        bot.db.set_eligible(ctx.author.id, 1 if eligible else 0)
        access = "✅ Enabled" if eligible and not bot.db.is_user_banned(ctx.author.id) else "❌ Disabled"

        embed = discord.Embed(title="Your Invites", color=discord.Color.blurple())
        embed.description = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Valid Invites: {valid}\n"
            f"Required: {required}\n\n"
            f"Status: {status_text}\n"
            f"VPS Access: {access}"
        )
        if fake:
            embed.set_footer(text=f"Invalid/fake invites: {fake}")
        await ctx.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in invites_command: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)


@bot.hybrid_command(name='leaderboard', description='Show top inviters')
async def leaderboard_command(ctx):
    """Display the invite leaderboard."""
    try:
        rows = bot.db.get_leaderboard(5)
        if not rows:
            await ctx.send("No invites recorded yet.", ephemeral=True)
            return
        lines = []
        medals = ['🥇', '🥈', '🥉', '4.', '5.']
        for i, r in enumerate(rows):
            mention = f"<@{r['user_id']}>"
            lines.append(f"{medals[i] if i < len(medals) else f'{i+1}.'} {mention} — {r['valid_invites']} invites")
        embed = discord.Embed(
            title="🏆 Invite Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in leaderboard_command: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)


def _container_ip(container):
    try:
        container.reload()
        nets = (container.attrs.get('NetworkSettings') or {})
        ip = nets.get('IPAddress') or ''
        if not ip:
            for net in (nets.get('Networks') or {}).values():
                if net.get('IPAddress'):
                    ip = net['IPAddress']
                    break
        return ip or 'N/A'
    except Exception:
        return 'N/A'


@bot.hybrid_command(name='vps', description='Show your VPS information')
async def vps_info_command(ctx):
    """Show the caller's VPS details without exposing credentials."""
    try:
        user_vps = bot.db.get_user_vps(ctx.author.id)
        if not user_vps:
            await ctx.send("You don't have any VPS instances.", ephemeral=True)
            return

        brand = bot.branding.get_active()
        brand_name = (brand or {}).get('brand_name', 'N/A')
        embeds = []
        for vps in user_vps:
            status_raw = (vps.get('status') or 'unknown').lower()
            emoji = {
                'running': '🟢 Running',
                'stopped': '🔴 Stopped',
                'suspended': '🟠 Suspended',
            }.get(status_raw, f"❔ {vps.get('status', 'Unknown')}")

            ip = 'N/A'
            try:
                if bot.docker_client and vps.get('container_id'):
                    container = bot.docker_client.containers.get(vps['container_id'])
                    ip = _container_ip(container)
            except Exception:
                pass

            created = vps.get('created_at', 'Unknown')
            try:
                dt = parse_created_at(created)
                if dt:
                    created = dt.strftime('%d %b %Y')
            except Exception:
                pass

            e = discord.Embed(title="🖥️ Your VPS", color=discord.Color.green())
            e.add_field(name="VPS ID", value=vps.get('vps_id', 'N/A'), inline=True)
            e.add_field(name="Status", value=emoji, inline=True)
            e.add_field(name="IP", value=ip, inline=True)
            e.add_field(name="Username", value=vps.get('username', 'root'), inline=True)
            e.add_field(name="Created", value=str(created), inline=True)
            e.add_field(name="Brand", value=vps.get('branding_version') or brand_name, inline=True)
            e.add_field(name="Specs", value=f"{vps.get('memory')}GB / {vps.get('cpu')}C / {vps.get('disk')}GB", inline=True)
            embeds.append(e)

        await ctx.send(embeds=embeds[:10], ephemeral=True)
    except Exception as e:
        logger.error(f"Error in vps_info_command: {e}")
        await ctx.send(f"❌ Error: {str(e)}", ephemeral=True)


async def _run_branding_on_vps(vps, status_like=None):
    """Shared installer path for /brand-reinstall and /refresh-motd."""
    token = vps.get('token')
    container_id = vps.get('container_id')
    brand = bot.branding.get_active()
    if not bot.docker_client or not container_id:
        return False, "Docker unavailable"

    exec_fn = await make_container_exec(container_id)
    b_ok, b_msg = await install_branding_files(exec_fn, brand or {})
    m_ok, m_msg = (False, "MOTD disabled")
    if brand and int(brand.get('motd_enabled') or 0):
        m_ok, m_msg = await run_installer(exec_fn, brand)

    brand_label = "None"
    if brand:
        brand_label = f"{brand.get('brand_name', 'Unknown')} v{brand.get('version', 1)}"
        bot.db.update_vps(token, {'branding_version': brand_label})

    ok = b_ok and (m_ok or not int((brand or {}).get('motd_enabled') or 0))
    bot.db.log_deployment(vps.get('created_by'), vps.get('vps_id'), 'brand_reinstall',
                          'ok' if ok else 'failed', f"{b_msg}; {m_msg}")
    return ok, f"{b_msg}; {m_msg} ({brand_label})"


@bot.hybrid_command(name='brand-reinstall', description='Reinstall branding/MOTD on a VPS (Admin only)')
@app_commands.describe(vps_id="VPS ID to reinstall branding on")
async def brand_reinstall_command(ctx, vps_id: str):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    token, vps = bot.db.get_vps_by_id(vps_id)
    if not vps:
        await ctx.send("❌ VPS not found!", ephemeral=True)
        return
    if vps.get('status') != 'running':
        await ctx.send("❌ VPS must be running to reinstall branding.", ephemeral=True)
        return
    status_msg = await ctx.send(f"🔄 Reinstalling branding on {vps_id}...")
    try:
        ok, msg = await _run_branding_on_vps(vps)
        await status_msg.edit(content=(f"✅ Branding reinstalled: {msg}" if ok else f"⚠️ Branding incomplete: {msg}"))
        logger.info(f"Admin brand-reinstall {vps_id}: {msg}")
    except Exception as e:
        logger.error(f"brand-reinstall failed for {vps_id}: {e}")
        await status_msg.edit(content=f"❌ Branding reinstall failed: {e}")


@bot.hybrid_command(name='refresh-motd', description='Refresh branded MOTD on your VPS')
@app_commands.describe(vps_id="VPS ID (optional if you have one)")
async def refresh_motd_command(ctx, vps_id: str = None):
    """Owner (or admin) can re-run branding/MOTD on their VPS."""
    try:
        if vps_id:
            token, vps = bot.db.get_vps_by_id(vps_id)
            if not vps or (vps.get('created_by') != str(ctx.author.id) and not is_admin(ctx)):
                await ctx.send("❌ VPS not found or you don't have access to it!", ephemeral=True)
                return
        else:
            owned = [v for v in bot.db.get_user_vps(ctx.author.id) if v.get('status') == 'running']
            if not owned:
                await ctx.send("You don't have a running VPS.", ephemeral=True)
                return
            vps = owned[0]

        status_msg = await ctx.send(f"🔄 Refreshing MOTD on {vps.get('vps_id')}...")
        ok, msg = await _run_branding_on_vps(vps)
        await status_msg.edit(content=(f"✅ {msg}" if ok else f"⚠️ {msg}"))
    except Exception as e:
        logger.error(f"refresh-motd failed: {e}")
        await ctx.send(f"❌ Error: {e}", ephemeral=True)


# ======================================================================
# Admin commands: invites / deployment toggles / channels
# ======================================================================

@bot.hybrid_command(name='setinvites', description='Set required invite count (Admin only)')
@app_commands.describe(amount="Required valid invites")
async def setinvites_command(ctx, amount: int):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    if amount < 0:
        await ctx.send("❌ Amount must be 0 or greater.", ephemeral=True)
        return
    bot.db.set_setting('required_invites', amount)
    logger.info(f"Admin set required invites to {amount}")
    await ctx.send(f"✅ Required invites set to **{amount}**.", ephemeral=True)


@bot.hybrid_command(name='resetinvites', description="Reset a user's invite progress (Admin only)")
@app_commands.describe(user="User to reset")
async def resetinvites_command(ctx, user: discord.User):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    bot.db.reset_invites(user.id)
    logger.info(f"Admin reset invites for {user.id}")
    await ctx.send(f"✅ Invite progress reset for {user.mention}.", ephemeral=True)


@bot.hybrid_command(name='addinvites', description='Manually add valid invites (Admin only)')
@app_commands.describe(user="User", amount="How many invites to add")
async def addinvites_command(ctx, user: discord.User, amount: int):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive.", ephemeral=True)
        return
    bot.db.add_invites(user.id, amount)
    required = bot.db.get_setting('required_invites', 5)
    valid = bot.db.get_valid_invites(user.id)
    eligible = valid >= required
    bot.db.set_eligible(user.id, 1 if eligible else 0)
    if eligible:
        await bot.invite_tracker.notify_completion(bot, user.id, valid, required)
    logger.info(f"Admin added {amount} invites to {user.id} (now {valid})")
    await ctx.send(
        f"✅ Added **{amount}** invites to {user.mention}. "
        f"Now **{valid}/{required}** — {'Eligible' if eligible else 'Not eligible'}.",
        ephemeral=True,
    )


@bot.hybrid_command(name='removeinvites', description='Remove invites (Admin only)')
@app_commands.describe(user="User", amount="How many invites to remove")
async def removeinvites_command(ctx, user: discord.User, amount: int):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive.", ephemeral=True)
        return
    bot.db.remove_invites(user.id, amount)
    required = bot.db.get_setting('required_invites', 5)
    valid = bot.db.get_valid_invites(user.id)
    eligible = valid >= required
    bot.db.set_eligible(user.id, 1 if eligible else 0)
    if not eligible:
        bot.db.set_completion_notified(user.id, 0)
    logger.info(f"Admin removed {amount} invites from {user.id} (now {valid})")
    await ctx.send(
        f"✅ Removed **{amount}** invites from {user.mention}. "
        f"Now **{valid}/{required}** — {'Eligible' if eligible else 'Not eligible'}.",
        ephemeral=True,
    )


@bot.hybrid_command(name='blacklist', description='Blacklist a user from VPS creation (Admin only)')
@app_commands.describe(user="User to blacklist")
async def blacklist_command(ctx, user: discord.User):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    bot.db.ban_user(user.id)
    logger.info(f"Admin blacklisted {user.id}")
    await ctx.send(f"✅ {user.mention} has been blacklisted from VPS creation.")


@bot.hybrid_command(name='unblacklist', description='Remove a user from the blacklist (Admin only)')
@app_commands.describe(user="User to unblacklist")
async def unblacklist_command(ctx, user: discord.User):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    bot.db.unban_user(user.id)
    logger.info(f"Admin unblacklisted {user.id}")
    await ctx.send(f"✅ {user.mention} has been removed from the blacklist.")


@bot.hybrid_command(name='vps-enable', description='Enable VPS creation (Admin only)')
async def vps_enable_command(ctx):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    bot.db.set_setting('vps_enabled', 1)
    logger.info("Admin enabled VPS deployment")
    await ctx.send("✅ VPS creation is now **enabled**.", ephemeral=True)


@bot.hybrid_command(name='vps-disable', description='Disable VPS creation (Admin only)')
async def vps_disable_command(ctx):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    bot.db.set_setting('vps_enabled', 0)
    logger.info("Admin disabled VPS deployment")
    await ctx.send("✅ VPS creation is now **disabled**.", ephemeral=True)


@bot.hybrid_command(name='setlogchannel', description='Set deployment/log channel (Admin only)')
@app_commands.describe(channel="Channel for deployment logs")
async def setlogchannel_command(ctx, channel: discord.TextChannel):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    bot.db.set_setting('log_channel_id', channel.id)
    logger.info(f"Admin set log channel to {channel.id}")
    await ctx.send(f"✅ Log channel set to {channel.mention}.", ephemeral=True)


@bot.hybrid_command(name='setcompletionchannel', description='Set invite-completion notification channel (Admin only)')
@app_commands.describe(channel="Channel for completion notifications")
async def setcompletionchannel_command(ctx, channel: discord.TextChannel):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    bot.db.set_setting('completion_channel_id', channel.id)
    logger.info(f"Admin set completion channel to {channel.id}")
    await ctx.send(f"✅ Completion channel set to {channel.mention}.", ephemeral=True)


# ======================================================================
# Branding commands
# ======================================================================

@bot.hybrid_command(name='brand', description='Show current VPS branding (Admin only)')
async def brand_command(ctx):
    if not is_admin(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    brand = bot.branding.get_active()
    if not brand:
        await ctx.send("No branding configured.", ephemeral=True)
        return
    await ctx.send(embed=render_brand_embed(brand))


def _brand_admin_check(ctx):
    if not is_admin(ctx):
        return False
    return True


@bot.hybrid_command(name='brand-name', description='Set brand name (Admin only)')
@app_commands.describe(name="Brand name")
async def brand_name_command(ctx, name: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active()
    bot.branding.update_field(active['profile_name'], 'brand_name', name.strip())
    await ctx.send(f"✅ Brand name set to **{name.strip()}** (version {bot.branding.get_active()['version']}).", ephemeral=True)


@bot.hybrid_command(name='brand-tagline', description='Set brand tagline (Admin only)')
@app_commands.describe(text="Tagline")
async def brand_tagline_command(ctx, text: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active()
    bot.branding.update_field(active['profile_name'], 'brand_tagline', text)
    await ctx.send("✅ Brand tagline updated.", ephemeral=True)


@bot.hybrid_command(name='brand-website', description='Set brand website (Admin only)')
@app_commands.describe(url="Website URL")
async def brand_website_command(ctx, url: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active()
    bot.branding.update_field(active['profile_name'], 'website', url)
    await ctx.send("✅ Brand website updated.", ephemeral=True)


@bot.hybrid_command(name='brand-discord', description='Set brand Discord invite (Admin only)')
@app_commands.describe(url="Discord invite URL")
async def brand_discord_command(ctx, url: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active()
    bot.branding.update_field(active['profile_name'], 'discord', url)
    await ctx.send("✅ Brand Discord link updated.", ephemeral=True)


@bot.hybrid_command(name='brand-support', description='Set brand support email (Admin only)')
@app_commands.describe(email="Support email")
async def brand_support_command(ctx, email: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active()
    bot.branding.update_field(active['profile_name'], 'support_email', email)
    await ctx.send("✅ Support email updated.", ephemeral=True)


@bot.hybrid_command(name='brand-motd', description='Enable/disable branded MOTD (Admin only)')
@app_commands.describe(state="on or off")
async def brand_motd_command(ctx, state: Literal['on', 'off']):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active()
    bot.branding.set_motd_enabled(active['profile_name'], state == 'on')
    await ctx.send(f"✅ MOTD **{state}** for future deployments.", ephemeral=True)


@bot.hybrid_command(name='brand-colors', description='Set MOTD colors (Admin only)')
@app_commands.describe(primary="Primary color", secondary="Secondary color")
async def brand_colors_command(ctx, primary: str, secondary: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    from config import ANSI_COLORS
    for c in (primary, secondary):
        if c.lower() not in ANSI_COLORS:
            await ctx.send(
                f"❌ Unknown color `{c}`. Valid: {', '.join(sorted(ANSI_COLORS))}",
                ephemeral=True,
            )
            return
    active = bot.branding.get_active()
    bot.branding.update_field(active['profile_name'], 'primary_color', primary.lower())
    bot.branding.update_field(active['profile_name'], 'secondary_color', secondary.lower())
    await ctx.send(f"✅ Colors set to **{primary}** / **{secondary}**.", ephemeral=True)


@bot.hybrid_command(name='brand-reset', description='Restore default branding (Admin only)')
async def brand_reset_command(ctx):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active() or {'profile_name': 'default'}
    bot.branding.reset(active['profile_name'])
    logger.info("Admin reset branding")
    await ctx.send("✅ Branding restored to defaults.", ephemeral=True)


@bot.hybrid_command(name='brand-create', description='Create a branding profile (Admin only)')
@app_commands.describe(name="New profile name")
async def brand_create_command(ctx, name: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    try:
        bot.branding.create_profile(name)
        await ctx.send(f"✅ Branding profile **{name}** created. Use `/brand-use {name}` to activate it.", ephemeral=True)
    except ValueError as e:
        await ctx.send(f"❌ {e}", ephemeral=True)


@bot.hybrid_command(name='brand-use', description='Activate a branding profile (Admin only)')
@app_commands.describe(name="Profile to activate")
async def brand_use_command(ctx, name: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    try:
        bot.branding.use_profile(name)
        brand = bot.branding.get_active()
        await ctx.send(f"✅ Active branding is now **{brand.get('brand_name')}** (v{brand.get('version')}).", ephemeral=True)
    except ValueError as e:
        await ctx.send(f"❌ {e}", ephemeral=True)


@bot.hybrid_command(name='brand-list', description='List branding profiles (Admin only)')
async def brand_list_command(ctx):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    profiles = bot.branding.list_profiles()
    if not profiles:
        await ctx.send("No branding profiles.", ephemeral=True)
        return
    lines = []
    for p in profiles:
        marker = " ✅ active" if p['is_active'] else ""
        lines.append(f"• **{p['profile_name']}** — {p['brand_name']} v{p['version']}{marker}")
    embed = discord.Embed(title="Branding Profiles", description="\n".join(lines), color=discord.Color.magenta())
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name='brand-template', description='Set custom MOTD template (Admin only)')
@app_commands.describe(
    template="Template text (use {brand_name} {hostname} {cpu} etc.). Pass 'clear' to reset."
)
async def brand_template_command(ctx, template: str):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    active = bot.branding.get_active()
    value = '' if template.strip().lower() == 'clear' else template
    bot.branding.update_field(active['profile_name'], 'motd_template', value)
    if value:
        await ctx.send(
            "✅ Custom MOTD template saved. Variables like `{brand_name}`, `{hostname}`, `{cpu}`, "
            "`{ram_percent}`, `{disk}`, `{website}` will be filled at login.\n"
            "Use `/brand-reinstall` to push it to existing VPS instances.",
            ephemeral=True,
        )
    else:
        await ctx.send("✅ Custom MOTD template cleared — default premium layout restored.", ephemeral=True)


@bot.hybrid_command(name='brand-update-existing', description='Push current branding to running VPS instances (Admin only)')
async def brand_update_existing_command(ctx):
    if not _brand_admin_check(ctx):
        await ctx.send("❌ You must be an admin to use this command!", ephemeral=True)
        return
    status_msg = await ctx.send("🔄 Updating branding on existing VPS instances...")
    all_vps = bot.db.get_all_vps()
    updated = failed = skipped = 0
    for token, vps in all_vps.items():
        if vps.get('status') != 'running':
            skipped += 1
            continue
        try:
            ok, _msg = await _run_branding_on_vps(vps)
            if ok:
                updated += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"brand-update-existing failed for {vps.get('vps_id')}: {e}")
            failed += 1
    logger.info(f"Admin brand-update-existing: updated={updated} failed={failed} skipped={skipped}")
    await status_msg.edit(
        content=(
            f"✅ Branding update complete.\n"
            f"Updated: {updated} | Failed: {failed} | Skipped (not running): {skipped}"
        )
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ You don't have permission to use this command!", ephemeral=True)
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send("❌ Command not found! Use `/help` to see available commands.", ephemeral=True)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument: {error.param.name}", ephemeral=True)
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(f"❌ An error occurred: {str(error)}", ephemeral=True)

# Run the bot
if __name__ == "__main__":
    try:
        # Create directories if they don't exist
        os.makedirs("temp_dockerfiles", exist_ok=True)
        os.makedirs("migrations", exist_ok=True)
        
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        traceback.print_exc()
