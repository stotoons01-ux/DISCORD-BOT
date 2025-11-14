import asyncio
import json
import os
from datetime import datetime
import logging
from typing import Dict, List

import discord

from gift_codes import get_active_gift_codes
try:
    from db.mongo_adapters import mongo_enabled, GiftcodeStateAdapter, GiftCodesAdapter
except Exception:
    mongo_enabled = lambda: False
    GiftcodeStateAdapter = None
    GiftCodesAdapter = None

logger = logging.getLogger(__name__)

# State file to persist configured channels and sent codes
STATE_FILE = os.path.join(os.path.dirname(__file__), 'giftcode_state.json')

# Default check interval in seconds (reduced to 10s by default for faster checks)
DEFAULT_INTERVAL = int(os.getenv('GIFTCODE_CHECK_INTERVAL', '10'))  # 10 seconds


class GiftCodePoster:
    def __init__(self):
        # Structure: {
        #   "channels": {"<guild_id>": <channel_id>, ...},
        #   "sent": {"<guild_id>": ["CODE1","CODE2"], "global": [..]}
        # }
        self.state: Dict = {"channels": {}, "sent": {}}
        self.lock = asyncio.Lock()
        self._load_state()

    def _normalize_code(self, code: str) -> str:
        """Normalize code strings for consistent comparison/storage."""
        if not code:
            return ""
        return str(code).strip().upper()

    def _load_state(self):
        try:
            # Prefer Mongo when available
            if mongo_enabled() and GiftcodeStateAdapter is not None:
                try:
                    s = GiftcodeStateAdapter.get_state()
                    if s:
                        self.state = s
                        # Ensure normalized shapes
                        self.state.setdefault('channels', {})
                        self.state.setdefault('sent', {})
                        self.state.setdefault('initialized', False)
                        return
                except Exception:
                    pass
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
            else:
                self._save_state_sync()
        except Exception as e:
            logger.error(f"Failed to load giftcode state: {e}")
        # Normalize any existing sent codes to ensure consistent comparisons
        try:
            sent = self.state.setdefault('sent', {})
            for guild_id, codes in list(sent.items()):
                normalized = [self._normalize_code(c) for c in (codes or []) if c]
                self.state['sent'][str(guild_id)] = list(dict.fromkeys(normalized))
        except Exception:
            pass
        # If Mongo is available, and we have a separate gift_codes collection,
        # pull any globally recorded codes so they count as already-sent.
        try:
            if mongo_enabled() and GiftCodesAdapter is not None:
                try:
                    all_codes = GiftCodesAdapter.get_all() or []
                    # get_all returns list of tuples like (code, date, status)
                    global_codes = [self._normalize_code(t[0]) for t in all_codes if t and t[0]]
                    if global_codes:
                        self.state.setdefault('sent', {}).setdefault('global', [])
                        # merge and dedupe
                        existing = set(self.state['sent'].get('global', []))
                        merged = list(dict.fromkeys([*existing, *global_codes]))
                        self.state['sent']['global'] = merged
                except Exception:
                    pass
        except Exception:
            pass
        # Ensure initialized flag exists so we can detect first-run behavior
        try:
            self.state.setdefault('initialized', False)
        except Exception:
            pass

    def _save_state_sync(self):
        try:
            # Prefer Mongo when available
            if mongo_enabled() and GiftcodeStateAdapter is not None:
                try:
                    GiftcodeStateAdapter.set_state(self.state)
                    return
                except Exception:
                    pass
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write giftcode state: {e}")

    async def _save_state(self):
        async with self.lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._save_state_sync)

    def set_channel(self, guild_id: int, channel_id: int):
        self.state.setdefault('channels', {})[str(guild_id)] = int(channel_id)
        # ensure sent dict exists for guild
        self.state.setdefault('sent', {}).setdefault(str(guild_id), [])
        # persist synchronously (caller should await saved state when possible)
        try:
            self._save_state_sync()
        except Exception:
            pass

    def unset_channel(self, guild_id: int):
        self.state.get('channels', {}).pop(str(guild_id), None)
        try:
            self._save_state_sync()
        except Exception:
            pass

    def get_channel(self, guild_id: int):
        return self.state.get('channels', {}).get(str(guild_id))

    def list_channels(self) -> Dict[str, int]:
        return {int(k): int(v) for k, v in self.state.get('channels', {}).items()}

    async def mark_sent(self, guild_id: int, codes: List[str]):
        async with self.lock:
            sent_list = self.state.setdefault('sent', {}).setdefault(str(guild_id), [])
            sent = set(self._normalize_code(c) for c in (sent_list or []))
            for c in (codes or []):
                if c:
                    sent.add(self._normalize_code(c))
            # store back
            # keep deterministic order
            self.state['sent'][str(guild_id)] = list(sorted(sent))
            # Persist synchronously to ensure durability across restarts
            try:
                self._save_state_sync()
                logger.info(f"Giftcode state saved synchronously after marking {len(codes or [])} codes for guild {guild_id}")
            except Exception as e:
                logger.error(f"Synchronous save failed: {e}")
                # Fallback to async save
                try:
                    await self._save_state()
                except Exception as e2:
                    logger.error(f"Async fallback save also failed: {e2}")
        # Also persist each new code into the Mongo `gift_codes` collection
        try:
            if mongo_enabled() and GiftCodesAdapter is not None:
                from datetime import datetime as _dt
                now = _dt.utcnow().isoformat()
                for c in (codes or []):
                    if not c:
                        continue
                    try:
                        GiftCodesAdapter.insert(self._normalize_code(c), now, validation_status='posted')
                    except Exception:
                        # Non-fatal: continue on insert errors
                        logger.debug(f"Failed to insert code into GiftCodesAdapter: {c}")
        except Exception:
            pass

    async def get_sent_set(self, guild_id: int):
        async with self.lock:
            sent = self.state.setdefault('sent', {})
            guild_codes = sent.setdefault(str(guild_id), [])
            global_codes = sent.setdefault('global', [])
            combined = list((guild_codes or []) + (global_codes or []))
            return set(self._normalize_code(c) for c in combined if c)


