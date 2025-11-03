import asyncio
import json
import os
from datetime import datetime
import logging
from typing import Dict, List

import discord

from gift_codes import get_active_gift_codes

logger = logging.getLogger(__name__)

# State file to persist configured channels and sent codes
STATE_FILE = os.path.join(os.path.dirname(__file__), 'giftcode_state.json')

# Default check interval in seconds
DEFAULT_INTERVAL = int(os.getenv('GIFTCODE_CHECK_INTERVAL', '300'))  # 5 minutes


class GiftCodePoster:
    def __init__(self):
        # Structure: {
        #   "channels": {"<guild_id>": <channel_id>, ...},
        #   "sent": {"<guild_id>": ["CODE1","CODE2"], "global": [..]}
        # }
        self.state: Dict = {"channels": {}, "sent": {}}
        self.lock = asyncio.Lock()
        self._load_state()

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
            else:
                self._save_state_sync()
        except Exception as e:
            logger.error(f"Failed to load giftcode state: {e}")

    def _save_state_sync(self):
        try:
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
            sent = set(self.state.setdefault('sent', {}).setdefault(str(guild_id), []))
            sent.update(codes)
            # store back
            self.state['sent'][str(guild_id)] = list(sent)
            await self._save_state()

    async def get_sent_set(self, guild_id: int):
        async with self.lock:
            return set(self.state.setdefault('sent', {}).setdefault(str(guild_id), []))


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

        await channel.send(embed=embed)
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

        # Build mapping of code -> full dict for richer embeds
        code_map = {c.get('code','').strip(): c for c in fetched if c.get('code')}
        fetched_codes = list(code_map.keys())
        fetched_set = set(fetched_codes)

        posted_total = 0
        errors = 0

        channels = poster.list_channels()
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
                new_code_keys = [k for k in fetched_codes if k and k not in sent_set]
                if not new_code_keys:
                    continue

                # Prepare list of dicts for embed
                new_code_dicts = [code_map[k] for k in new_code_keys if k in code_map]

                # Post new codes in one message (embed)
                await post_new_codes_to_channel(bot, channel, new_code_dicts)
                # Mark as sent (store code strings)
                await poster.mark_sent(guild_id, new_code_keys)
                posted_total += len(new_code_keys)

            except Exception as e:
                logger.error(f"Error processing guild {guild_id}: {e}")
                errors += 1

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
