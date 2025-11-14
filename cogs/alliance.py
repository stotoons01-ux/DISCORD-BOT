import discord
from discord import app_commands
from discord.ext import commands
import sqlite3  
import asyncio
from datetime import datetime
import logging
import os
import traceback

logger = logging.getLogger(__name__)
try:
    from db.mongo_adapters import mongo_enabled, AllianceMetadataAdapter, AllianceMembersAdapter, UserProfilesAdapter
except Exception:
    mongo_enabled = lambda: False
    AllianceMetadataAdapter = None
    AllianceMembersAdapter = None
    UserProfilesAdapter = None

class Alliance(commands.Cog):
    def __init__(self, bot, conn):
        self.bot = bot
        self.conn = conn
        self.c = self.conn.cursor()
        
        self.conn_users = sqlite3.connect('db/users.sqlite')
        self.c_users = self.conn_users.cursor()
        
        self.conn_settings = sqlite3.connect('db/settings.sqlite')
        self.c_settings = self.conn_settings.cursor()
        
        self.conn_giftcode = sqlite3.connect('db/giftcode.sqlite')
        self.c_giftcode = self.conn_giftcode.cursor()

        self._create_table()
        self._check_and_add_column()

    # --------------------- Mongo admin helpers ---------------------
    def _load_admins_from_mongo(self) -> dict:
        """Return a mapping of admin user_id -> is_initial when Mongo is enabled."""
        try:
            if not mongo_enabled() or AllianceMetadataAdapter is None:
                return {}
            data = AllianceMetadataAdapter.get_metadata('admin_list')
            if not data:
                return {}
            # Expecting a dict-like mapping of string user_id -> int flag
            if isinstance(data, dict):
                return {int(k): int(v) for k, v in data.items()}
            return {}
        except Exception:
            return {}

    def _get_admin_count(self) -> int:
        if mongo_enabled():
            return len(self._load_admins_from_mongo())
        try:
            self.c_settings.execute("SELECT COUNT(*) FROM admin")
            return int(self.c_settings.fetchone()[0])
        except Exception:
            return 0

    def _is_admin(self, user_id: int) -> tuple:
        """Return (exists: bool, is_initial: int|None)."""
        if mongo_enabled():
            admins = self._load_admins_from_mongo()
            val = admins.get(int(user_id))
            return (val is not None, int(val) if val is not None else None)
        try:
            self.c_settings.execute("SELECT id, is_initial FROM admin WHERE id = ?", (user_id,))
            admin = self.c_settings.fetchone()
            if admin is None:
                return (False, None)
            return (True, admin[1])
        except Exception:
            return (False, None)

    def _add_admin(self, user_id: int, is_initial: int = 1) -> bool:
        if mongo_enabled():
            try:
                admins = self._load_admins_from_mongo()
                admins[str(int(user_id))] = int(is_initial)
                return AllianceMetadataAdapter.set_metadata('admin_list', admins)
            except Exception:
                return False
        try:
            self.c_settings.execute("INSERT INTO admin (id, is_initial) VALUES (?, ?)", (user_id, is_initial))
            self.conn_settings.commit()
            return True
        except Exception:
            return False

    # --------------------- Mongo alliances helpers ---------------------
    def _load_alliances_from_mongo(self) -> list:
        """Return list of tuples (alliance_id:int, name:str, discord_server_id:int)"""
        try:
            if not mongo_enabled() or AllianceMetadataAdapter is None:
                return []
            data = AllianceMetadataAdapter.get_metadata('alliances') or {}
            result = []
            for k, v in data.items():
                try:
                    aid = int(k)
                    name = v.get('name')
                    dsid = int(v.get('discord_server_id')) if v.get('discord_server_id') is not None else None
                    result.append((aid, name, dsid))
                except Exception:
                    continue
            # sort by alliance_id
            return sorted(result, key=lambda x: x[0])
        except Exception:
            return []

    def _load_alliancesettings_from_mongo(self) -> dict:
        """Return mapping alliance_id -> {'channel_id':..., 'interval':...} or empty dict"""
        try:
            if not mongo_enabled() or AllianceMetadataAdapter is None:
                return {}
            data = AllianceMetadataAdapter.get_metadata('alliancesettings') or {}
            # ensure proper types
            out = {}
            for k, v in data.items():
                try:
                    aid = int(k)
                    ch = int(v.get('channel_id')) if v.get('channel_id') is not None else None
                    interval = int(v.get('interval')) if v.get('interval') is not None else 0
                    out[aid] = {'channel_id': ch, 'interval': interval}
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _save_alliance_to_mongo(self, alliance_id: int, name: str, discord_server_id: int | None) -> bool:
        try:
            if not mongo_enabled() or AllianceMetadataAdapter is None:
                return False
            alliances = AllianceMetadataAdapter.get_metadata('alliances') or {}
            alliances[str(int(alliance_id))] = {'name': name, 'discord_server_id': int(discord_server_id) if discord_server_id is not None else None}
            return AllianceMetadataAdapter.set_metadata('alliances', alliances)
        except Exception:
            return False

    def _save_alliancesettings_to_mongo(self, alliance_id: int, channel_id: int | None, interval: int) -> bool:
        try:
            if not mongo_enabled() or AllianceMetadataAdapter is None:
                return False
            settings = AllianceMetadataAdapter.get_metadata('alliancesettings') or {}
            settings[str(int(alliance_id))] = {'channel_id': int(channel_id) if channel_id is not None else None, 'interval': int(interval)}
            return AllianceMetadataAdapter.set_metadata('alliancesettings', settings)
        except Exception:
            return False

    def _delete_alliance_from_mongo(self, alliance_id: int) -> bool:
        try:
            if not mongo_enabled() or AllianceMetadataAdapter is None:
                return False
            alliances = AllianceMetadataAdapter.get_metadata('alliances') or {}
            settings = AllianceMetadataAdapter.get_metadata('alliancesettings') or {}
            alliances.pop(str(int(alliance_id)), None)
            settings.pop(str(int(alliance_id)), None)
            ok1 = AllianceMetadataAdapter.set_metadata('alliances', alliances)
            ok2 = AllianceMetadataAdapter.set_metadata('alliancesettings', settings)
            return bool(ok1 and ok2)
        except Exception:
            return False

    def _get_next_alliance_id(self) -> int:
        try:
            if mongo_enabled() and AllianceMetadataAdapter is not None:
                alliances = AllianceMetadataAdapter.get_metadata('alliances') or {}
                if not alliances:
                    return 1
                nums = [int(k) for k in alliances.keys() if str(k).isdigit()]
                return max(nums) + 1 if nums else 1
            # fallback to sqlite max(alliance_id)+1
            self.c.execute("SELECT MAX(alliance_id) FROM alliance_list")
            r = self.c.fetchone()
            if r and r[0]:
                return int(r[0]) + 1
            return 1
        except Exception:
            return 1

    def _member_count_for_alliance(self, alliance_id: int) -> int:
        try:
            if mongo_enabled() and AllianceMembersAdapter is not None:
                docs = AllianceMembersAdapter.get_all_members() or []
                return sum(1 for d in docs if str(d.get('alliance')) == str(alliance_id) or d.get('alliance') == alliance_id)
            # sqlite fallback
            self.c_users.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
            return int(self.c_users.fetchone()[0])
        except Exception:
            return 0

    async def _report_and_log_exception(self, interaction: discord.Interaction, exc: Exception, label: str = "general"):
        """Persist full traceback for debugging and notify the invoking user with a truncated traceback.

        Writes the full traceback to `logs/{label}_errors.log` next to the bot module and
        attempts to send a short ephemeral message back to the user containing the tail
        of the traceback so you can paste it here.
        """
        try:
            tb = traceback.format_exc()
        except Exception:
            tb = str(exc)

        logger.exception(f"Error in {label}: {exc}")

        # Ensure logs directory exists and append
        try:
            logs_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
            os.makedirs(logs_dir, exist_ok=True)
            log_path = os.path.normpath(os.path.join(logs_dir, f"{label}_errors.log"))
            with open(log_path, 'a', encoding='utf-8') as fh:
                fh.write(f"--- {datetime.utcnow().isoformat()}Z ---\n")
                fh.write(tb + '\n\n')
        except Exception:
            logger.exception(f"Failed to write {label} traceback to file")

        # Send truncated traceback back to the user (ephemeral)
        try:
            short_tb = tb[-1500:]
            msg = (
                f"An internal error occurred while opening {label.replace('_', ' ').title()}.\n"
                f"Traceback (truncated):\n{short_tb}\n\n"
                "Full traceback written to logs; please paste the truncated text into the issue report."
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            logger.exception(f"Failed to notify user about {label} error")

    def _create_table(self):
        self.c.execute("""
            CREATE TABLE IF NOT EXISTS alliance_list (
                alliance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                discord_server_id INTEGER
            )
        """)
        self.conn.commit()

        # Ensure alliancesettings exists in the alliance DB
        try:
            self.c.execute("""
                CREATE TABLE IF NOT EXISTS alliancesettings (
                    alliance_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    interval INTEGER
                )
            """)
            self.conn.commit()
        except Exception:
            logger.debug("Failed to ensure alliancesettings table in alliance DB", exc_info=True)

        # Ensure users table exists in users DB (used for member counts)
        try:
            self.c_users.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    fid INTEGER PRIMARY KEY,
                    nickname TEXT,
                    furnace_lv INTEGER DEFAULT 0,
                    kid INTEGER,
                    stove_lv_content TEXT,
                    alliance TEXT
                )
            """)
            self.conn_users.commit()
        except Exception:
            logger.debug("Failed to ensure users table in users DB", exc_info=True)

        # Ensure admin table exists in settings DB (used for permission checks)
        try:
            self.c_settings.execute("""
                CREATE TABLE IF NOT EXISTS admin (
                    id INTEGER PRIMARY KEY,
                    is_initial INTEGER
                )
            """)
            self.conn_settings.commit()
        except Exception:
            logger.debug("Failed to ensure admin table in settings DB", exc_info=True)

        # Ensure giftcode tables exist in giftcode DB
        try:
            self.c_giftcode.execute("""
                CREATE TABLE IF NOT EXISTS gift_codes (
                    giftcode TEXT PRIMARY KEY,
                    date TEXT,
                    validation_status TEXT DEFAULT 'pending'
                )
            """)
            self.c_giftcode.execute("""
                CREATE TABLE IF NOT EXISTS user_giftcodes (
                    fid INTEGER,
                    giftcode TEXT,
                    status TEXT,
                    PRIMARY KEY (fid, giftcode)
                )
            """)
            self.conn_giftcode.commit()
        except Exception:
            logger.debug("Failed to ensure giftcode tables", exc_info=True)

    def _check_and_add_column(self):
        self.c.execute("PRAGMA table_info(alliance_list)")
        columns = [info[1] for info in self.c.fetchall()]
        if "discord_server_id" not in columns:
            self.c.execute("ALTER TABLE alliance_list ADD COLUMN discord_server_id INTEGER")
            self.conn.commit()

    async def view_alliances(self, interaction: discord.Interaction):
        
        if interaction.guild is None:
            await interaction.response.send_message("❌ This command must be used in a server, not in DMs.", ephemeral=True)
            return

        user_id = interaction.user.id
        is_admin, is_initial = self._is_admin(user_id)
        if not is_admin:
            await interaction.response.send_message("You do not have permission to view alliances.", ephemeral=True)
            return
        guild_id = interaction.guild.id

        try:
            alliances = []
            if mongo_enabled():
                settings_map = self._load_alliancesettings_from_mongo()
                raw = self._load_alliances_from_mongo()
                if is_initial == 1:
                    alliances = [(aid, name, settings_map.get(aid, {}).get('interval', 0)) for (aid, name, _dsid) in raw]
                else:
                    alliances = [(aid, name, settings_map.get(aid, {}).get('interval', 0)) for (aid, name, dsid) in raw if dsid == guild_id]
            else:
                if is_initial == 1:
                    query = """
                        SELECT a.alliance_id, a.name, COALESCE(s.interval, 0) as interval
                        FROM alliance_list a
                        LEFT JOIN alliancesettings s ON a.alliance_id = s.alliance_id
                        ORDER BY a.alliance_id ASC
                    """
                    self.c.execute(query)
                else:
                    query = """
                        SELECT a.alliance_id, a.name, COALESCE(s.interval, 0) as interval
                        FROM alliance_list a
                        LEFT JOIN alliancesettings s ON a.alliance_id = s.alliance_id
                        WHERE a.discord_server_id = ?
                        ORDER BY a.alliance_id ASC
                    """
                    self.c.execute(query, (guild_id,))
                alliances = self.c.fetchall()

            alliance_list = ""
            for alliance_id, name, interval in alliances:
                
                member_count = self._member_count_for_alliance(alliance_id)
                
                interval_text = f"{interval} minutes" if interval > 0 else "No automatic control"
                alliance_list += f"🛡️ **{alliance_id}: {name}**\n👥 Members: {member_count}\n⏱️ Control Interval: {interval_text}\n\n"

            if not alliance_list:
                alliance_list = "No alliances found."

            embed = discord.Embed(
                title="Existing Alliances",
                description=alliance_list,
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(
                "An error occurred while fetching alliances.", 
                ephemeral=True
            )

    async def alliance_autocomplete(self, interaction: discord.Interaction, current: str):
        if mongo_enabled() and AllianceMetadataAdapter is not None:
            raw = self._load_alliances_from_mongo()
            alliances = [(aid, name) for (aid, name, _dsid) in raw]
        else:
            self.c.execute("SELECT alliance_id, name FROM alliance_list")
            alliances = self.c.fetchall()

        return [
            app_commands.Choice(name=f"{name} (ID: {alliance_id})", value=str(alliance_id))
            for alliance_id, name in alliances if current.lower() in (name or '').lower()
        ][:25]

    @app_commands.command(name="settings", description="Open settings menu.")
    async def settings(self, interaction: discord.Interaction):
        try:
            if interaction.guild is not None: # Check bot permissions only if in a guild
                perm_check = interaction.guild.get_member(interaction.client.user.id)
                if not perm_check.guild_permissions.administrator:
                    await interaction.response.send_message(
                        "Beeb boop 🤖 I need **Administrator** permissions to function. "
                        "Go to server settings --> Roles --> find my role --> scroll down and turn on Administrator", 
                        ephemeral=True
                    )
                    return
                
            admin_count = self._get_admin_count()

            user_id = interaction.user.id

            if admin_count == 0:
                # Add the invoking user as the initial/global admin (persisted to Mongo if enabled)
                added = self._add_admin(user_id, 1)
                first_use_embed = discord.Embed(
                    title="🎉 First Time Setup",
                    description=(
                        "This command has been used for the first time and no administrators were found.\n\n"
                        f"**{interaction.user.name}** has been added as the Global Administrator.\n\n"
                        "You can now access all administrative functions."
                    ),
                    color=discord.Color.green()
                )
                # Notify regardless of persistence success (best-effort)
                await interaction.response.send_message(embed=first_use_embed, ephemeral=True)
                await asyncio.sleep(3)

            is_admin, is_initial = self._is_admin(user_id)
            if not is_admin:
                await interaction.response.send_message(
                    "You do not have permission to access this menu.", 
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="⚙️ Settings Menu",
                description=(
                    "Please select a category:\n\n"
                    "**Menu Categories**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🏰 **Alliance Operations**\n"
                    "└ Manage alliances and settings\n\n"
                    "👥 **Alliance Member Operations**\n"
                    "└ Add, remove, and view members\n\n"
                    "🤖 **Bot Operations**\n"
                    "└ Configure bot settings\n\n"
                    "🎁 **Gift Code Operations**\n"
                    "└ Manage gift codes and rewards\n\n"
                    "📜 **Alliance History**\n"
                    "└ View alliance changes and history\n\n"
                    "🆘 **Support Operations**\n"
                    "└ Access support features\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.blue()
            )
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Alliance Operations",
                emoji="🏰",
                style=discord.ButtonStyle.primary,
                custom_id="alliance_operations",
                row=0
            ))
            view.add_item(discord.ui.Button(
                label="Member Operations",
                emoji="👥",
                style=discord.ButtonStyle.primary,
                custom_id="member_operations",
                row=0
            ))
            view.add_item(discord.ui.Button(
                label="Bot Operations",
                emoji="🤖",
                style=discord.ButtonStyle.primary,
                custom_id="bot_operations",
                row=1
            ))
            view.add_item(discord.ui.Button(
                label="Gift Operations",
                emoji="🎁",
                style=discord.ButtonStyle.primary,
                custom_id="gift_code_operations",
                row=1
            ))
            view.add_item(discord.ui.Button(
                label="Alliance History",
                emoji="📜",
                style=discord.ButtonStyle.primary,
                custom_id="alliance_history",
                row=2
            ))
            view.add_item(discord.ui.Button(
                label="Support Operations",
                emoji="🆘",
                style=discord.ButtonStyle.primary,
                custom_id="support_operations",
                row=2
            ))
            view.add_item(discord.ui.Button(
                label="Other Features",
                emoji="🔧",
                style=discord.ButtonStyle.primary,
                custom_id="other_features",
                row=3
            ))

            if admin_count == 0:
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await interaction.response.send_message(embed=embed, view=view)

        except Exception as e:
            if not any(error_code in str(e) for error_code in ["10062", "40060"]):
                print(f"Settings command error: {e}")
            error_message = "An error occurred while processing your request."
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(error_message, ephemeral=True)
                else:
                    await interaction.followup.send(error_message, ephemeral=True)
            except Exception:
                # Fallback: sometimes the library thinks the response is not done
                # while Discord already marked the interaction as acknowledged
                # (HTTP 40060 / Unknown interaction). In that case, try followup.
                try:
                    await interaction.followup.send(error_message, ephemeral=True)
                except Exception:
                    logger.exception("Failed to send error message for /settings")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            # If this interaction is a simple arrow (⬅️/➡️) emoji button used for
            # pagination elsewhere, ignore it here so the view that owns the
            # message can handle it. This prevents sending an unnecessary
            # "You do not have permission" message when arrow buttons are
            # clicked by non-admin users.
            try:
                data = interaction.data or {}
                emoji = data.get('emoji')
                if emoji and isinstance(emoji, dict):
                    name = emoji.get('name')
                    if name in ('⬅️', '➡️', '⬅', '➡'):
                        return
            except Exception:
                pass

            custom_id = interaction.data.get("custom_id")
            user_id = interaction.user.id
            is_admin, is_initial = self._is_admin(user_id)

            if not is_admin:
                # Interaction may already be acknowledged elsewhere; try response first,
                # then fallback to followup to avoid HTTP 40060 (already acknowledged).
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("You do not have permission to perform this action.", ephemeral=True)
                    else:
                        await interaction.followup.send("You do not have permission to perform this action.", ephemeral=True)
                except Exception:
                    try:
                        await interaction.followup.send("You do not have permission to perform this action.", ephemeral=True)
                    except Exception:
                        logger.exception("Failed to send permission denial for interaction")
                return

            try:
                if custom_id == "alliance_operations":
                    embed = discord.Embed(
                        title="🏰 Alliance Operations",
                        description=(
                            "Please select an operation:\n\n"
                            "**Available Operations**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "➕ **Add Alliance**\n"
                            "└ Create a new alliance\n\n"
                            "✏️ **Edit Alliance**\n"
                            "└ Modify existing alliance settings\n\n"
                            "🗑️ **Delete Alliance**\n"
                            "└ Remove an existing alliance\n\n"
                            "👀 **View Alliances**\n"
                            "└ List all available alliances\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                        ),
                        color=discord.Color.blue()
                    )
                    
                    view = discord.ui.View()
                    view.add_item(discord.ui.Button(
                        label="Add Alliance", 
                        emoji="➕",
                        style=discord.ButtonStyle.success, 
                        custom_id="add_alliance", 
                        disabled=is_initial != 1
                    ))
                    view.add_item(discord.ui.Button(
                        label="Edit Alliance", 
                        emoji="✏️",
                        style=discord.ButtonStyle.primary, 
                        custom_id="edit_alliance", 
                        disabled=is_initial != 1
                    ))
                    view.add_item(discord.ui.Button(
                        label="Delete Alliance", 
                        emoji="🗑️",
                        style=discord.ButtonStyle.danger, 
                        custom_id="delete_alliance", 
                        disabled=is_initial != 1
                    ))
                    view.add_item(discord.ui.Button(
                        label="View Alliances", 
                        emoji="👀",
                        style=discord.ButtonStyle.primary, 
                        custom_id="view_alliances"
                    ))
                    view.add_item(discord.ui.Button(
                        label="Check Alliance", 
                        emoji="🔍",
                        style=discord.ButtonStyle.primary, 
                        custom_id="check_alliance"
                    ))
                    view.add_item(discord.ui.Button(
                        label="Main Menu", 
                        emoji="🏠",
                        style=discord.ButtonStyle.secondary, 
                        custom_id="main_menu"
                    ))

                    await interaction.response.edit_message(embed=embed, view=view)

                elif custom_id == "edit_alliance":
                    if is_initial != 1:
                        await interaction.response.send_message("You do not have permission to perform this action.", ephemeral=True)
                        return
                    await self.edit_alliance(interaction)

                elif custom_id == "check_alliance":
                    if mongo_enabled() and AllianceMetadataAdapter is not None:
                        settings_map = self._load_alliancesettings_from_mongo()
                        raw = self._load_alliances_from_mongo()
                        alliances = [(aid, name, settings_map.get(aid, {}).get('interval', 0)) for (aid, name, _dsid) in raw]
                    else:
                        self.c.execute("""
                            SELECT a.alliance_id, a.name, COALESCE(s.interval, 0) as interval
                            FROM alliance_list a
                            LEFT JOIN alliancesettings s ON a.alliance_id = s.alliance_id
                            ORDER BY a.name
                        """)
                        alliances = self.c.fetchall()

                    if not alliances:
                        await interaction.response.send_message("No alliances found to check.", ephemeral=True)
                        return

                    options = [
                        discord.SelectOption(
                            label="Check All Alliances",
                            value="all",
                            description="Start control process for all alliances",
                            emoji="🔄"
                        )
                    ]
                    
                    options.extend([
                        discord.SelectOption(
                            label=f"{name[:40]}",
                            value=str(alliance_id),
                            description=f"Control Interval: {interval} minutes"
                        ) for alliance_id, name, interval in alliances
                    ])

                    select = discord.ui.Select(
                        placeholder="Select an alliance to check",
                        options=options,
                        custom_id="alliance_check_select"
                    )

                    async def alliance_check_callback(select_interaction: discord.Interaction):
                        try:
                            selected_value = select_interaction.data["values"][0]
                            control_cog = self.bot.get_cog('Control')
                            
                            if not control_cog:
                                await select_interaction.response.send_message("Control module not found.", ephemeral=True)
                                return
                            
                            # Ensure the centralized queue processor is running
                            await control_cog.login_handler.start_queue_processor()
                            
                            if selected_value == "all":
                                progress_embed = discord.Embed(
                                    title="🔄 Alliance Control Queue",
                                    description=(
                                        "**Control Queue Information**\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 **Total Alliances:** `{len(alliances)}`\n"
                                        "🔄 **Status:** `Adding alliances to control queue...`\n"
                                        "⏰ **Queue Start:** `Now`\n"
                                        "⚠️ **Note:** `Each alliance will be processed in sequence`\n"
                                        "⏱️ **Wait Time:** `1 minute between each alliance control`\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                        "⌛ Please wait while alliances are being processed..."
                                    ),
                                    color=discord.Color.blue()
                                )
                                await select_interaction.response.send_message(embed=progress_embed)
                                msg = await select_interaction.original_response()
                                message_id = msg.id

                                # Queue all alliance operations at once
                                queued_alliances = []
                                settings_map = self._load_alliancesettings_from_mongo() if mongo_enabled() else None
                                for index, (alliance_id, name, _) in enumerate(alliances):
                                    try:
                                        if settings_map is not None:
                                            ch_id = settings_map.get(alliance_id, {}).get('channel_id')
                                            channel = self.bot.get_channel(ch_id) if ch_id else select_interaction.channel
                                        else:
                                            self.c.execute("""
                                                SELECT channel_id FROM alliancesettings WHERE alliance_id = ?
                                            """, (alliance_id,))
                                            channel_data = self.c.fetchone()
                                            channel = self.bot.get_channel(channel_data[0]) if channel_data else select_interaction.channel
                                        
                                        await control_cog.login_handler.queue_operation({
                                            'type': 'alliance_control',
                                            'callback': lambda ch=channel, aid=alliance_id, inter=select_interaction: control_cog.check_agslist(ch, aid, interaction=inter),
                                            'description': f'Manual control check for alliance {name}',
                                            'alliance_id': alliance_id,
                                            'interaction': select_interaction
                                        })
                                        queued_alliances.append((alliance_id, name))
                                    
                                    except Exception as e:
                                        print(f"Error queuing alliance {name}: {e}")
                                        continue
                                
                                # Update status to show all alliances have been queued
                                queue_status_embed = discord.Embed(
                                    title="🔄 Alliance Control Queue",
                                    description=(
                                        "**Control Queue Information**\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 **Total Alliances Queued:** `{len(queued_alliances)}`\n"
                                        f"⏰ **Queue Start:** <t:{int(datetime.now().timestamp())}:R>\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                        "⌛ All alliance controls have been queued and will process in order..."
                                    ),
                                    color=discord.Color.blue()
                                )
                                channel = select_interaction.channel
                                msg = await channel.fetch_message(message_id)
                                await msg.edit(embed=queue_status_embed)
                                
                                # Monitor queue completion
                                start_time = datetime.now()
                                while True:
                                    queue_info = control_cog.login_handler.get_queue_info()
                                    
                                    # Check if all our operations are done
                                    if queue_info['queue_size'] == 0 and queue_info['current_operation'] is None:
                                        # Double-check by waiting a moment
                                        await asyncio.sleep(2)
                                        queue_info = control_cog.login_handler.get_queue_info()
                                        if queue_info['queue_size'] == 0 and queue_info['current_operation'] is None:
                                            break
                                    
                                    # Update status periodically
                                    if queue_info['current_operation'] and queue_info['current_operation'].get('type') == 'alliance_control':
                                        current_alliance_id = queue_info['current_operation'].get('alliance_id')
                                        current_name = next((name for aid, name in queued_alliances if aid == current_alliance_id), "Unknown")
                                        
                                        update_embed = discord.Embed(
                                            title="🔄 Alliance Control Queue",
                                            description=(
                                                "**Control Queue Information**\n"
                                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                                f"📊 **Total Alliances:** `{len(queued_alliances)}`\n"
                                                f"🔄 **Currently Processing:** `{current_name}`\n"
                                                f"📈 **Queue Remaining:** `{queue_info['queue_size']}`\n"
                                                f"⏰ **Started:** <t:{int(start_time.timestamp())}:R>\n"
                                                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                                "⌛ Processing controls..."
                                            ),
                                            color=discord.Color.blue()
                                        )
                                        await msg.edit(embed=update_embed)
                                    
                                    await asyncio.sleep(5)  # Check every 5 seconds
                                
                                # All operations complete
                                queue_complete_embed = discord.Embed(
                                    title="✅ Alliance Control Queue Complete",
                                    description=(
                                        "**Queue Status Information**\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 **Total Alliances Processed:** `{len(queued_alliances)}`\n"
                                        "🔄 **Status:** `All controls completed`\n"
                                        f"⏰ **Completion Time:** <t:{int(datetime.now().timestamp())}:R>\n"
                                        f"⏱️ **Total Duration:** `{int((datetime.now() - start_time).total_seconds())} seconds`\n"
                                        "📝 **Note:** `Control results have been shared in respective channels`\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━"
                                    ),
                                    color=discord.Color.green()
                                )
                                await msg.edit(embed=queue_complete_embed)
                            
                            else:
                                alliance_id = int(selected_value)
                                if mongo_enabled():
                                    alliances_map = AllianceMetadataAdapter.get_metadata('alliances') or {}
                                    settings_map = self._load_alliancesettings_from_mongo()
                                    a_doc = alliances_map.get(str(alliance_id))
                                    if not a_doc:
                                        await select_interaction.response.send_message("Alliance not found.", ephemeral=True)
                                        return
                                    alliance_name = a_doc.get('name')
                                    channel_id = settings_map.get(alliance_id, {}).get('channel_id')
                                    channel = self.bot.get_channel(channel_id) if channel_id else select_interaction.channel
                                else:
                                    self.c.execute("""
                                        SELECT a.name, s.channel_id 
                                        FROM alliance_list a
                                        LEFT JOIN alliancesettings s ON a.alliance_id = s.alliance_id
                                        WHERE a.alliance_id = ?
                                    """, (alliance_id,))
                                    alliance_data = self.c.fetchone()

                                    if not alliance_data:
                                        await select_interaction.response.send_message("Alliance not found.", ephemeral=True)
                                        return

                                    alliance_name, channel_id = alliance_data
                                    channel = self.bot.get_channel(channel_id) if channel_id else select_interaction.channel
                                
                                status_embed = discord.Embed(
                                    title="🔍 Alliance Control",
                                    description=(
                                        "**Control Information**\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 **Alliance:** `{alliance_name}`\n"
                                        f"🔄 **Status:** `Queued`\n"
                                        f"⏰ **Queue Time:** `Now`\n"
                                        f"📢 **Results Channel:** `{channel.name if channel else 'Designated channel'}`\n"
                                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                        "⏳ Alliance control will begin shortly..."
                                    ),
                                    color=discord.Color.blue()
                                )
                                await select_interaction.response.send_message(embed=status_embed)
                                
                                await control_cog.login_handler.queue_operation({
                                    'type': 'alliance_control',
                                    'callback': lambda ch=channel, aid=alliance_id: control_cog.check_agslist(ch, aid),
                                    'description': f'Manual control check for alliance {alliance_name}',
                                    'alliance_id': alliance_id
                                })

                        except Exception as e:
                            print(f"Alliance check error: {e}")
                            await select_interaction.response.send_message(
                                "An error occurred during the control process.", 
                                ephemeral=True
                            )

                    select.callback = alliance_check_callback
                    view = discord.ui.View()
                    view.add_item(select)

                    embed = discord.Embed(
                        title="🔍 Alliance Control",
                        description=(
                            "Please select an alliance to check:\n\n"
                            "**Information**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "• Select 'Check All Alliances' to process all alliances\n"
                            "• Control process may take a few minutes\n"
                            "• Results will be shared in the designated channel\n"
                            "• Other controls will be queued during the process\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                        ),
                        color=discord.Color.blue()
                    )
                    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

                elif custom_id == "member_operations":
                    try:
                        member_cog = self.bot.get_cog("AllianceMemberOperations")
                        if member_cog:
                            await member_cog.handle_member_operations(interaction)
                        else:
                            await interaction.response.send_message("❌ Alliance Member Operations module not found.", ephemeral=True)
                    except Exception as e:
                        await self._report_and_log_exception(interaction, e, label="alliance_member_operations")

                elif custom_id == "bot_operations":
                    try:
                        bot_ops_cog = interaction.client.get_cog("BotOperations")
                        if bot_ops_cog:
                            try:
                                await bot_ops_cog.show_bot_operations_menu(interaction)
                            except Exception as inner_e:
                                await self._report_and_log_exception(interaction, inner_e, label="bot_operations")
                        else:
                            await interaction.response.send_message(
                                "❌ Bot Operations module not found.",
                                ephemeral=True
                            )
                    except Exception as e:
                        await self._report_and_log_exception(interaction, e, label="bot_operations")

                elif custom_id == "gift_code_operations":
                    try:
                        gift_ops_cog = interaction.client.get_cog("GiftOperations")
                        if gift_ops_cog:
                                try:
                                    await gift_ops_cog.show_gift_menu(interaction)
                                except Exception as inner_e:
                                    # Capture the full traceback
                                    traceback_str = traceback.format_exc()
                                    print(f"Gift operations inner exception: {inner_e}\n{traceback_str}")
                                    logger.exception("Error while executing GiftOperations.show_gift_menu")

                                    # Ensure logs directory exists and append the traceback for later inspection
                                    try:
                                        logs_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
                                        os.makedirs(logs_dir, exist_ok=True)
                                        log_path = os.path.normpath(os.path.join(logs_dir, 'gift_ops_errors.log'))
                                        with open(log_path, 'a', encoding='utf-8') as fh:
                                            fh.write(f"--- {datetime.utcnow().isoformat()}Z ---\n")
                                            fh.write(traceback_str + '\n\n')
                                    except Exception:
                                        logger.exception("Failed to write gift operations traceback to file")

                                    # Send a truncated traceback back to the user (ephemeral) to help debugging
                                    short_tb = traceback_str[-1500:]
                                    message_text = (
                                        "An internal error occurred while opening Gift Operations. "
                                        "I've captured the error for debugging and appended it to logs/gift_ops_errors.log.\n\n"
                                        "Traceback (truncated):\n"
                                        f"{short_tb}"
                                    )
                                    try:
                                        if not interaction.response.is_done():
                                            await interaction.response.send_message(message_text, ephemeral=True)
                                        else:
                                            await interaction.followup.send(message_text, ephemeral=True)
                                    except Exception:
                                        logger.exception("Failed to notify user about gift operations inner exception")
                        else:
                            await interaction.response.send_message(
                                "❌ Gift Operations module not found.",
                                ephemeral=True
                            )
                    except Exception as e:
                            logger.exception(f"Gift operations error when attempting to get cog or call show_gift_menu: {e}")
                            # Best-effort user message
                            try:
                                if not interaction.response.is_done():
                                    await interaction.response.send_message(
                                        "An error occurred while loading Gift Operations.",
                                        ephemeral=True
                                    )
                                else:
                                    await interaction.followup.send(
                                        "An error occurred while loading Gift Operations.",
                                        ephemeral=True
                                    )
                            except Exception:
                                logger.exception("Failed to send error response for Gift Operations")

                elif custom_id == "add_alliance":
                    if is_initial != 1:
                        await interaction.response.send_message("You do not have permission to perform this action.", ephemeral=True)
                        return
                    await self.add_alliance(interaction)

                elif custom_id == "delete_alliance":
                    if is_initial != 1:
                        await interaction.response.send_message("You do not have permission to perform this action.", ephemeral=True)
                        return
                    await self.delete_alliance(interaction)

                elif custom_id == "view_alliances":
                    await self.view_alliances(interaction)

                elif custom_id == "support_operations":
                    try:
                        support_ops_cog = interaction.client.get_cog("SupportOperations")
                        if support_ops_cog:
                            try:
                                await support_ops_cog.show_support_menu(interaction)
                            except Exception as inner_e:
                                await self._report_and_log_exception(interaction, inner_e, label="support_operations")
                        else:
                            await interaction.response.send_message(
                                "❌ Support Operations module not found.",
                                ephemeral=True
                            )
                    except Exception as e:
                        await self._report_and_log_exception(interaction, e, label="support_operations")

                elif custom_id == "alliance_history":
                    try:
                        changes_cog = interaction.client.get_cog("Changes")
                        if changes_cog:
                            try:
                                await changes_cog.show_alliance_history_menu(interaction)
                            except Exception as inner_e:
                                await self._report_and_log_exception(interaction, inner_e, label="alliance_history")
                        else:
                            await interaction.response.send_message(
                                "❌ Alliance History module not found.",
                                ephemeral=True
                            )
                    except Exception as e:
                        await self._report_and_log_exception(interaction, e, label="alliance_history")

                elif custom_id == "other_features":
                    try:
                        other_features_cog = interaction.client.get_cog("OtherFeatures")
                        if other_features_cog:
                            try:
                                await other_features_cog.show_other_features_menu(interaction)
                            except Exception as inner_e:
                                await self._report_and_log_exception(interaction, inner_e, label="other_features")
                        else:
                            await interaction.response.send_message(
                                "❌ Other Features module not found.",
                                ephemeral=True
                            )
                    except Exception as e:
                        await self._report_and_log_exception(interaction, e, label="other_features")

            except Exception as e:
                # Avoid spamming additional interaction responses when Discord
                # reports the interaction is already acknowledged or unknown
                if not any(error_code in str(e) for error_code in ["10062", "40060"]):
                    print(f"Error processing interaction with custom_id '{custom_id}': {e}")

                # Safely notify the user: prefer response if not done, otherwise use followup.
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            "An error occurred while processing your request. Please try again.",
                            ephemeral=True
                        )
                    else:
                        await interaction.followup.send(
                            "An error occurred while processing your request. Please try again.",
                            ephemeral=True
                        )
                except Exception:
                    # Best-effort: if both response and followup fail, log and move on
                    logger.exception("Failed to send error response for interaction")

    async def add_alliance(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Please perform this action in a Discord channel.", ephemeral=True)
            return

        modal = AllianceModal(title="Add Alliance")
        await interaction.response.send_modal(modal)
        await modal.wait()

        try:
            alliance_name = modal.name.value.strip()
            interval = int(modal.interval.value.strip())

            embed = discord.Embed(
                title="Channel Selection",
                description=(
                    "**Instructions:**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Please select a channel for the alliance\n\n"
                    "**Page:** 1/1\n"
                    f"**Total Channels:** {len(interaction.guild.text_channels)}"
                ),
                color=discord.Color.blue()
            )

                async def channel_select_callback(select_interaction: discord.Interaction):
                    try:
                        # Check if alliance already exists (Mongo first, then sqlite)
                        existing_alliance = None
                        if mongo_enabled() and AllianceMetadataAdapter is not None:
                            alliances_map = AllianceMetadataAdapter.get_metadata('alliances') or {}
                            if any(v.get('name') == alliance_name for v in alliances_map.values()):
                                existing_alliance = True
                        else:
                            self.c.execute("SELECT alliance_id FROM alliance_list WHERE name = ?", (alliance_name,))
                            existing_alliance = self.c.fetchone()

                        if existing_alliance:
                            error_embed = discord.Embed(
                                title="Error",
                                description="An alliance with this name already exists.",
                                color=discord.Color.red()
                            )
                            await select_interaction.response.edit_message(embed=error_embed, view=None)
                            return

                        channel_id = int(select_interaction.data["values"][0])

                        # Save alliance to Mongo or SQLite
                        if mongo_enabled() and AllianceMetadataAdapter is not None:
                            alliance_id = self._get_next_alliance_id()
                            ok1 = self._save_alliance_to_mongo(alliance_id, alliance_name, interaction.guild.id)
                            ok2 = self._save_alliancesettings_to_mongo(alliance_id, channel_id, interval)
                            if not (ok1 and ok2):
                                raise Exception('Failed to save alliance to MongoDB')
                        else:
                            self.c.execute("INSERT INTO alliance_list (name, discord_server_id) VALUES (?, ?)", 
                                           (alliance_name, interaction.guild.id))
                            alliance_id = self.c.lastrowid
                            self.c.execute("INSERT INTO alliancesettings (alliance_id, channel_id, interval) VALUES (?, ?, ?)", 
                                           (alliance_id, channel_id, interval))
                            self.conn.commit()

                        # Legacy giftcodecontrol table (still in SQLite for now)
                        try:
                            self.c_giftcode.execute("""
                                INSERT INTO giftcodecontrol (alliance_id, status) 
                                VALUES (?, 1)
                            """, (alliance_id,))
                            self.conn_giftcode.commit()
                        except Exception:
                            # not critical; ignore if table missing or insert fails
                            pass

                        result_embed = discord.Embed(
                            title="✅ Alliance Successfully Created",
                            description="The alliance has been created with the following details:",
                            color=discord.Color.green()
                        )
                        
                        info_section = (
                            f"**🛡️ Alliance Name**\n{alliance_name}\n\n"
                            f"**🔢 Alliance ID**\n{alliance_id}\n\n"
                            f"**📢 Channel**\n<#{channel_id}>\n\n"
                            f"**⏱️ Control Interval**\n{interval} minutes"
                        )
                        result_embed.add_field(name="Alliance Details", value=info_section, inline=False)
                        
                        result_embed.set_footer(text="Alliance settings have been successfully saved")
                        result_embed.timestamp = discord.utils.utcnow()
                        
                        await select_interaction.response.edit_message(embed=result_embed, view=None)

                    except Exception as e:
                        error_embed = discord.Embed(
                            title="Error",
                            description=f"Error creating alliance: {str(e)}",
                            color=discord.Color.red()
                        )
                        try:
                            await select_interaction.response.edit_message(embed=error_embed, view=None)
                        except Exception:
                            # fallback to responding
                            try:
                                await select_interaction.response.send_message(embed=error_embed, ephemeral=True)
                            except Exception:
                                logger.exception("Failed to notify user about alliance creation error")

            channels = interaction.guild.text_channels
            view = PaginatedChannelView(channels, channel_select_callback)
            await modal.interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except ValueError:
            error_embed = discord.Embed(
                title="Error",
                description="Invalid interval value. Please enter a number.",
                color=discord.Color.red()
            )
            await modal.interaction.response.send_message(embed=error_embed, ephemeral=True)
        except Exception as e:
            error_embed = discord.Embed(
                title="Error",
                description=f"Error: {str(e)}",
                color=discord.Color.red()
            )
            await modal.interaction.response.send_message(embed=error_embed, ephemeral=True)

    async def edit_alliance(self, interaction: discord.Interaction):
        if mongo_enabled() and AllianceMetadataAdapter is not None:
            settings_map = self._load_alliancesettings_from_mongo()
            alliances = [(aid, name, settings_map.get(aid, {}).get('interval', 0), settings_map.get(aid, {}).get('channel_id', 0)) for aid, name, _ in self._load_alliances_from_mongo()]
        else:
            self.c.execute("""
                SELECT a.alliance_id, a.name, COALESCE(s.interval, 0) as interval, COALESCE(s.channel_id, 0) as channel_id 
                FROM alliance_list a 
                LEFT JOIN alliancesettings s ON a.alliance_id = s.alliance_id
                ORDER BY a.alliance_id ASC
            """)
            alliances = self.c.fetchall()
        
        if not alliances:
            no_alliance_embed = discord.Embed(
                title="❌ No Alliances Found",
                description=(
                    "There are no alliances registered in the database.\n"
                    "Please create an alliance first using the `/alliance create` command."
                ),
                color=discord.Color.red()
            )
            no_alliance_embed.set_footer(text="Use /alliance create to add a new alliance")
            return await interaction.response.send_message(embed=no_alliance_embed, ephemeral=True)

        alliance_options = [
            discord.SelectOption(
                label=f"{name} (ID: {alliance_id})",
                value=f"{alliance_id}",
                description=f"Interval: {interval} minutes"
            ) for alliance_id, name, interval, _ in alliances
        ]
        
        items_per_page = 25
        option_pages = [alliance_options[i:i + items_per_page] for i in range(0, len(alliance_options), items_per_page)]
        total_pages = len(option_pages)

        class PaginatedAllianceView(discord.ui.View):
            def __init__(self, pages, original_callback):
                super().__init__(timeout=7200)
                self.current_page = 0
                self.pages = pages
                self.original_callback = original_callback
                self.total_pages = len(pages)
                self.update_view()

            def update_view(self):
                self.clear_items()
                
                select = discord.ui.Select(
                    placeholder=f"Select alliance ({self.current_page + 1}/{self.total_pages})",
                    options=self.pages[self.current_page]
                )
                select.callback = self.original_callback
                self.add_item(select)
                
                previous_button = discord.ui.Button(
                    label="◀️",
                    style=discord.ButtonStyle.grey,
                    custom_id="previous",
                    disabled=(self.current_page == 0)
                )
                previous_button.callback = self.previous_callback
                self.add_item(previous_button)

                next_button = discord.ui.Button(
                    label="▶️",
                    style=discord.ButtonStyle.grey,
                    custom_id="next",
                    disabled=(self.current_page == len(self.pages) - 1)
                )
                next_button.callback = self.next_callback
                self.add_item(next_button)

            async def previous_callback(self, interaction: discord.Interaction):
                self.current_page = (self.current_page - 1) % len(self.pages)
                self.update_view()
                
                embed = interaction.message.embeds[0]
                embed.description = (
                    "**Instructions:**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "1️⃣ Select an alliance from the dropdown menu\n"
                    "2️⃣ Use ◀️ ▶️ buttons to navigate between pages\n\n"
                    f"**Current Page:** {self.current_page + 1}/{self.total_pages}\n"
                    f"**Total Alliances:** {sum(len(page) for page in self.pages)}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                )
                await interaction.response.edit_message(embed=embed, view=self)

            async def next_callback(self, interaction: discord.Interaction):
                self.current_page = (self.current_page + 1) % len(self.pages)
                self.update_view()
                
                embed = interaction.message.embeds[0]
                embed.description = (
                    "**Instructions:**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "1️⃣ Select an alliance from the dropdown menu\n"
                    "2️⃣ Use ◀️ ▶️ buttons to navigate between pages\n\n"
                    f"**Current Page:** {self.current_page + 1}/{self.total_pages}\n"
                    f"**Total Alliances:** {sum(len(page) for page in self.pages)}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                )
                await interaction.response.edit_message(embed=embed, view=self)

        async def select_callback(select_interaction: discord.Interaction):
                try:
                    alliance_id = int(select_interaction.data["values"][0])
                    alliance_data = next(a for a in alliances if a[0] == alliance_id)

                    if mongo_enabled() and AllianceMetadataAdapter is not None:
                        settings_map = self._load_alliancesettings_from_mongo()
                        settings_data = (settings_map.get(alliance_id, {}).get('interval', 0), settings_map.get(alliance_id, {}).get('channel_id', 0))
                    else:
                        self.c.execute("""
                            SELECT interval, channel_id 
                            FROM alliancesettings 
                            WHERE alliance_id = ?
                        """, (alliance_id,))
                        settings_data = self.c.fetchone()

                    modal = AllianceModal(
                        title="Edit Alliance",
                        default_name=alliance_data[1],
                        default_interval=str(settings_data[0] if settings_data else 0)
                    )
                await select_interaction.response.send_modal(modal)
                await modal.wait()

                try:
                    alliance_name = modal.name.value.strip()
                    interval = int(modal.interval.value.strip())

                    embed = discord.Embed(
                        title="🔄 Channel Selection",
                        description=(
                            "**Current Channel Information**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📢 Current channel: {f'<#{settings_data[1]}>' if settings_data else 'Not set'}\n"
                            "**Page:** 1/1\n"
                            f"**Total Channels:** {len(interaction.guild.text_channels)}\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                        ),
                        color=discord.Color.blue()
                    )

                    async def channel_select_callback(channel_interaction: discord.Interaction):
                        try:
                            channel_id = int(channel_interaction.data["values"][0])

                            if mongo_enabled() and AllianceMetadataAdapter is not None:
                                # Update name and settings in Mongo
                                ok1 = self._save_alliance_to_mongo(alliance_id, alliance_name, interaction.guild.id)
                                ok2 = self._save_alliancesettings_to_mongo(alliance_id, channel_id, interval)
                                if not (ok1 and ok2):
                                    raise Exception('Failed to update alliance in MongoDB')
                            else:
                                self.c.execute("UPDATE alliance_list SET name = ? WHERE alliance_id = ?", 
                                              (alliance_name, alliance_id))
                                
                                if settings_data:
                                    self.c.execute("""
                                        UPDATE alliancesettings 
                                        SET channel_id = ?, interval = ? 
                                        WHERE alliance_id = ?
                                    """, (channel_id, interval, alliance_id))
                                else:
                                    self.c.execute("""
                                        INSERT INTO alliancesettings (alliance_id, channel_id, interval)
                                        VALUES (?, ?, ?)
                                    """, (alliance_id, channel_id, interval))
                                
                                self.conn.commit()

                            result_embed = discord.Embed(
                                title="✅ Alliance Successfully Updated",
                                description="The alliance details have been updated as follows:",
                                color=discord.Color.green()
                            )
                            
                            info_section = (
                                f"**🛡️ Alliance Name**\n{alliance_name}\n\n"
                                f"**🔢 Alliance ID**\n{alliance_id}\n\n"
                                f"**📢 Channel**\n<#{channel_id}>\n\n"
                                f"**⏱️ Control Interval**\n{interval} minutes"
                            )
                            result_embed.add_field(name="Alliance Details", value=info_section, inline=False)
                            
                            result_embed.set_footer(text="Alliance settings have been successfully saved")
                            result_embed.timestamp = discord.utils.utcnow()
                            
                            await channel_interaction.response.edit_message(embed=result_embed, view=None)

                        except Exception as e:
                            error_embed = discord.Embed(
                                title="❌ Error",
                                description=f"An error occurred while updating the alliance: {str(e)}",
                                color=discord.Color.red()
                            )
                            await channel_interaction.response.edit_message(embed=error_embed, view=None)

                    channels = interaction.guild.text_channels
                    view = PaginatedChannelView(channels, channel_select_callback)
                    await modal.interaction.response.send_message(embed=embed, view=view, ephemeral=True)

                except ValueError:
                    error_embed = discord.Embed(
                        title="Error",
                        description="Invalid interval value. Please enter a number.",
                        color=discord.Color.red()
                    )
                    await modal.interaction.response.send_message(embed=error_embed, ephemeral=True)
                except Exception as e:
                    error_embed = discord.Embed(
                        title="Error",
                        description=f"Error: {str(e)}",
                        color=discord.Color.red()
                    )
                    await modal.interaction.response.send_message(embed=error_embed, ephemeral=True)

            except Exception as e:
                error_embed = discord.Embed(
                    title="❌ Error",
                    description=f"An error occurred: {str(e)}",
                    color=discord.Color.red()
                )
                if not select_interaction.response.is_done():
                    await select_interaction.response.send_message(embed=error_embed, ephemeral=True)
                else:
                    await select_interaction.followup.send(embed=error_embed, ephemeral=True)

        view = PaginatedAllianceView(option_pages, select_callback)
        embed = discord.Embed(
            title="🛡️ Alliance Edit Menu",
            description=(
                "**Instructions:**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ Select an alliance from the dropdown menu\n"
                "2️⃣ Use ◀️ ▶️ buttons to navigate between pages\n\n"
                f"**Current Page:** {1}/{total_pages}\n"
                f"**Total Alliances:** {len(alliances)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.blue()
        )
        embed.set_footer(text="Use the dropdown menu below to select an alliance")
        embed.timestamp = discord.utils.utcnow()
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def delete_alliance(self, interaction: discord.Interaction):
        try:
            if mongo_enabled() and AllianceMetadataAdapter is not None:
                raw = self._load_alliances_from_mongo()
                alliances = [(aid, name) for (aid, name, _dsid) in raw]
            else:
                self.c.execute("SELECT alliance_id, name FROM alliance_list ORDER BY name")
                alliances = self.c.fetchall()

            if not alliances:
                no_alliance_embed = discord.Embed(
                    title="❌ No Alliances Found",
                    description="There are no alliances to delete.",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=no_alliance_embed, ephemeral=True)
                return

            alliance_members = {}
            for alliance_id, _ in alliances:
                member_count = self._member_count_for_alliance(alliance_id)
                alliance_members[alliance_id] = member_count

            items_per_page = 25
            all_options = [
                discord.SelectOption(
                    label=f"{name[:40]} (ID: {alliance_id})",
                    value=f"{alliance_id}",
                    description=f"👥 Members: {alliance_members[alliance_id]} | Click to delete",
                    emoji="🗑️"
                ) for alliance_id, name in alliances
            ]
            
            option_pages = [all_options[i:i + items_per_page] for i in range(0, len(all_options), items_per_page)]
            
            embed = discord.Embed(
                title="🗑️ Delete Alliance",
                description=(
                    "**⚠️ Warning: This action cannot be undone!**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "1️⃣ Select an alliance from the dropdown menu\n"
                    "2️⃣ Use ◀️ ▶️ buttons to navigate between pages\n\n"
                    f"**Current Page:** 1/{len(option_pages)}\n"
                    f"**Total Alliances:** {len(alliances)}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.red()
            )
            embed.set_footer(text="⚠️ Warning: Deleting an alliance will remove all its data!")
            embed.timestamp = discord.utils.utcnow()

            view = PaginatedDeleteView(option_pages, self.alliance_delete_callback)
            
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            print(f"Error in delete_alliance: {e}")
            error_embed = discord.Embed(
                title="❌ Error",
                description="An error occurred while loading the delete menu.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)

    async def alliance_delete_callback(self, interaction: discord.Interaction):
        try:
            alliance_id = int(interaction.data["values"][0])
            
            if mongo_enabled() and AllianceMetadataAdapter is not None:
                alliances_map = AllianceMetadataAdapter.get_metadata('alliances') or {}
                settings_map = self._load_alliancesettings_from_mongo()
                a_doc = alliances_map.get(str(alliance_id))
                if not a_doc:
                    await interaction.response.send_message("Alliance not found.", ephemeral=True)
                    return
                alliance_name = a_doc.get('name')
                settings_count = 1 if settings_map.get(alliance_id) else 0
                users_count = self._member_count_for_alliance(alliance_id)
            else:
                self.c.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (alliance_id,))
                alliance_data = self.c.fetchone()
                
                if not alliance_data:
                    await interaction.response.send_message("Alliance not found.", ephemeral=True)
                    return
                
                alliance_name = alliance_data[0]

                self.c.execute("SELECT COUNT(*) FROM alliancesettings WHERE alliance_id = ?", (alliance_id,))
                settings_count = self.c.fetchone()[0]

                self.c_users.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                users_count = self.c_users.fetchone()[0]

            # For legacy adminserver/giftcode tables, still query sqlite if present
            try:
                self.c_settings.execute("SELECT COUNT(*) FROM adminserver WHERE alliances_id = ?", (alliance_id,))
                admin_server_count = self.c_settings.fetchone()[0]
            except Exception:
                admin_server_count = 0

            try:
                self.c_giftcode.execute("SELECT COUNT(*) FROM giftcode_channel WHERE alliance_id = ?", (alliance_id,))
                gift_channels_count = self.c_giftcode.fetchone()[0]
            except Exception:
                gift_channels_count = 0

            try:
                self.c_giftcode.execute("SELECT COUNT(*) FROM giftcodecontrol WHERE alliance_id = ?", (alliance_id,))
                gift_code_control_count = self.c_giftcode.fetchone()[0]
            except Exception:
                gift_code_control_count = 0

            confirm_embed = discord.Embed(
                title="⚠️ Confirm Alliance Deletion",
                description=(
                    f"Are you sure you want to delete this alliance?\n\n"
                    f"**Alliance Details:**\n"
                    f"🛡️ **Name:** {alliance_name}\n"
                    f"🔢 **ID:** {alliance_id}\n"
                    f"👥 **Members:** {users_count}\n\n"
                    f"**Data to be Deleted:**\n"
                    f"⚙️ Alliance Settings: {settings_count}\n"
                    f"👥 User Records: {users_count}\n"
                    f"🏰 Admin Server Records: {admin_server_count}\n"
                    f"📢 Gift Channels: {gift_channels_count}\n"
                    f"📊 Gift Code Controls: {gift_code_control_count}\n\n"
                    "**⚠️ WARNING: This action cannot be undone!**"
                ),
                color=discord.Color.red()
            )
            
            confirm_view = discord.ui.View(timeout=60)
            
            async def confirm_callback(button_interaction: discord.Interaction):
                try:
                    if mongo_enabled() and AllianceMetadataAdapter is not None:
                        # Remove alliance metadata
                        ok = self._delete_alliance_from_mongo(alliance_id)
                        alliance_count = 1 if ok else 0

                        # Remove member docs associated with this alliance (if any)
                        users_deleted = 0
                        try:
                            docs = AllianceMembersAdapter.get_all_members() or []
                            for d in docs:
                                try:
                                    fid = d.get('fid') or d.get('id') or d.get('_id')
                                    if str(d.get('alliance')) == str(alliance_id) or d.get('alliance') == alliance_id:
                                        if AllianceMembersAdapter.delete_member(str(fid)):
                                            users_deleted += 1
                                except Exception:
                                    continue
                        except Exception:
                            users_deleted = 0
                        users_count_deleted = users_deleted

                        # Legacy sqlite cleanup for related tables (best-effort)
                        try:
                            self.c_settings.execute("DELETE FROM adminserver WHERE alliances_id = ?", (alliance_id,))
                            admin_server_count = self.c_settings.rowcount
                            self.conn_settings.commit()
                        except Exception:
                            admin_server_count = 0

                        try:
                            self.c_giftcode.execute("DELETE FROM giftcode_channel WHERE alliance_id = ?", (alliance_id,))
                            gift_channels_count = self.c_giftcode.rowcount
                        except Exception:
                            gift_channels_count = 0

                        try:
                            self.c_giftcode.execute("DELETE FROM giftcodecontrol WHERE alliance_id = ?", (alliance_id,))
                            gift_code_control_count = self.c_giftcode.rowcount
                        except Exception:
                            gift_code_control_count = 0
                        try:
                            self.conn_giftcode.commit()
                        except Exception:
                            pass
                    else:
                        self.c.execute("DELETE FROM alliance_list WHERE alliance_id = ?", (alliance_id,))
                        alliance_count = self.c.rowcount
                        
                        self.c.execute("DELETE FROM alliancesettings WHERE alliance_id = ?", (alliance_id,))
                        admin_settings_count = self.c.rowcount
                        
                        self.conn.commit()

                        self.c_users.execute("DELETE FROM users WHERE alliance = ?", (alliance_id,))
                        users_count_deleted = self.c_users.rowcount
                        self.conn_users.commit()

                        self.c_settings.execute("DELETE FROM adminserver WHERE alliances_id = ?", (alliance_id,))
                        admin_server_count = self.c_settings.rowcount
                        self.conn_settings.commit()

                        self.c_giftcode.execute("DELETE FROM giftcode_channel WHERE alliance_id = ?", (alliance_id,))
                        gift_channels_count = self.c_giftcode.rowcount

                        self.c_giftcode.execute("DELETE FROM giftcodecontrol WHERE alliance_id = ?", (alliance_id,))
                        gift_code_control_count = self.c_giftcode.rowcount
                        
                        self.conn_giftcode.commit()

                    cleanup_embed = discord.Embed(
                        title="✅ Alliance Successfully Deleted",
                        description=(
                            f"Alliance **{alliance_name}** has been deleted.\n\n"
                            "**Cleaned Up Data:**\n"
                            f"🛡️ Alliance Records: {alliance_count}\n"
                            f"👥 Users Removed: {users_count_deleted}\n"
                            f"⚙️ Alliance Settings: {admin_settings_count}\n"
                            f"🏰 Admin Server Records: {admin_server_count}\n"
                            f"📢 Gift Channels: {gift_channels_count}\n"
                            f"📊 Gift Code Controls: {gift_code_control_count}"
                        ),
                        color=discord.Color.green()
                    )
                    cleanup_embed.set_footer(text="All related data has been successfully removed")
                    cleanup_embed.timestamp = discord.utils.utcnow()
                    
                    await button_interaction.response.edit_message(embed=cleanup_embed, view=None)
                    
                except Exception as e:
                    error_embed = discord.Embed(
                        title="❌ Error",
                        description=f"An error occurred while deleting the alliance: {str(e)}",
                        color=discord.Color.red()
                    )
                    await button_interaction.response.edit_message(embed=error_embed, view=None)

            async def cancel_callback(button_interaction: discord.Interaction):
                cancel_embed = discord.Embed(
                    title="❌ Deletion Cancelled",
                    description="Alliance deletion has been cancelled.",
                    color=discord.Color.grey()
                )
                await button_interaction.response.edit_message(embed=cancel_embed, view=None)

            confirm_button = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger)
            cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.grey)
            confirm_button.callback = confirm_callback
            cancel_button.callback = cancel_callback
            confirm_view.add_item(confirm_button)
            confirm_view.add_item(cancel_button)

            await interaction.response.edit_message(embed=confirm_embed, view=confirm_view)

        except Exception as e:
            print(f"Error in alliance_delete_callback: {e}")
            error_embed = discord.Embed(
                title="❌ Error",
                description="An error occurred while processing the deletion.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)

    async def handle_button_interaction(self, interaction: discord.Interaction):
        custom_id = interaction.data["custom_id"]
        
        if custom_id == "main_menu":
            embed = discord.Embed(
                title="⚙️ Settings Menu",
                description=(
                    "Please select a category:\n\n"
                    "**Menu Categories**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🏰 **Alliance Operations**\n"
                    "└ Manage alliances and settings\n\n"
                    "👥 **Alliance Member Operations**\n"
                    "└ Add, remove, and view members\n\n"
                    "🤖 **Bot Operations**\n"
                    "└ Configure bot settings\n\n"
                    "🎁 **Gift Code Operations**\n"
                    "└ Manage gift codes and rewards\n\n"
                    "📜 **Alliance History**\n"
                    "└ View alliance changes and history\n\n"
                    "🆘 **Support Operations**\n"
                    "└ Access support features\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.blue()
            )
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Alliance Operations",
                emoji="🏰",
                style=discord.ButtonStyle.primary,
                custom_id="alliance_operations",
                row=0
            ))
            view.add_item(discord.ui.Button(
                label="Member Operations",
                emoji="👥",
                style=discord.ButtonStyle.primary,
                custom_id="member_operations",
                row=0
            ))
            view.add_item(discord.ui.Button(
                label="Bot Operations",
                emoji="🤖",
                style=discord.ButtonStyle.primary,
                custom_id="bot_operations",
                row=1
            ))
            view.add_item(discord.ui.Button(
                label="Gift Operations",
                emoji="🎁",
                style=discord.ButtonStyle.primary,
                custom_id="gift_code_operations",
                row=1
            ))
            view.add_item(discord.ui.Button(
                label="Alliance History",
                emoji="📜",
                style=discord.ButtonStyle.primary,
                custom_id="alliance_history",
                row=2
            ))
            view.add_item(discord.ui.Button(
                label="Support Operations",
                emoji="🆘",
                style=discord.ButtonStyle.primary,
                custom_id="support_operations",
                row=2
            ))
            view.add_item(discord.ui.Button(
                label="Other Features",
                emoji="🔧",
                style=discord.ButtonStyle.primary,
                custom_id="other_features",
                row=3
            ))


            await interaction.response.edit_message(embed=embed, view=view)

        elif custom_id == "other_features":
            try:
                other_features_cog = interaction.client.get_cog("OtherFeatures")
                if other_features_cog:
                    await other_features_cog.show_other_features_menu(interaction)
                else:
                    await interaction.response.send_message(
                        "❌ Other Features module not found.",
                        ephemeral=True
                    )
            except Exception as e:
                if not any(error_code in str(e) for error_code in ["10062", "40060"]):
                    print(f"Other features error: {e}")
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "An error occurred while loading Other Features menu.",
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        "An error occurred while loading Other Features menu.",
                        ephemeral=True
                    )

    async def show_main_menu(self, interaction: discord.Interaction):
        try:
            embed = discord.Embed(
                title="⚙️ Settings Menu",
                description=(
                    "Please select a category:\n\n"
                    "**Menu Categories**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🏰 **Alliance Operations**\n"
                    "└ Manage alliances and settings\n\n"
                    "👥 **Alliance Member Operations**\n"
                    "└ Add, remove, and view members\n\n"
                    "🤖 **Bot Operations**\n"
                    "└ Configure bot settings\n\n"
                    "🎁 **Gift Code Operations**\n"
                    "└ Manage gift codes and rewards\n\n"
                    "📜 **Alliance History**\n"
                    "└ View alliance changes and history\n\n"
                    "🆘 **Support Operations**\n"
                    "└ Access support features\n\n"
                    "🔧 **Other Features**\n"
                    "└ Access other features\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.blue()
            )
            
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Alliance Operations",
                emoji="🏰",
                style=discord.ButtonStyle.primary,
                custom_id="alliance_operations",
                row=0
            ))
            view.add_item(discord.ui.Button(
                label="Member Operations",
                emoji="👥",
                style=discord.ButtonStyle.primary,
                custom_id="member_operations",
                row=0
            ))
            view.add_item(discord.ui.Button(
                label="Bot Operations",
                emoji="🤖",
                style=discord.ButtonStyle.primary,
                custom_id="bot_operations",
                row=1
            ))
            view.add_item(discord.ui.Button(
                label="Gift Operations",
                emoji="🎁",
                style=discord.ButtonStyle.primary,
                custom_id="gift_code_operations",
                row=1
            ))
            view.add_item(discord.ui.Button(
                label="Alliance History",
                emoji="📜",
                style=discord.ButtonStyle.primary,
                custom_id="alliance_history",
                row=2
            ))
            view.add_item(discord.ui.Button(
                label="Support Operations",
                emoji="🆘",
                style=discord.ButtonStyle.primary,
                custom_id="support_operations",
                row=2
            ))
            view.add_item(discord.ui.Button(
                label="Other Features",
                emoji="🔧",
                style=discord.ButtonStyle.primary,
                custom_id="other_features",
                row=3
            ))

            try:
                await interaction.response.edit_message(embed=embed, view=view)
            except discord.InteractionResponded:
                pass
                
        except Exception as e:
            pass

    @discord.ui.button(label="Bot Operations", emoji="🤖", style=discord.ButtonStyle.primary, custom_id="bot_operations", row=1)
    async def bot_operations_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            bot_ops_cog = interaction.client.get_cog("BotOperations")
            if bot_ops_cog:
                await bot_ops_cog.show_bot_operations_menu(interaction)
            else:
                await interaction.response.send_message(
                    "❌ Bot Operations module not found.",
                    ephemeral=True
                )
        except Exception as e:
            print(f"Bot operations button error: {e}")
            await interaction.response.send_message(
                "❌ An error occurred. Please try again.",
                ephemeral=True
            )

class AllianceModal(discord.ui.Modal):
    def __init__(self, title: str, default_name: str = "", default_interval: str = "0"):
        super().__init__(title=title)
        
        self.name = discord.ui.TextInput(
            label="Alliance Name",
            placeholder="Enter alliance name",
            default=default_name,
            required=True
        )
        self.add_item(self.name)
        
        self.interval = discord.ui.TextInput(
            label="Control Interval (minutes)",
            placeholder="Enter interval (0 to disable)",
            default=default_interval,
            required=True
        )
        self.add_item(self.interval)

    async def on_submit(self, interaction: discord.Interaction):
        self.interaction = interaction

class AllianceView(discord.ui.View):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    @discord.ui.button(
        label="Main Menu",
        emoji="🏠",
        style=discord.ButtonStyle.secondary,
        custom_id="main_menu"
    )
    async def main_menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_main_menu(interaction)

class MemberOperationsView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def get_admin_alliances(self, user_id, guild_id):
        is_admin, is_initial = self.cog._is_admin(user_id)
        if not is_admin:
            return []

        if mongo_enabled() and AllianceMetadataAdapter is not None:
            raw = self.cog._load_alliances_from_mongo()
            if is_initial == 1:
                return [(aid, name) for (aid, name, _dsid) in raw]
            else:
                return [(aid, name) for (aid, name, dsid) in raw if dsid == guild_id]
        else:
            if is_initial == 1:
                self.cog.c.execute("SELECT alliance_id, name FROM alliance_list ORDER BY name")
            else:
                self.cog.c.execute("""
                    SELECT alliance_id, name 
                    FROM alliance_list 
                    WHERE discord_server_id = ? 
                    ORDER BY name
                """, (guild_id,))
            return self.cog.c.fetchall()

    @discord.ui.button(label="Add Member", emoji="➕", style=discord.ButtonStyle.primary, custom_id="add_member")
    async def add_member_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            alliances = await self.get_admin_alliances(interaction.user.id, interaction.guild.id)
            if not alliances:
                await interaction.response.send_message("İttifak üyesi ekleme yetkiniz yok.", ephemeral=True)
                return

            options = [
                discord.SelectOption(
                    label=f"{name}",
                    value=str(alliance_id),
                    description=f"İttifak ID: {alliance_id}"
                ) for alliance_id, name in alliances
            ]

            select = discord.ui.Select(
                placeholder="Bir ittifak seçin",
                options=options,
                custom_id="alliance_select"
            )

            view = discord.ui.View()
            view.add_item(select)

            await interaction.response.send_message(
                "Üye eklemek istediğiniz ittifakı seçin:",
                view=view,
                ephemeral=True
            )

        except Exception as e:
            print(f"Error in add_member_button: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred during the process of adding a member.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "An error occurred during the process of adding a member.",
                    ephemeral=True
                )

    @discord.ui.button(label="Remove Member", emoji="➖", style=discord.ButtonStyle.danger, custom_id="remove_member")
    async def remove_member_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            alliances = await self.get_admin_alliances(interaction.user.id, interaction.guild.id)
            if not alliances:
                await interaction.response.send_message("You are not authorized to delete alliance members.", ephemeral=True)
                return

            options = [
                discord.SelectOption(
                    label=f"{name}",
                    value=str(alliance_id),
                    description=f"Alliance ID: {alliance_id}"
                ) for alliance_id, name in alliances
            ]

            select = discord.ui.Select(
                placeholder="Choose an alliance",
                options=options,
                custom_id="alliance_select_remove"
            )

            view = discord.ui.View()
            view.add_item(select)

            await interaction.response.send_message(
                "Select the alliance you want to delete members from:",
                view=view,
                ephemeral=True
            )

        except Exception as e:
            print(f"Error in remove_member_button: {e}")
            await interaction.response.send_message(
                "An error occurred during the member deletion process.",
                ephemeral=True
            )

    @discord.ui.button(label="View Members", emoji="👥", style=discord.ButtonStyle.primary, custom_id="view_members")
    async def view_members_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            alliances = await self.get_admin_alliances(interaction.user.id, interaction.guild.id)
            if not alliances:
                await interaction.response.send_message("You are not authorized to screen alliance members.", ephemeral=True)
                return

            options = [
                discord.SelectOption(
                    label=f"{name}",
                    value=str(alliance_id),
                    description=f"Alliance ID: {alliance_id}"
                ) for alliance_id, name in alliances
            ]

            select = discord.ui.Select(
                placeholder="Choose an alliance",
                options=options,
                custom_id="alliance_select_view"
            )

            view = discord.ui.View()
            view.add_item(select)

            await interaction.response.send_message(
                "Select the alliance whose members you want to view:",
                view=view,
                ephemeral=True
            )

        except Exception as e:
            print(f"Error in view_members_button: {e}")
            await interaction.response.send_message(
                "An error occurred while viewing the member list.",
                ephemeral=True
            )

    @discord.ui.button(label="Main Menu", emoji="🏠", style=discord.ButtonStyle.secondary, custom_id="main_menu")
    async def main_menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.show_main_menu(interaction)
        except Exception as e:
            print(f"Error in main_menu_button: {e}")
            await interaction.response.send_message(
                "An error occurred during return to the main menu.",
                ephemeral=True
            )

class PaginatedDeleteView(discord.ui.View):
    def __init__(self, pages, original_callback):
        super().__init__(timeout=7200)
        self.current_page = 0
        self.pages = pages
        self.original_callback = original_callback
        self.total_pages = len(pages)
        self.update_view()

    def update_view(self):
        self.clear_items()
        
        select = discord.ui.Select(
            placeholder=f"Select alliance to delete ({self.current_page + 1}/{self.total_pages})",
            options=self.pages[self.current_page]
        )
        select.callback = self.original_callback
        self.add_item(select)
        
        previous_button = discord.ui.Button(
            label="◀️",
            style=discord.ButtonStyle.grey,
            custom_id="previous",
            disabled=(self.current_page == 0)
        )
        previous_button.callback = self.previous_callback
        self.add_item(previous_button)

        next_button = discord.ui.Button(
            label="▶️",
            style=discord.ButtonStyle.grey,
            custom_id="next",
            disabled=(self.current_page == len(self.pages) - 1)
        )
        next_button.callback = self.next_callback
        self.add_item(next_button)

    async def previous_callback(self, interaction: discord.Interaction):
        self.current_page = (self.current_page - 1) % len(self.pages)
        self.update_view()
        
        embed = discord.Embed(
            title="🗑️ Delete Alliance",
            description=(
                "**⚠️ Warning: This action cannot be undone!**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ Select an alliance from the dropdown menu\n"
                "2️⃣ Use ◀️ ▶️ buttons to navigate between pages\n\n"
                f"**Current Page:** {self.current_page + 1}/{self.total_pages}\n"
                f"**Total Alliances:** {sum(len(page) for page in self.pages)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.red()
        )
        embed.set_footer(text="⚠️ Warning: Deleting an alliance will remove all its data!")
        embed.timestamp = discord.utils.utcnow()
        
        await interaction.response.edit_message(embed=embed, view=self)

    async def next_callback(self, interaction: discord.Interaction):
        self.current_page = (self.current_page + 1) % len(self.pages)
        self.update_view()
        
        embed = discord.Embed(
            title="🗑️ Delete Alliance",
            description=(
                "**⚠️ Warning: This action cannot be undone!**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ Select an alliance from the dropdown menu\n"
                "2️⃣ Use ◀️ ▶️ buttons to navigate between pages\n\n"
                f"**Current Page:** {self.current_page + 1}/{self.total_pages}\n"
                f"**Total Alliances:** {sum(len(page) for page in self.pages)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.red()
        )
        embed.set_footer(text="⚠️ Warning: Deleting an alliance will remove all its data!")
        embed.timestamp = discord.utils.utcnow()
        
        await interaction.response.edit_message(embed=embed, view=self)

class PaginatedChannelView(discord.ui.View):
    def __init__(self, channels, original_callback):
        super().__init__(timeout=7200)
        self.current_page = 0
        self.channels = channels
        self.original_callback = original_callback
        self.items_per_page = 25
        self.pages = [channels[i:i + self.items_per_page] for i in range(0, len(channels), self.items_per_page)]
        self.total_pages = len(self.pages)
        self.update_view()

    def update_view(self):
        self.clear_items()
        
        current_channels = self.pages[self.current_page]
        channel_options = [
            discord.SelectOption(
                label=f"#{channel.name}"[:100],
                value=str(channel.id),
                description=f"Channel ID: {channel.id}" if len(f"#{channel.name}") > 40 else None,
                emoji="📢"
            ) for channel in current_channels
        ]
        
        select = discord.ui.Select(
            placeholder=f"Select channel ({self.current_page + 1}/{self.total_pages})",
            options=channel_options
        )
        select.callback = self.original_callback
        self.add_item(select)
        
        if self.total_pages > 1:
            previous_button = discord.ui.Button(
                label="◀️",
                style=discord.ButtonStyle.grey,
                custom_id="previous",
                disabled=(self.current_page == 0)
            )
            previous_button.callback = self.previous_callback
            self.add_item(previous_button)

            next_button = discord.ui.Button(
                label="▶️",
                style=discord.ButtonStyle.grey,
                custom_id="next",
                disabled=(self.current_page == len(self.pages) - 1)
            )
            next_button.callback = self.next_callback
            self.add_item(next_button)

    async def previous_callback(self, interaction: discord.Interaction):
        self.current_page = (self.current_page - 1) % len(self.pages)
        self.update_view()
        
        embed = interaction.message.embeds[0]
        embed.description = (
            f"**Page:** {self.current_page + 1}/{self.total_pages}\n"
            f"**Total Channels:** {len(self.channels)}\n\n"
            "Please select a channel from the menu below."
        )
        
        await interaction.response.edit_message(embed=embed, view=self)

    async def next_callback(self, interaction: discord.Interaction):
        self.current_page = (self.current_page + 1) % len(self.pages)
        self.update_view()
        
        embed = interaction.message.embeds[0]
        embed.description = (
            f"**Page:** {self.current_page + 1}/{self.total_pages}\n"
            f"**Total Channels:** {len(self.channels)}\n\n"
            "Please select a channel from the menu below."
        )
        
        await interaction.response.edit_message(embed=embed, view=self)

async def setup(bot):
    try:
        # Prefer using a shared connection created in main.py (attached to bot)
        conn = None
        if hasattr(bot, "_connections") and isinstance(bot._connections, dict):
            conn = bot._connections.get("conn_alliance")

        if conn is None:
            # Fallback: ensure the repository `db` folder exists and open local DB
            from pathlib import Path

            repo_root = Path(__file__).resolve().parents[1]
            db_dir = repo_root / "db"
            try:
                db_dir.mkdir(parents=True, exist_ok=True)
            except Exception as mkdir_exc:
                logger.error(f"Failed to create db directory {db_dir}: {mkdir_exc}")

            db_path = db_dir / "alliance.sqlite"

            try:
                conn = sqlite3.connect(str(db_path))
            except Exception as conn_exc:
                logger.error(f"✗ Failed to open SQLite DB at {db_path}: {conn_exc}")
                raise

        cog = Alliance(bot, conn)
        await bot.add_cog(cog)
        logger.info(f"✓ Alliance cog loaded successfully")
    except Exception as e:
        logger.error(f"✗ Failed to setup Alliance cog: {e}", exc_info=True)
        raise
