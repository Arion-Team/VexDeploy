"""SQLite persistence for VexDeploy."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DEFAULT_BRAND, DEFAULT_SETTINGS


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _fetch(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _fetch_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── schema ──────────────────────────────────────────────
    def _migrate(self) -> None:
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS vps_instances (
                vps_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                container_id TEXT NOT NULL,
                container_name TEXT NOT NULL,
                memory_mb INTEGER NOT NULL,
                cpus INTEGER NOT NULL,
                disk_gb INTEGER NOT NULL,
                os_image TEXT NOT NULL,
                status TEXT NOT NULL,
                ip_address TEXT DEFAULT '',
                ssh_port INTEGER DEFAULT 0,
                username TEXT DEFAULT 'root',
                password_hash TEXT DEFAULT '',
                password_plain TEXT DEFAULT '',
                branding_version INTEGER DEFAULT 0,
                brand_label TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen TEXT
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS user_invites (
                user_id TEXT PRIMARY KEY,
                valid_invites INTEGER DEFAULT 0,
                fake_invites INTEGER DEFAULT 0,
                eligible INTEGER DEFAULT 0,
                completion_notified INTEGER DEFAULT 0,
                updated_at TEXT
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS invite_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                join_discriminator TEXT NOT NULL UNIQUE,
                member_id TEXT NOT NULL,
                inviter_id TEXT,
                invite_code TEXT,
                guild_id TEXT,
                is_fake INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS invite_attributions (
                member_id TEXT PRIMARY KEY,
                inviter_id TEXT NOT NULL,
                invite_code TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS branding (
                profile_name TEXT PRIMARY KEY,
                brand_name TEXT NOT NULL,
                brand_tagline TEXT DEFAULT '',
                website TEXT DEFAULT '',
                discord TEXT DEFAULT '',
                support_email TEXT DEFAULT '',
                motd_enabled INTEGER DEFAULT 1,
                motd_type TEXT DEFAULT 'premium',
                primary_color TEXT DEFAULT 'cyan',
                secondary_color TEXT DEFAULT 'magenta',
                logo TEXT DEFAULT 'AUTO',
                footer TEXT DEFAULT '',
                motd_template TEXT DEFAULT '',
                is_active INTEGER DEFAULT 0,
                version INTEGER DEFAULT 1,
                updated_at TEXT
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS deployment_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                vps_id TEXT DEFAULT '',
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id TEXT PRIMARY KEY,
                banned_at TEXT NOT NULL,
                banned_by TEXT DEFAULT ''
            )
            """
        )
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                user_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                added_by TEXT DEFAULT ''
            )
            """
        )

        self._repair_vps_schema()
        self._repair_deployment_logs_schema()

        for key, value in DEFAULT_SETTINGS.items():
            self._exec(
                "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
                (key, value),
            )

        active = self._fetch_one(
            "SELECT profile_name FROM branding WHERE is_active = 1"
        )
        if not active:
            self._seed_default_brand()

        if self.get_setting("brand_enforced_v1", None) is None:
            self._enforce_vexdeploy_brand()
            self.set_setting("brand_enforced_v1", "1")

    def _table_cols(self, table: str) -> list[str]:
        try:
            return [r[1] for r in self._fetch(f"PRAGMA table_info({table})")]
        except Exception:
            return []

    def _repair_vps_schema(self) -> None:
        """Bring an old/foreign vps_instances table to the expected schema."""
        cols = self._table_cols("vps_instances")
        if not cols:
            return

        owner_aliases = ("owner_id", "user_id", "discord_id", "creator_id", "owner")
        found_owner = next((c for c in owner_aliases if c in cols), None)
        if found_owner and found_owner != "owner_id":
            try:
                self._exec(
                    f"ALTER TABLE vps_instances RENAME COLUMN {found_owner} TO owner_id"
                )
                cols = self._table_cols("vps_instances")
            except Exception:
                cols = self._table_cols("vps_instances")

        if "owner_id" not in cols:
            self._rebuild_vps_table(old_cols=cols)
            return

        add_columns: list[tuple[str, str]] = [
            ("vps_id", "TEXT"),
            ("container_id", "TEXT NOT NULL DEFAULT ''"),
            ("container_name", "TEXT NOT NULL DEFAULT ''"),
            ("memory_mb", "INTEGER NOT NULL DEFAULT 1024"),
            ("cpus", "INTEGER NOT NULL DEFAULT 1"),
            ("disk_gb", "INTEGER NOT NULL DEFAULT 10"),
            ("os_image", "TEXT NOT NULL DEFAULT 'ubuntu:22.04'"),
            ("status", "TEXT NOT NULL DEFAULT 'running'"),
            ("ip_address", "TEXT DEFAULT ''"),
            ("ssh_port", "INTEGER DEFAULT 0"),
            ("username", "TEXT DEFAULT 'root'"),
            ("password_hash", "TEXT DEFAULT ''"),
            ("password_plain", "TEXT DEFAULT ''"),
            ("branding_version", "INTEGER DEFAULT 0"),
            ("brand_label", "TEXT DEFAULT ''"),
            ("created_at", "TEXT DEFAULT ''"),
            ("last_seen", "TEXT"),
        ]
        cols_set = set(cols)
        for name, decl in add_columns:
            if name not in cols_set:
                try:
                    self._exec(f"ALTER TABLE vps_instances ADD COLUMN {name} {decl}")
                except Exception:
                    pass

        final_cols = self._table_cols("vps_instances")
        required = {"vps_id", "owner_id", "container_id", "container_name",
                    "memory_mb", "cpus", "disk_gb", "os_image", "status",
                    "created_at"}
        if not required.issubset(set(final_cols)):
            self._rebuild_vps_table(old_cols=final_cols)

    def _rebuild_vps_table(self, old_cols: list[str] | None = None) -> None:
        """Recreate vps_instances with the canonical schema, preserving rows if possible."""
        old_cols = old_cols or []
        old_rows: list[dict[str, Any]] = []
        if old_cols:
            try:
                old_rows = [dict(r) for r in self._fetch("SELECT * FROM vps_instances")]
            except Exception:
                old_rows = []

        id_aliases = ("vps_id", "instance_id", "id", "uuid")
        owner_aliases = ("owner_id", "user_id", "discord_id", "creator_id", "owner")
        container_aliases = ("container_id", "docker_id", "container")

        def pick(row: dict[str, Any], aliases: tuple[str, ...], default: str = "") -> Any:
            for a in aliases:
                if a in row and row[a] not in (None, ""):
                    return row[a]
            for a in aliases:
                if a in row:
                    return row[a]
            return default

        try:
            self._exec("DROP TABLE IF EXISTS vps_instances")
        except Exception:
            pass
        self._exec(
            """
            CREATE TABLE IF NOT EXISTS vps_instances (
                vps_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                container_id TEXT NOT NULL,
                container_name TEXT NOT NULL,
                memory_mb INTEGER NOT NULL,
                cpus INTEGER NOT NULL,
                disk_gb INTEGER NOT NULL,
                os_image TEXT NOT NULL,
                status TEXT NOT NULL,
                ip_address TEXT DEFAULT '',
                ssh_port INTEGER DEFAULT 0,
                username TEXT DEFAULT 'root',
                password_hash TEXT DEFAULT '',
                password_plain TEXT DEFAULT '',
                branding_version INTEGER DEFAULT 0,
                brand_label TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                last_seen TEXT
            )
            """
        )

        if not old_rows:
            return

        for i, row in enumerate(old_rows):
            vps_id = str(pick(row, id_aliases, f"vx_migrated_{i:04d}"))
            owner_id = str(pick(row, owner_aliases, "0")) or "0"
            container_id = str(pick(row, container_aliases, "")) or ""
            try:
                self._exec(
                    """
                    INSERT OR IGNORE INTO vps_instances (
                        vps_id, owner_id, container_id, container_name,
                        memory_mb, cpus, disk_gb, os_image, status,
                        ip_address, ssh_port, username, password_hash, password_plain,
                        branding_version, brand_label, created_at, last_seen
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        vps_id,
                        owner_id,
                        container_id,
                        str(row.get("container_name") or f"vps-{vps_id}"),
                        int(row.get("memory_mb") or 1024),
                        int(row.get("cpus") or row.get("cpu") or 1),
                        int(row.get("disk_gb") or row.get("disk") or 10),
                        str(row.get("os_image") or "ubuntu:22.04"),
                        str(row.get("status") or "running"),
                        str(row.get("ip_address") or row.get("ip") or ""),
                        int(row.get("ssh_port") or 0),
                        str(row.get("username") or "root"),
                        str(row.get("password_hash") or row.get("password_plain") or ""),
                        str(row.get("password_plain") or row.get("password_hash") or ""),
                        int(row.get("branding_version") or 0),
                        str(row.get("brand_label") or ""),
                        str(row.get("created_at") or utcnow()),
                        row.get("last_seen"),
                    ),
                )
            except Exception:
                continue

    def _repair_deployment_logs_schema(self) -> None:
        cols = self._table_cols("deployment_logs")
        if not cols:
            return
        if "owner_id" in cols or "user_id" not in cols:
            if "owner_id" not in cols and "user_id" not in cols:
                try:
                    self._exec("DROP TABLE IF EXISTS deployment_logs")
                    self._exec(
                        """
                        CREATE TABLE IF NOT EXISTS deployment_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            owner_id TEXT NOT NULL,
                            vps_id TEXT DEFAULT '',
                            stage TEXT NOT NULL,
                            status TEXT NOT NULL,
                            message TEXT DEFAULT '',
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                except Exception:
                    pass
            return
        try:
            self._exec(
                "ALTER TABLE deployment_logs RENAME COLUMN user_id TO owner_id"
            )
        except Exception:
            try:
                self._exec(
                    "ALTER TABLE deployment_logs ADD COLUMN owner_id TEXT DEFAULT ''"
                )
            except Exception:
                pass

    def _enforce_vexdeploy_brand(self) -> None:
        """One-time rewrite of any stored brand name/footer to VexDeploy."""
        b = dict(DEFAULT_BRAND)
        try:
            rows = self._fetch("SELECT profile_name, brand_name, footer FROM branding")
        except Exception:
            return
        for row in rows:
            profile = str(row["profile_name"])
            self._exec(
                """
                UPDATE branding
                SET brand_name = ?,
                    footer = ?,
                    version = version + 1,
                    updated_at = ?
                WHERE profile_name = ?
                """,
                (str(b["brand_name"]), str(b["footer"]), utcnow(), profile),
            )

    def _seed_default_brand(self) -> None:
        b = dict(DEFAULT_BRAND)
        self._exec(
            """
            INSERT OR IGNORE INTO branding (
                profile_name, brand_name, brand_tagline, website, discord,
                support_email, motd_enabled, motd_type, primary_color,
                secondary_color, logo, footer, motd_template, is_active, version, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "default",
                str(b["brand_name"]),
                str(b["brand_tagline"]),
                str(b["website"]),
                str(b["discord"]),
                str(b["support_email"]),
                1 if b["motd_enabled"] else 0,
                str(b["motd_type"]),
                str(b["primary_color"]),
                str(b["secondary_color"]),
                str(b["logo"]),
                str(b["footer"]),
                str(b["motd_template"]),
                1,
                1,
                utcnow(),
            ),
        )

    # ── settings ────────────────────────────────────────────
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._fetch_one(
            "SELECT value FROM system_settings WHERE key = ?", (key,)
        )
        if not row:
            return default
        raw = row["value"]
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(raw)
            except ValueError:
                return default
        return raw

    def set_setting(self, key: str, value: Any) -> None:
        self._exec(
            """
            INSERT INTO system_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    def get_setting_str(self, key: str, default: str = "") -> str:
        return str(self.get_setting(key, default))

    def delete_setting(self, key: str) -> None:
        self._exec("DELETE FROM system_settings WHERE key = ?", (key,))

    # ── admins / bans ───────────────────────────────────────
    def add_admin(self, user_id: str, added_by: str = "") -> None:
        self._exec(
            "INSERT OR IGNORE INTO admin_users (user_id, added_at, added_by) VALUES (?,?,?)",
            (user_id, utcnow(), added_by),
        )

    def remove_admin(self, user_id: str) -> None:
        self._exec("DELETE FROM admin_users WHERE user_id = ?", (user_id,))

    def list_admins(self) -> list[str]:
        return [r["user_id"] for r in self._fetch("SELECT user_id FROM admin_users")]

    def ban_user(self, user_id: str, by: str = "") -> None:
        self._exec(
            "INSERT OR REPLACE INTO banned_users (user_id, banned_at, banned_by) VALUES (?,?,?)",
            (user_id, utcnow(), by),
        )

    def unban_user(self, user_id: str) -> None:
        self._exec("DELETE FROM banned_users WHERE user_id = ?", (user_id,))

    def is_banned(self, user_id: str) -> bool:
        return self._fetch_one(
            "SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)
        ) is not None

    def list_banned(self) -> list[sqlite3.Row]:
        return self._fetch("SELECT * FROM banned_users ORDER BY banned_at DESC")

    # ── invites ─────────────────────────────────────────────
    def get_invite_row(self, user_id: str) -> sqlite3.Row | None:
        return self._fetch_one(
            "SELECT * FROM user_invites WHERE user_id = ?", (user_id,)
        )

    def set_invites(self, user_id: str, count: int, eligible: bool | None = None) -> None:
        count = max(0, int(count))
        required = int(self.get_setting("required_invites", 5))
        if eligible is None:
            eligible = count >= required
        existing = self.get_invite_row(user_id)
        if existing:
            self._exec(
                """
                UPDATE user_invites
                SET valid_invites = ?, eligible = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (count, 1 if eligible else 0, utcnow(), user_id),
            )
        else:
            self._exec(
                """
                INSERT INTO user_invites
                    (user_id, valid_invites, fake_invites, eligible, completion_notified, updated_at)
                VALUES (?,?,?,?,0,?)
                """,
                (user_id, count, 0, 1 if eligible else 0, utcnow()),
            )

    def add_invites(self, user_id: str, amount: int) -> int:
        row = self.get_invite_row(user_id)
        current = int(row["valid_invites"]) if row else 0
        new_val = max(0, current + int(amount))
        self.set_invites(user_id, new_val)
        return new_val

    def remove_invites(self, user_id: str, amount: int) -> int:
        return self.add_invites(user_id, -int(amount))

    def reset_invites(self, user_id: str) -> None:
        self._exec(
            """
            INSERT INTO user_invites (user_id, valid_invites, fake_invites, eligible, completion_notified, updated_at)
            VALUES (?,0,0,0,0,?)
            ON CONFLICT(user_id) DO UPDATE SET
                valid_invites=0, fake_invites=0, eligible=0,
                completion_notified=0, updated_at=excluded.updated_at
            """,
            (user_id, utcnow()),
        )

    def set_completion_notified(self, user_id: str, flag: bool = True) -> None:
        self._exec(
            "UPDATE user_invites SET completion_notified = ?, updated_at = ? WHERE user_id = ?",
            (1 if flag else 0, utcnow(), user_id),
        )

    def recompute_eligibility(self, user_id: str) -> bool:
        required = int(self.get_setting("required_invites", 5))
        row = self.get_invite_row(user_id)
        valid = int(row["valid_invites"]) if row else 0
        eligible = valid >= required
        if row:
            self.set_invites(user_id, valid, eligible=eligible)
            if not eligible and row["completion_notified"]:
                self.set_completion_notified(user_id, False)
        else:
            self.set_invites(user_id, 0, eligible=False)
        return eligible

    def get_leaderboard(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._fetch(
            """
            SELECT user_id, valid_invites, fake_invites, eligible
            FROM user_invites
            ORDER BY valid_invites DESC, user_id ASC
            LIMIT ?
            """,
            (limit,),
        )

    def record_join_event(
        self,
        *,
        member_id: str,
        guild_id: str,
        inviter_id: str | None,
        invite_code: str | None,
        is_fake: bool = False,
    ) -> bool:
        """Return True if this join created a new credit (not a duplicate)."""
        discriminator = f"{guild_id}:{member_id}"
        try:
            self._exec(
                """
                INSERT INTO invite_events
                    (join_discriminator, member_id, inviter_id, invite_code, guild_id, is_fake, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    discriminator,
                    member_id,
                    inviter_id,
                    invite_code,
                    guild_id,
                    1 if is_fake else 0,
                    utcnow(),
                ),
            )
        except sqlite3.IntegrityError:
            return False

        if inviter_id and not is_fake:
            self._exec(
                """
                INSERT INTO invite_attributions (member_id, inviter_id, invite_code, created_at)
                VALUES (?,?,?,?)
                ON CONFLICT(member_id) DO UPDATE SET
                    inviter_id=excluded.inviter_id,
                    invite_code=excluded.invite_code
                """,
                (member_id, inviter_id, invite_code, utcnow()),
            )
            self.add_invites(inviter_id, 1)
            return True
        return False

    def record_leave(self, member_id: str) -> str | None:
        """Handle member leave. Returns inviter_id if invite was revoked."""
        attr = self._fetch_one(
            "SELECT inviter_id FROM invite_attributions WHERE member_id = ?",
            (member_id,),
        )
        if not attr:
            return None
        inviter_id = attr["inviter_id"]
        self._exec("DELETE FROM invite_attributions WHERE member_id = ?", (member_id,))
        self._exec(
            """
            UPDATE invite_events
            SET is_fake = 1
            WHERE member_id = ? AND is_fake = 0
            """,
            (member_id,),
        )
        self.remove_invites(inviter_id, 1)
        self.recompute_eligibility(inviter_id)
        return inviter_id

    # ── VPS ─────────────────────────────────────────────────
    def create_vps_record(
        self,
        *,
        owner_id: str,
        container_id: str,
        container_name: str,
        memory_mb: int,
        cpus: int,
        disk_gb: int,
        os_image: str,
        ip_address: str = "",
        ssh_port: int = 0,
        username: str = "root",
        password: str = "",
        status: str = "running",
        brand_label: str = "",
        branding_version: int = 0,
    ) -> str:
        vps_id = f"vx_{uuid.uuid4().hex[:10]}"
        self._exec(
            """
            INSERT INTO vps_instances (
                vps_id, owner_id, container_id, container_name,
                memory_mb, cpus, disk_gb, os_image, status,
                ip_address, ssh_port, username, password_hash, password_plain,
                branding_version, brand_label, created_at, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                vps_id,
                owner_id,
                container_id,
                container_name,
                memory_mb,
                cpus,
                disk_gb,
                os_image,
                status,
                ip_address,
                ssh_port,
                username,
                password,
                password,
                branding_version,
                brand_label,
                utcnow(),
                utcnow(),
            ),
        )
        return vps_id

    def get_vps(self, vps_id: str) -> sqlite3.Row | None:
        return self._fetch_one(
            "SELECT * FROM vps_instances WHERE vps_id = ?", (vps_id,)
        )

    def get_vps_by_container(self, container_id: str) -> sqlite3.Row | None:
        return self._fetch_one(
            "SELECT * FROM vps_instances WHERE container_id = ?", (container_id,)
        )

    def list_user_vps(self, owner_id: str) -> list[sqlite3.Row]:
        return self._fetch(
            "SELECT * FROM vps_instances WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        )

    def list_all_vps(self) -> list[sqlite3.Row]:
        return self._fetch("SELECT * FROM vps_instances ORDER BY created_at DESC")

    def count_vps(self, owner_id: str | None = None) -> int:
        if owner_id:
            row = self._fetch_one(
                "SELECT COUNT(*) AS c FROM vps_instances WHERE owner_id = ?",
                (owner_id,),
            )
        else:
            row = self._fetch_one("SELECT COUNT(*) AS c FROM vps_instances")
        return int(row["c"]) if row else 0

    def update_vps_status(self, vps_id: str, status: str) -> None:
        self._exec(
            "UPDATE vps_instances SET status = ?, last_seen = ? WHERE vps_id = ?",
            (status, utcnow(), vps_id),
        )

    def update_vps_password(self, vps_id: str, password: str) -> None:
        self._exec(
            "UPDATE vps_instances SET password_plain = ?, password_hash = ? WHERE vps_id = ?",
            (password, password, vps_id),
        )

    def update_vps_branding(self, vps_id: str, version: int, label: str) -> None:
        self._exec(
            """
            UPDATE vps_instances
            SET branding_version = ?, brand_label = ?, last_seen = ?
            WHERE vps_id = ?
            """,
            (version, label, utcnow(), vps_id),
        )

    def delete_vps(self, vps_id: str) -> None:
        self._exec("DELETE FROM vps_instances WHERE vps_id = ?", (vps_id,))

    def get_last_vps_created_at(self, owner_id: str) -> str | None:
        row = self._fetch_one(
            """
            SELECT created_at FROM vps_instances
            WHERE owner_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (owner_id,),
        )
        return row["created_at"] if row else None

    # ── branding ────────────────────────────────────────────
    def get_active_branding(self) -> dict[str, Any] | None:
        row = self._fetch_one(
            "SELECT * FROM branding WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
        )
        return dict(row) if row else None

    def get_branding_profile(self, profile_name: str) -> dict[str, Any] | None:
        row = self._fetch_one(
            "SELECT * FROM branding WHERE profile_name = ?", (profile_name,)
        )
        return dict(row) if row else None

    def list_branding_profiles(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._fetch("SELECT * FROM branding ORDER BY profile_name")
        ]

    def create_branding_profile(
        self, profile_name: str, values: dict[str, Any] | None = None
    ) -> None:
        base = dict(DEFAULT_BRAND)
        if values:
            base.update(values)
        self._exec(
            """
            INSERT OR IGNORE INTO branding (
                profile_name, brand_name, brand_tagline, website, discord,
                support_email, motd_enabled, motd_type, primary_color,
                secondary_color, logo, footer, motd_template, is_active, version, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?)
            """,
            (
                profile_name,
                str(base["brand_name"]),
                str(base["brand_tagline"]),
                str(base["website"]),
                str(base["discord"]),
                str(base["support_email"]),
                1 if base["motd_enabled"] else 0,
                str(base["motd_type"]),
                str(base["primary_color"]),
                str(base["secondary_color"]),
                str(base["logo"]),
                str(base["footer"]),
                str(base["motd_template"]),
                utcnow(),
            ),
        )

    def activate_branding(self, profile_name: str) -> bool:
        if not self.get_branding_profile(profile_name):
            return False
        self._exec("UPDATE branding SET is_active = 0")
        self._exec(
            "UPDATE branding SET is_active = 1, updated_at = ? WHERE profile_name = ?",
            (utcnow(), profile_name),
        )
        return True

    def update_branding_field(self, profile_name: str, field: str, value: Any) -> int:
        profile = self.get_branding_profile(profile_name)
        if not profile:
            return 0
        allowed = {
            "brand_name",
            "brand_tagline",
            "website",
            "discord",
            "support_email",
            "motd_enabled",
            "motd_type",
            "primary_color",
            "secondary_color",
            "logo",
            "footer",
            "motd_template",
        }
        if field not in allowed:
            return int(profile["version"])
        if field == "motd_enabled":
            db_value = 1 if value else 0
        else:
            db_value = value
        old = profile.get(field)
        if old == db_value or str(old) == str(db_value):
            return int(profile["version"])
        self._exec(
            f"UPDATE branding SET {field} = ?, version = version + 1, updated_at = ? WHERE profile_name = ?",
            (db_value, utcnow(), profile_name),
        )
        return int(profile["version"]) + 1

    def reset_active_branding(self) -> None:
        active = self.get_active_branding()
        name = active["profile_name"] if active else "default"
        b = dict(DEFAULT_BRAND)
        self.update_branding_field(name, "brand_name", b["brand_name"])
        self.update_branding_field(name, "brand_tagline", b["brand_tagline"])
        self.update_branding_field(name, "website", b["website"])
        self.update_branding_field(name, "discord", b["discord"])
        self.update_branding_field(name, "support_email", b["support_email"])
        self.update_branding_field(name, "motd_enabled", True)
        self.update_branding_field(name, "primary_color", b["primary_color"])
        self.update_branding_field(name, "secondary_color", b["secondary_color"])
        self.update_branding_field(name, "logo", b["logo"])
        self.update_branding_field(name, "footer", b["footer"])
        self.update_branding_field(name, "motd_template", b["motd_template"])

    def active_brand_version(self) -> int:
        brand = self.get_active_branding()
        return int(brand["version"]) if brand else 0

    def active_brand_label(self) -> str:
        brand = self.get_active_branding()
        if not brand:
            return "VexDeploy"
        return f"{brand['brand_name']} v{brand['version']}"

    # ── logs ────────────────────────────────────────────────
    def log_deployment(
        self, owner_id: str, vps_id: str, stage: str, status: str, message: str = ""
    ) -> None:
        self._exec(
            """
            INSERT INTO deployment_logs (owner_id, vps_id, stage, status, message, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (owner_id, vps_id, stage, status, message, utcnow()),
        )

    def recent_logs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._fetch(
            "SELECT * FROM deployment_logs ORDER BY id DESC LIMIT ?", (limit,)
        )

    def export_backup(self) -> dict[str, Any]:
        tables = [
            "system_settings",
            "vps_instances",
            "user_invites",
            "invite_events",
            "invite_attributions",
            "branding",
            "deployment_logs",
            "banned_users",
            "admin_users",
        ]
        data: dict[str, Any] = {}
        for table in tables:
            rows = self._fetch(f"SELECT * FROM {table}")
            data[table] = [dict(r) for r in rows]
        data["_meta"] = {"exported_at": utcnow()}
        return data

    def import_backup(self, data: dict[str, Any]) -> None:
        for table, rows in data.items():
            if table.startswith("_") or not isinstance(rows, list) or not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join("?" for _ in cols)
            col_sql = ",".join(cols)
            for row in rows:
                self._exec(
                    f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
                    tuple(row.get(c) for c in cols),
                )