poster = GiftCodePoster()


async def post_new_codes_to_channel(bot: discord.Client, channel: discord.TextChannel, new_codes: List[Dict]):
    """Post new codes using the same embed style as /giftcode. Expects list of code dicts."""
    if not new_codes:
        return

    try:
        embed = discord.Embed(
            title="✨ New Whiteout Survival Gift Codes ✨",
            color=0xffd700,
            description=f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        embed.set_thumbnail(url="https://i.postimg.cc/s2xHV7N7/Groovy-gift.gif")

        for code in new_codes[:10]:
            code_str = code.get('code', '')
            value = f"```{code_str}```\n*Rewards:* {code.get('rewards','Rewards not specified')}\n*Expires:* {code.get('expiry','Unknown')}"
            embed.add_field(name="🎟️ Code:", value=value, inline=False)

        if len(new_codes) > 10:
            embed.set_footer(text=f"And {len(new_codes) - 10} more codes...")
        else:
            embed.set_footer(text="Use /giftcode to see all active codes!")

        # Build a View that matches the interactive behavior in /giftcode
        class GiftCodeView(discord.ui.View):
            def __init__(self, codes_list):
                super().__init__(timeout=300)
                self.codes = codes_list or []
                self.message = None

            @discord.ui.button(label="Copy Code", style=discord.ButtonStyle.primary, custom_id="giftcode_copy")
            async def copy_button(self, interaction_button: discord.Interaction, button: discord.ui.Button):
                # Send all active gift codes in a simple plain-text DM (one code per line).
                # If DMs are closed, fall back to an ephemeral message with the same plain text.
                if not self.codes:
                    try:
                        await interaction_button.response.send_message("No gift codes available to copy.", ephemeral=True)
                    except Exception:
                        logger.debug("Failed to send ephemeral no-codes message")
                    return

                # Build a simple plain-text list of codes (only the code strings)
                code_list = [c.get('code', '').strip() for c in self.codes if c.get('code')]
                if not code_list:
                    try:
                        await interaction_button.response.send_message("Couldn't find any codes to copy.", ephemeral=True)
                    except Exception:
                        logger.debug("Failed to send ephemeral no-code-found message")
                    return

                plain_text = "\n".join(code_list)
                # Append the signature line similar to the main command
                plain_text += "\n\nGift Code :gift:  STATE #3063"

                try:
                    await interaction_button.response.defer(ephemeral=True)
                except Exception:
                    pass

                user = interaction_button.user
                dm_sent = False
                try:
                    await user.send(plain_text)
                    dm_sent = True
                except Exception as dm_err:
                    logger.info(f"Could not send DM to user {getattr(user, 'id', 'unknown')}: {dm_err}")

                try:
                    if dm_sent:
                        await interaction_button.followup.send("I've sent all active gift codes to your DMs. Check your messages!", ephemeral=True)
                    else:
                        await interaction_button.followup.send(f"Couldn't DM you. Here are the codes:\n\n{plain_text}", ephemeral=True)
                except Exception:
                    logger.debug("Failed to send followup after DM attempt")

            @discord.ui.button(label="Refresh Codes", style=discord.ButtonStyle.secondary, custom_id="giftcode_refresh")
            async def refresh_button(self, interaction_button: discord.Interaction, button: discord.ui.Button):
                await interaction_button.response.defer(ephemeral=True)
                try:
                    new_codes_fresh = await get_active_gift_codes()
                    if not new_codes_fresh:
                        await interaction_button.followup.send("No active gift codes available right now.", ephemeral=True)
                        return

                    self.codes = new_codes_fresh
                    # Rebuild embed
                    new_embed = discord.Embed(
                        title="✨ Active Whiteout Survival Gift Codes ✨",
                        color=0xffd700,
                        description=f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    )
                    new_embed.set_thumbnail(url="https://i.postimg.cc/s2xHV7N7/Groovy-gift.gif")

                    for code in (self.codes or [])[:10]:
                        name = f"🎟️ Code:"
                        value = f"```{code.get('code','')}```\n*Rewards:* {code.get('rewards','Rewards not specified')}\n*Expires:* {code.get('expiry','Unknown')}"
                        new_embed.add_field(name=name, value=value, inline=False)

                    if self.codes and len(self.codes) > 10:
                        new_embed.set_footer(text=f"And {len(self.codes) - 10} more codes...")
                    else:
                        new_embed.set_footer(text="Use /giftcode to see all active codes!")

                    # Edit the message containing the embed
                    if self.message:
                        try:
                            await self.message.edit(embed=new_embed)
                            await interaction_button.followup.send("Gift codes refreshed.", ephemeral=True)
                        except Exception as edit_err:
                            logger.error(f"Failed to edit gift code message: {edit_err}")
                            await interaction_button.followup.send("Failed to update the gift codes message.", ephemeral=True)
                    else:
                        await interaction_button.followup.send(embed=new_embed, ephemeral=False)

                except Exception as e:
                    logger.error(f"Error refreshing gift codes via button: {e}")
                    await interaction_button.followup.send("Error while refreshing gift codes.", ephemeral=True)

            @discord.ui.button(label="Redeem Latest (Alliance)", style=discord.ButtonStyle.success, custom_id="giftcode_redeem_alliance")
            async def redeem_for_alliance(self, interaction_button: discord.Interaction, button: discord.ui.Button):
                """Fetch the latest active gift codes and open an alliance selector to enqueue a manual redemption."""
                try:
                    await interaction_button.response.defer(ephemeral=True)
                except Exception:
                    pass

                # Fetch freshest codes
                try:
                    codes_fresh = await get_active_gift_codes()
                except Exception as e:
                    logger.error(f"Error fetching active gift codes for redeem action: {e}")
                    await interaction_button.followup.send("Failed to fetch active gift codes.", ephemeral=True)
                    return

                if not codes_fresh:
                    await interaction_button.followup.send("No active gift codes available right now.", ephemeral=True)
                    return

                latest = codes_fresh[0]
                code_str = latest.get('code') if isinstance(latest, dict) else str(latest)
                if not code_str:
                    await interaction_button.followup.send("Couldn't determine the latest gift code.", ephemeral=True)
                    return

                # Get GiftOperations cog to obtain available alliances and to queue redemption
                cog = None
                try:
                    cog = interaction_button.client.get_cog('GiftOperations')
                except Exception:
                    cog = None

                if not cog:
                    await interaction_button.followup.send("GiftOperations cog not available on this bot instance.", ephemeral=True)
                    return

                try:
                    available = await cog.get_available_alliances(interaction_button)
                except Exception as e:
                    logger.exception(f"Error getting available alliances for user: {e}")
                    await interaction_button.followup.send("Failed to retrieve available alliances.", ephemeral=True)
                    return

                if not available:
                    await interaction_button.followup.send("You don't have any configured alliances or you're not authorized to redeem codes.", ephemeral=True)
                    return

                # Build a temporary view with a select menu for alliances
                class _AllianceSelect(discord.ui.Select):
                    def __init__(self, options):
                        super().__init__(placeholder='🏰 Select an alliance to redeem the latest code', min_values=1, max_values=1, options=options)

                    async def callback(self, select_interaction: discord.Interaction):
                        try:
                            await select_interaction.response.defer(ephemeral=True)
                        except Exception:
                            pass

                        selected = self.values[0]
                        try:
                            # queue manual redemption for the selected alliance
                            queue_positions = await cog.add_manual_redemption_to_queue(code_str, [int(selected)], select_interaction)
                            await select_interaction.followup.send(f"Queued redemption of `{code_str}` for alliance ID {selected}. Queue position(s): {queue_positions}", ephemeral=True)
                        except Exception as e:
                            logger.exception(f"Error queueing manual redemption for alliance {selected}: {e}")
                            try:
                                await select_interaction.followup.send("Failed to queue the redemption. Check logs.", ephemeral=True)
                            except Exception:
                                pass

                options = [discord.SelectOption(label=str(name)[:100], value=str(aid)) for aid, name in available]
                view_sel = discord.ui.View(timeout=120)
                view_sel.add_item(_AllianceSelect(options))

                try:
                    await interaction_button.followup.send(f"Select an alliance to redeem the latest gift code: `{code_str}`", view=view_sel, ephemeral=True)
                except Exception as e:
                    logger.exception(f"Failed to send alliance select view: {e}")
                    await interaction_button.followup.send("Failed to open alliance selector.", ephemeral=True)

        view = GiftCodeView(new_codes)
        sent = await channel.send(embed=embed, view=view)
        # Attach message reference to the view so Refresh can edit
        try:
            view.message = sent
        except Exception:
            logger.debug("Could not attach message reference to GiftCodeView")

        logger.info(f"Posted {len(new_codes)} new gift codes to {getattr(channel.guild,'name',None)} ({channel.id})")
    except Exception as e:
        logger.error(f"Failed to post gift codes to channel {getattr(channel,'id',None)}: {e}")


async def run_check_once(bot: discord.Client):
    """Fetch active codes and post new ones to configured channels. Returns summary dict."""
    try:
        fetched = await get_active_gift_codes()
        if not fetched:
            logger.info("No codes fetched from source")
            return {"posted": 0, "errors": 0}

        # Build mapping of normalized code -> full dict for richer embeds
        code_map = {poster._normalize_code(c.get('code','')): c for c in fetched if c.get('code')}
        fetched_codes = list(code_map.keys())
        fetched_set = set(fetched_codes)

        posted_total = 0
        errors = 0

        channels = poster.list_channels()
        initialized = bool(poster.state.get('initialized'))
        for guild_id, channel_id in channels.items():
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    logger.debug(f"Bot not in guild {guild_id}")
                    continue
                channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
                if not channel:
                    logger.warning(f"Configured gift channel {channel_id} not found for guild {guild_id}")
                    continue

                sent_set = await poster.get_sent_set(guild_id)
                # If this is the first run after the poster was created (no persisted state),
                # and the guild has no recorded sent codes, avoid blasting all current codes.
                # Instead, mark the currently fetched codes as sent and skip posting on this run.
                if (not initialized) and (not sent_set):
                    try:
                        # mark fetched codes as sent for this guild to avoid reposts
                        await poster.mark_sent(guild_id, list(fetched_set))
                        logger.info(f"Initialising sent set for guild {guild_id} with current codes (no post)")
                    except Exception as e:
                        logger.error(f"Failed to initialize sent set for guild {guild_id}: {e}")
                    continue

                # fetched_codes and sent_set are normalized already
                new_code_keys = [k for k in fetched_codes if k and k not in sent_set]
                if not new_code_keys:
                    continue

                # Prepare list of dicts for embed (use original casing from fetched map)
                new_code_dicts = [code_map[k] for k in new_code_keys if k in code_map]

                # Post new codes in one message (embed)
                await post_new_codes_to_channel(bot, channel, new_code_dicts)
                # Mark as sent (store code strings)
                await poster.mark_sent(guild_id, new_code_keys)
                posted_total += len(new_code_keys)

            except Exception as e:
                logger.error(f"Error processing guild {guild_id}: {e}")
                errors += 1

        # If this was the first run, persist initialized flag so subsequent runs behave normally
        try:
            if not poster.state.get('initialized'):
                poster.state['initialized'] = True
                await poster._save_state()
        except Exception:
            pass

        return {"posted": posted_total, "errors": errors}
    except Exception as e:
        logger.error(f"Giftcode poster check failed: {e}")
        return {"posted": 0, "errors": 1}


async def start_poster(bot: discord.Client, interval: int = DEFAULT_INTERVAL):
    """Background loop that periodically checks for new gift codes and posts them."""
    logger.info(f"Starting giftcode poster with interval={interval}s")
    while True:
        try:
            await run_check_once(bot)
        except Exception as e:
            logger.error(f"Unhandled error in giftcode poster loop: {e}")
        await asyncio.sleep(interval)


async def run_now_and_report(bot: discord.Client):
    return await run_check_once(bot)
