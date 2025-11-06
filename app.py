import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import os
import json
import logging
from api_manager import make_request, manager, make_image_request

from angel_personality import get_system_prompt, angel_personality
from user_mapping import get_known_user_name
from gift_codes import get_active_gift_codes
from reminder_system import ReminderSystem, set_user_timezone, get_user_timezone, TimeParser
from event_tips import EVENT_TIPS, get_event_info
from thinking_animation import ThinkingAnimation
import sys
import signal
import asyncio
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
import io
import health_server
import uptime_checker
import giftcode_poster
import aiohttp
from urllib.parse import quote
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
import random
import time
from pathlib import Path
import re
from wos_api import fetch_player_info
from beartrap_rag import is_beartrap_question, answer_beartrap_question
 
# Feedback state file (optional persistent feedback channel)
FEEDBACK_STATE_PATH = Path(__file__).parent / "feedback_state.json"
FEEDBACK_LOG_PATH = Path(__file__).parent / "feedback_log.txt"

def load_feedback_state():
    try:
        if FEEDBACK_STATE_PATH.exists():
            with FEEDBACK_STATE_PATH.open('r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        # logger may not be configured yet at import time; use print as last resort
        try:
            logger.error(f"Failed to load feedback state: {e}")
        except Exception:
            print(f"Failed to load feedback state: {e}")
    return {}

def save_feedback_state(state: dict):
    try:
        with FEEDBACK_STATE_PATH.open('w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        return True
    except Exception as e:
        try:
            logger.error(f"Failed to save feedback state: {e}")
        except Exception:
            print(f"Failed to save feedback state: {e}")
        return False

def get_feedback_channel_id():
    # Prefer persisted state over environment variable
    state = load_feedback_state()
    cid = state.get('channel_id')
    if cid:
        return int(cid)
    env_cid = os.getenv('FEEDBACK_CHANNEL_ID')
    return int(env_cid) if env_cid else None

def append_feedback_log(user, user_id, feedback_text, posted_channel=False, posted_owner=False):
    try:
        ts = datetime.utcnow().isoformat() + 'Z'
        entry = {
            'timestamp': ts,
            'user': str(user),
            'user_id': int(user_id),
            'posted_channel': bool(posted_channel),
            'posted_owner': bool(posted_owner),
            'feedback': feedback_text[:4000]
        }
        with FEEDBACK_LOG_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        try:
            logger.error(f"Failed to append feedback log: {e}")
        except Exception:
            print(f"Failed to append feedback log: {e}")
    


async def fetch_pollinations_image(prompt_text: str, width: int = None, height: int = None, model_name: str = None, seed: int = None) -> bytes:
    """Module-level helper to fetch images from Pollinations public endpoint."""
    base = "https://image.pollinations.ai/prompt/"
    encoded = quote(prompt_text, safe='')
    url = base + encoded
    params = []
    if width:
        params.append(f"width={int(width)}")
    if height:
        params.append(f"height={int(height)}")
    if model_name:
        params.append(f"model={quote(model_name, safe='')}")
    if seed is not None:
        params.append(f"seed={int(seed)}")
    if params:
        url = url + "?" + "&".join(params)

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status == 200:
                content_type = resp.headers.get("Content-Type", "") or resp.headers.get("content-type", "")
                if content_type and content_type.startswith("image/"):
                    return await resp.read()
                data = await resp.read()
                if data:
                    return data
                raise Exception(f"Empty response from Pollinations (status 200) for URL: {url}")
            elif resp.status == 429:
                raise Exception("Rate limited by Pollinations API")
            elif resp.status >= 500:
                raise Exception(f"Pollinations server error: {resp.status}")
            else:
                text = await resp.text()
                raise Exception(f"Pollinations request failed: {resp.status} - {text}")


def detect_image_request(text: str):
    """Detect whether the text is asking for an image and try to extract the prompt.

    Returns (matched: bool, prompt: Optional[str]). The prompt is the best-effort
    substring describing what to generate (may be the full text if extraction fails).
    """
    if not text:
        return False, None
    q = text.strip()
    q_lower = q.lower()

    # Quick phrase list (cover common conversational variants)
    phrases = [
        "create an image", "generate an image", "make an image",
        "image of", "picture of", "photo of", "drawing of", "sketch of",
        "draw me", "draw a", "draw an", "render", "render me", "paint me",
        "i want an image", "i want a picture", "show me a picture", "show me an image",
        "take a picture of", "could you draw", "can you draw", "please draw", "plz draw",
        "illustrate", "illustration of", "create a picture", "give me a picture",
    ]

    for p in phrases:
        if p in q_lower:
            idx = q_lower.find(p)
            # Text after the matched phrase is likely the prompt
            prompt = q[idx + len(p):].strip()
            if prompt:
                return True, prompt
            # Try to find an "of X" pattern after or near the phrase
            m = re.search(r"(?:of|:|-)\s*(.+)$", q)
            if m:
                return True, m.group(1).strip()
            # As a last resort return the whole text
            return True, q

    # Regex: look for direct "<image-term> of <target>" (e.g., "picture of a cat")
    image_terms = r"(?:image|picture|photo|drawing|sketch|render|illustration|art|portrait)"
    m = re.search(rf"{image_terms}\s+of\s+(?P<t>.+)", q, flags=re.I)
    if m:
        return True, m.group('t').strip()

    # Regex: verbs that imply generation with an image term somewhere nearby
    verb_terms = r"(?:create|generate|make|draw|render|paint|sketch|illustrate|show|give|send|produce|take|capture)"
    # Allow up to 40 chars between verb and image term to catch sarcastic/colloquial phrasing
    m2 = re.search(rf"(?P<verb>{verb_terms}).{{0,40}}(?:{image_terms})(?:\s+of\s+(?P<t2>.+))?", q, flags=re.I)
    if m2:
        if m2.group('t2'):
            return True, m2.group('t2').strip()
        # Otherwise attempt to extract whatever comes after the match
        end = m2.end()
        trailing = q[end:].strip()
        if trailing:
            return True, trailing
        return True, q

    return False, None


class EditImageModal(discord.ui.Modal, title="Edit Image"):
    edit_prompt = discord.ui.TextInput(
        label="Edit Prompt",
        placeholder="Describe how you want to modify the image...",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )

    def __init__(self, original_prompt: str, width: Optional[int] = None, height: Optional[int] = None, model: Optional[str] = None):
        super().__init__()
        self.original_prompt = original_prompt
        self.width = width
        self.height = height
        self.model = model

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            new_prompt = f"{self.original_prompt}. Edit: {self.edit_prompt.value}"
            image_bytes = await fetch_pollinations_image(new_prompt, width=self.width, height=self.height, model_name=self.model)
            from io import BytesIO
            image_file = discord.File(BytesIO(image_bytes), filename="edited_image.png")

            embed = discord.Embed(title="✏️ Edited Image", description=f"**Prompt:** {new_prompt}", color=0x00FF7F)
            embed.set_image(url="attachment://edited_image.png")
            await interaction.followup.send(embed=embed, file=image_file)
        except Exception as e:
            await interaction.followup.send(f"Failed to edit image: {e}", ephemeral=True)


class PollinateButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.secondary, custom_id="regenerate-button")
    async def regenerate(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            # Extract prompt/model/dimensions from original embed
            if not interaction.message.embeds:
                await interaction.followup.send("Original embed not found.", ephemeral=True)
                return
            embed = interaction.message.embeds[0]
            # Prompt field may be in fields or description
            prompt = None
            for f in embed.fields:
                if f.name.lower() == "prompt":
                    prompt = f.value.strip('`')
                    break
            if not prompt:
                # Try description
                prompt = embed.description or ""

            # Get model and dimensions
            model = None
            width = None
            height = None
            for f in embed.fields:
                if f.name.lower() == "model":
                    model = f.value
                if f.name.lower() == "dimensions":
                    parts = f.value.split('x')
                    if len(parts) == 2:
                        try:
                            width = int(parts[0])
                            height = int(parts[1])
                        except Exception:
                            width = None
                            height = None

            image_bytes = await fetch_pollinations_image(prompt, width=width, height=height, model_name=model)
            from io import BytesIO
            file = discord.File(BytesIO(image_bytes), filename="regenerated.png")
            # Send new image as followup
            new_embed = discord.Embed(title="🔁 Regenerated Image", description=f"**Prompt:** {prompt}", color=0x00FF7F)
            new_embed.set_image(url="attachment://regenerated.png")
            await interaction.followup.send(embed=new_embed, file=file)
        except Exception as e:
            await interaction.followup.send(f"Failed to regenerate image: {e}", ephemeral=True)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, custom_id="edit-button")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.message.embeds:
                await interaction.response.send_message("Original embed not found.", ephemeral=True)
                return
            embed = interaction.message.embeds[0]
            prompt = None
            for f in embed.fields:
                if f.name.lower() == "prompt":
                    prompt = f.value.strip('`')
                    break
            # Extract width/height/model if present
            model = None
            width = None
            height = None
            for f in embed.fields:
                if f.name.lower() == "model":
                    model = f.value
                if f.name.lower() == "dimensions":
                    parts = f.value.split('x')
                    if len(parts) == 2:
                        try:
                            width = int(parts[0])
                            height = int(parts[1])
                        except Exception:
                            pass

            modal = EditImageModal(prompt or "", width=width, height=height, model=model)
            await interaction.response.send_modal(modal)
        except Exception as e:
            await interaction.response.send_message(f"Failed to open edit modal: {e}", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="delete-button")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            author_id = None
            try:
                author_id = interaction.message.interaction.user.id
            except Exception:
                pass
            if author_id and interaction.user.id != author_id and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("You don't have permission to delete this image.", ephemeral=True)
                return
            await interaction.message.delete()
            await interaction.response.send_message("Image deleted.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to delete image: {e}", ephemeral=True)

    @discord.ui.button(label="Bookmark", style=discord.ButtonStyle.secondary, custom_id="bookmark-button")
    async def bookmark(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.message.embeds:
                await interaction.response.send_message("Original embed not found.", ephemeral=True)
                return
            embed = interaction.message.embeds[0]
            url = embed.url or None
            dm_embed = discord.Embed(title="📌 Bookmarked Image", description=embed.fields[0].value if embed.fields else "", color=0x00FF7F)
            if url:
                dm_embed.add_field(name="Link", value=url, inline=False)
                dm_embed.set_image(url=url)
            await interaction.user.send(embed=dm_embed)
            await interaction.response.send_message("Bookmarked — sent to your DMs.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to bookmark image: {e}", ephemeral=True)


class PollinateNoEditView(discord.ui.View):
    """Same as PollinateButtonView but without the Edit button (for HF-generated images)."""
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.secondary, custom_id="regenerate-noedit")
    async def regenerate(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            if not interaction.message.embeds:
                await interaction.followup.send("Original embed not found.", ephemeral=True)
                return
            embed = interaction.message.embeds[0]
            prompt = None
            for f in embed.fields:
                if f.name.lower() == "prompt":
                    prompt = f.value.strip('`')
                    break
            if not prompt:
                prompt = embed.description or ""

            # Try to extract dimensions/model
            model = None
            width = None
            height = None
            for f in embed.fields:
                if f.name.lower() == "model":
                    model = f.value
                if f.name.lower() == "dimensions":
                    parts = f.value.split('x')
                    if len(parts) == 2:
                        try:
                            width = int(parts[0])
                            height = int(parts[1])
                        except Exception:
                            width = None
                            height = None

            # For HF-generated images we call make_image_request
            image_bytes = await make_image_request(prompt, width=width, height=height, model=os.getenv('HUGGINGFACE_MODEL'))
            from io import BytesIO
            file = discord.File(BytesIO(image_bytes), filename="regenerated.png")
            new_embed = discord.Embed(title="🔁 Regenerated Image", description=f"**Prompt:** {prompt}", color=0x00FF7F)
            new_embed.set_image(url="attachment://regenerated.png")
            await interaction.followup.send(embed=new_embed, file=file)
        except Exception as e:
            await interaction.followup.send(f"Failed to regenerate image: {e}", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="delete-noedit")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            author_id = None
            try:
                author_id = interaction.message.interaction.user.id
            except Exception:
                pass
            if author_id and interaction.user.id != author_id and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("You don't have permission to delete this image.", ephemeral=True)
                return
            await interaction.message.delete()
            await interaction.response.send_message("Image deleted.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to delete image: {e}", ephemeral=True)

    @discord.ui.button(label="Bookmark", style=discord.ButtonStyle.secondary, custom_id="bookmark-noedit")
    async def bookmark(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.message.embeds:
                await interaction.response.send_message("Original embed not found.", ephemeral=True)
                return
            embed = interaction.message.embeds[0]
            url = embed.url or None
            dm_embed = discord.Embed(title="📌 Bookmarked Image", description=embed.fields[0].value if embed.fields else "", color=0x00FF7F)
            if url:
                dm_embed.add_field(name="Link", value=url, inline=False)
                dm_embed.set_image(url=url)
            await interaction.user.send(embed=dm_embed)
            await interaction.response.send_message("Bookmarked — sent to your DMs.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to bookmark image: {e}", ephemeral=True)


load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix='!', intents=intents)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Logging: add file handlers for both human-readable and structured JSONL chat logs
LOG_DIR = Path(__file__).parent / "logs"
try:
    LOG_DIR.mkdir(exist_ok=True)
except Exception:
    # If directory creation fails, fallback to current directory
    LOG_DIR = Path('.')

# Human-readable chat log (kept for quick inspection)
chat_log_txt = LOG_DIR / 'chat_logs.txt'
file_handler = logging.FileHandler(str(chat_log_txt))
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Structured JSONL chat log for programmatic analysis (one JSON object per line)
CHAT_LOG_JSONL = LOG_DIR / 'chat_logs.jsonl'
def append_chat_log(entry: dict):
    """Append a JSON object as a single line to the JSONL chat log.

    This keeps a machine-friendly record of messages with metadata useful
    for analytics, replays, and debugging.
    """
    try:
        with CHAT_LOG_JSONL.open('a', encoding='utf-8') as jf:
            jf.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        # If the structured log fails, write a minimal fallback to the human log
        try:
            logger.error('Failed to append structured chat log entry')
        except Exception:
            pass


# --- Dice command (slash + text fallback) ---------------------------------
# Sends a rolling GIF then replaces it with a static dice face (1-6).
DICE_GIF_URL = "https://cdn.discordapp.com/attachments/1435569370389807144/1435585171658379385/ezgif-6882c768e3ab08.gif"
DICE_FACE_URLS = {
    1: "https://cdn.discordapp.com/attachments/1435569370389807144/1435586859098181632/Screenshot_20251105-153253copyad.png",
    2: "https://cdn.discordapp.com/attachments/1435569370389807144/1435587042154385510/2idce_2.png",
    3: "https://cdn.discordapp.com/attachments/1435569370389807144/1435589652353388565/3dice_1.png",
    4: "https://cdn.discordapp.com/attachments/1435569370389807144/1435585681987735582/Screenshot_20251105-153253copy.png",
    5: "https://cdn.discordapp.com/attachments/1435569370389807144/1435587924036026408/5dice_1.png",
    6: "https://cdn.discordapp.com/attachments/1435569370389807144/1435589024147570708/6dice_1.png",
}


def build_codes_embed(codes_list):
    """Build a gift codes embed for a list of codes.

    Placed near the top of the module so message-based triggers can call it
    before other definitions later in the file.
    """
    embed = discord.Embed(
        title="✨ Active Whiteout Survival Gift Codes ✨",
        color=0xffd700,
        description=f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    embed.set_thumbnail(url="https://i.postimg.cc/s2xHV7N7/Groovy-gift.gif")

    for code in (codes_list or [])[:10]:  # Limit to 10 codes
        name = f"🎟️ Code:"
        value = f"```{code.get('code','')}```\n*Rewards:* {code.get('rewards','Rewards not specified')}\n*Expires:* {code.get('expiry','Unknown')}"
        embed.add_field(name=name, value=value, inline=False)

    if codes_list and len(codes_list) > 10:
        embed.set_footer(text=f"And {len(codes_list) - 10} more codes...")
    else:
        embed.set_footer(text="Use /giftcode to see all active codes!")

    return embed


@bot.tree.command(name="dice", description="Roll a six-sided dice")
async def dice(interaction: discord.Interaction):
    """Slash command: shows rolling animation then edits to the result image."""
    try:
        # Defer the interaction so we can follow up and edit the message
        await interaction.response.defer(thinking=True)

        # Send the rolling GIF as an embed followup
        rolling_embed = discord.Embed(title=f"{interaction.user.display_name} rolls the dice...", color=0x2ecc71)
        rolling_embed.set_image(url=DICE_GIF_URL)
        rolling_msg = await interaction.followup.send(embed=rolling_embed)

        # Wait a bit to simulate rolling
        await asyncio.sleep(2.0)

        # Pick result and edit message to static face
        result = random.randint(1, 6)
        result_embed = discord.Embed(title=f"🎲 {interaction.user.display_name} rolled a {result}!", color=0x2ecc71)
        result_embed.set_image(url=DICE_FACE_URLS.get(result))

        try:
            await rolling_msg.edit(embed=result_embed)
        except Exception:
            # Fallback: send a new followup if edit fails
            await interaction.followup.send(embed=result_embed)

    except Exception as e:
        logger.error(f"Error in /dice command: {e}")
        try:
            await interaction.followup.send(content="Failed to roll the dice.")
        except Exception:
            pass


@bot.command(name='dice')
async def dice_text(ctx: commands.Context):
    """Text command fallback: !dice"""
    try:
        rolling_embed = discord.Embed(title=f"{ctx.author.display_name} rolls the dice...", color=0x2ecc71)
        rolling_embed.set_image(url=DICE_GIF_URL)
        rolling_msg = await ctx.send(embed=rolling_embed)

        await asyncio.sleep(2.0)

        result = random.randint(1, 6)
        result_embed = discord.Embed(title=f"🎲 {ctx.author.display_name} rolled a {result}!", color=0x2ecc71)
        result_embed.set_image(url=DICE_FACE_URLS.get(result))

        try:
            await rolling_msg.edit(embed=result_embed)
        except Exception:
            await ctx.send(embed=result_embed)
    except Exception as e:
        logger.error(f"Error in !dice command: {e}")
        try:
            await ctx.send("Failed to roll the dice.")
        except Exception:
            pass


# ---------- Birthday command and storage ---------------------------------
BIRTHDAY_FILE = Path(__file__).parent / "birthdays.json"

# Notify channel helper: read channel ID from env var BIRTHDAY_NOTIFY_CHANNEL
def get_notify_channel_id_from_env() -> Optional[int]:
    env_val = os.getenv('BIRTHDAY_NOTIFY_CHANNEL')
    if not env_val:
        return None
    try:
        return int(env_val)
    except Exception:
        logger.error(f"Invalid BIRTHDAY_NOTIFY_CHANNEL env var: {env_val}")
        return None

def load_birthdays() -> dict:
    try:
        if BIRTHDAY_FILE.exists():
            with BIRTHDAY_FILE.open('r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load birthdays file: {e}")
    return {}

def save_birthdays(data: dict) -> bool:
    try:
        with BIRTHDAY_FILE.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save birthdays file: {e}")
        return False

def set_birthday(user_id: int, day: int, month: int) -> None:
    data = load_birthdays()
    data[str(user_id)] = {"day": int(day), "month": int(month)}
    save_birthdays(data)

def remove_birthday(user_id: int) -> bool:
    data = load_birthdays()
    if str(user_id) in data:
        try:
            del data[str(user_id)]
            save_birthdays(data)
            return True
        except Exception as e:
            logger.error(f"Failed to remove birthday for {user_id}: {e}")
            return False
    return False

def get_birthday(user_id: int):
    data = load_birthdays()
    return data.get(str(user_id))


class BirthdayModal(discord.ui.Modal, title="Add / Update Birthday"):
    day = discord.ui.TextInput(label="Day (1-31)", placeholder="e.g. 23", required=True, max_length=2)
    month = discord.ui.TextInput(label="Month (1-12)", placeholder="e.g. 7", required=True, max_length=2)

    def __init__(self, target_user: Optional[discord.User] = None):
        super().__init__()
        self.target_user = target_user

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validate inputs
            try:
                d = int(self.day.value.strip())
                m = int(self.month.value.strip())
            except Exception:
                await interaction.response.send_message("Please enter numeric values for day and month.", ephemeral=True)
                return

            if not (1 <= m <= 12):
                await interaction.response.send_message("Month must be between 1 and 12.", ephemeral=True)
                return
            if not (1 <= d <= 31):
                await interaction.response.send_message("Day must be between 1 and 31.", ephemeral=True)
                return

            user = interaction.user
            user_id = user.id if self.target_user is None else self.target_user.id

            # Check previous entry to determine if this is new or an update
            prev = get_birthday(user_id)

            # If submitting for self and an entry already exists, require removal first
            if prev and self.target_user is None:
                await interaction.response.send_message(
                    "You already have a birthday saved. To change it, first remove your existing entry using 'Remove my entry', then add a new birthday.",
                    ephemeral=True
                )
                return

            # Otherwise save (this allows overwriting when target_user is set — e.g., admin use)
            set_birthday(user_id, d, m)

            await interaction.response.send_message(f"Saved birthday for <@{user_id}>: {d}/{m}", ephemeral=True)

            # Notify configured channel (per-guild or env fallback) with a detailed embed
            try:
                # Read notify channel id from env var (BIRTHDAY_NOTIFY_CHANNEL)
                notify_id = get_notify_channel_id_from_env()

                if notify_id is not None:
                    channel = bot.get_channel(notify_id)
                    if channel is None:
                        try:
                            channel = await bot.fetch_channel(notify_id)
                        except Exception:
                            channel = None

                    if channel is not None:
                        status = "Updated" if prev else "New Entry"
                        info_embed = discord.Embed(title="🎉 Birthday Submitted", color=0xff69b4, timestamp=datetime.utcnow())
                        info_embed.add_field(name="User", value=f"{user.mention} ({user})", inline=False)
                        info_embed.add_field(name="User ID", value=str(user_id), inline=True)
                        # If target_user differs, show target
                        if self.target_user is not None:
                            info_embed.add_field(name="Target User", value=f"{self.target_user.mention} ({self.target_user.id})", inline=True)
                        info_embed.add_field(name="Day", value=str(d), inline=True)
                        info_embed.add_field(name="Month", value=str(m), inline=True)
                        info_embed.add_field(name="Action", value=status, inline=True)
                        if interaction.guild:
                            info_embed.add_field(name="Guild", value=f"{interaction.guild.name} ({interaction.guild.id})", inline=False)

                        info_embed.set_footer(text="Birthday manager")

                        try:
                            await channel.send(embed=info_embed)
                        except Exception as send_err:
                            logger.error(f"Failed to send birthday notification to channel {notify_id}: {send_err}")
                    else:
                        logger.error(f"Birthday notify channel {notify_id} not found or inaccessible.")
                else:
                    # No configured notify channel; nothing to do
                    logger.debug("No birthday notify channel configured for this guild or via env var.")
            except Exception as notify_exc:
                logger.error(f"Error while notifying birthday channel: {notify_exc}")
        except Exception as e:
            logger.error(f"Error in BirthdayModal.on_submit: {e}")
            await interaction.response.send_message("Failed to save birthday.", ephemeral=True)


class BirthdayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Add/Update birthday", style=discord.ButtonStyle.primary, custom_id="birthday_add_update")
    async def add_update(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Prevent users from creating multiple entries: if they already have one, instruct to remove first
            existing = get_birthday(interaction.user.id)
            if existing:
                await interaction.response.send_message(
                    "You already have a birthday saved. To change it, first click 'Remove my entry' to delete your existing entry, then click 'Add/Update birthday' to submit a new one.",
                    ephemeral=True
                )
                return

            modal = BirthdayModal()
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"Error opening BirthdayModal: {e}")
            await interaction.response.send_message("Failed to open birthday form.", ephemeral=True)

    @discord.ui.button(label="Remove my entry", style=discord.ButtonStyle.danger, custom_id="birthday_remove")
    async def remove_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            removed = remove_birthday(interaction.user.id)
            if removed:
                await interaction.response.send_message("Your birthday entry was removed.", ephemeral=True)

                # Send removal notification to configured notify channel (env var)
                try:
                    notify_id = get_notify_channel_id_from_env()
                    if notify_id:
                        channel = bot.get_channel(notify_id)
                        if channel is None:
                            try:
                                channel = await bot.fetch_channel(notify_id)
                            except Exception:
                                channel = None

                        if channel is not None:
                            info_embed = discord.Embed(title="🗑️ Birthday Removed", color=0xff69b4, timestamp=datetime.utcnow())
                            info_embed.add_field(name="User", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
                            info_embed.add_field(name="User ID", value=str(interaction.user.id), inline=True)
                            # Try to include guild info if available
                            if interaction.guild:
                                info_embed.add_field(name="Guild", value=f"{interaction.guild.name} ({interaction.guild.id})", inline=False)

                            info_embed.set_footer(text="Birthday manager")

                            try:
                                await channel.send(embed=info_embed)
                            except Exception as send_err:
                                logger.error(f"Failed to send birthday removal notification to channel {notify_id}: {send_err}")
                        else:
                            logger.error(f"Birthday notify channel {notify_id} not found or inaccessible.")
                    else:
                        logger.debug("No BIRTHDAY_NOTIFY_CHANNEL configured; skipping removal notification.")
                except Exception as notify_exc:
                    logger.error(f"Error while notifying birthday removal channel: {notify_exc}")
            else:
                await interaction.response.send_message("No birthday entry found for you.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error removing birthday: {e}")
            await interaction.response.send_message("Failed to remove your entry.", ephemeral=True)


@bot.tree.command(name="birthday", description="Manage your birthday entry (day & month)")
async def birthday(interaction: discord.Interaction):
    """Sends an embed explaining the birthday system with buttons to add/update or remove your birthday."""
    try:
        embed_text = (
            "**🎉 Let's never miss a birthday again!**\n\n"
            
            "🎂 Click “Add Birthday”\n\n"
            "📅 Choose day & month\n\n"
            "🥳 Your day gets celebrated – party vibes guaranteed!\n\n"
            "🔄 Update? Just click the button again\n\n"
            "✨ More entries = more fun & more party vibes! 🎉🎈"
        )

        embed = discord.Embed(title="Birthday Manager", description=embed_text, color=0xff69b4)
        embed.set_image(url="https://cdn.discordapp.com/attachments/1435569370389807144/1435875606632988672/v04HfJr.png?ex=690d8edd&is=690c3d5d&hm=83662954ad3897d2b39763d40c347e27222018839a178420a57eb643ffbc3542")

        view = BirthdayView()
        await interaction.response.send_message(embed=embed, view=view)
    except Exception as e:
        logger.error(f"Error in /birthday command: {e}")
        try:
            await interaction.response.send_message("Failed to send birthday manager.", ephemeral=True)
        except Exception:
            pass


# /birthday_setchannel removed — notification channel is read from the BIRTHDAY_NOTIFY_CHANNEL env var

@bot.event
async def on_message(message: discord.Message):
    """Minimal early on_message handler.

    This handler no longer performs dice detection to avoid duplicate
    triggers — the main comprehensive `on_message` later in the file
    handles conversation, DMs, logging, and dice. Here we only pass
    prefixed commands to the command processor.
    """
    try:
        if message.author.bot:
            return

        # If user is invoking a prefixed command, let command processor handle it
        content = (message.content or "").strip()
        if content.startswith(bot.command_prefix):
            await bot.process_commands(message)
            return

        # Otherwise, do nothing and allow other listeners/handlers to run.
        return
    except Exception as e:
        logger.error(f"Unexpected error in on_message (early handler): {e}")


# Reduce noise: silence informational logs from the gift_codes module (it's verbose)
logging.getLogger('gift_codes').setLevel(logging.WARNING)


# Global exception hook to log uncaught exceptions
def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        # Let keyboard interrupts be handled normally
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_uncaught_exception

# Asyncio exception handler
def asyncio_exception_handler(loop, context):
    msg = context.get("exception", context.get("message"))
    logger.critical(f"Asyncio exception: {msg}")

try:
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(asyncio_exception_handler)
except Exception:
    # In case event loop isn't set up yet
    pass

# Signal handlers to log terminations
def _signal_handler(sig, frame):
    logger.warning(f"Received signal {sig}; shutting down gracefully...")

try:
    signal.signal(signal.SIGINT, _signal_handler)
except Exception:
    pass
try:
    signal.signal(signal.SIGTERM, _signal_handler)
except Exception:
    pass

# Initialize systems
reminder_system = ReminderSystem(bot)
thinking_animation = ThinkingAnimation()

# Health server flag
health_server_started = False

# Conversation history storage: user_id -> list of message dicts
conversation_history = {}

@bot.event
async def on_ready():
    try:
        logger.info(f'{bot.user} has connected to Discord!')
        # Start lightweight health server so Render sees an open port (for uptime pings)
        global health_server_started
        if not health_server_started:
            try:
                port = int(os.getenv('PORT', 8080))
            except Exception:
                port = 8080
            try:
                bot.loop.create_task(health_server.start_health_server())
                health_server_started = True
                logger.info(f'Health server task started on port {port}')
            except Exception as hs_err:
                logger.error(f'Failed to start health server: {hs_err}')

            # Start uptime checker task (monitors health URL and posts to channel on changes)
            try:
                bot.loop.create_task(uptime_checker.start_uptime_checker(bot))
                logger.info('Uptime checker task started')
            except Exception as uc_err:
                logger.error(f'Failed to start uptime checker: {uc_err}')

            # Start giftcode poster task (periodically checks wosgiftcodes and posts new codes)
            try:
                bot.loop.create_task(giftcode_poster.start_poster(bot))
                logger.info('Giftcode poster task started')
            except Exception as gp_err:
                logger.error(f'Failed to start giftcode poster: {gp_err}')
        
        # Music cog removed — skip loading to prevent music slash commands from registering
        try:
            # music_cog.py removed; skipping load
            logger.info('Skipping music cog load (file removed)')


            
            # Force sync all commands after loading cog
            await bot.tree.sync()
            logger.info('Successfully synced global commands')
            
        except Exception as e:
            logger.error(f'Error loading music cog: {str(e)}')
            import traceback
            logger.error(traceback.format_exc())
                
        # If a GUILD_ID is provided, do guild-specific sync for faster testing
        if os.getenv('GUILD_ID'):
            guild_id = int(os.getenv('GUILD_ID'))
            guild = discord.Object(id=guild_id)
            bot.tree.copy_global_to(guild=guild)
            # Force sync commands to guild immediately
            await bot.tree.sync(guild=guild)
            logger.info(f'Synced commands to guild {guild_id}')
        else:
            # Global sync for production
            await bot.tree.sync()
            logger.info('Synced commands globally')

            # Hide music-related slash commands while music is under maintenance.
            # This removes the app commands from the global tree so they don't appear to users.
            try:
                # Also remove giftcode/timezone related commands that have been deprecated/removed
                music_commands = ['play', 'pause', 'resume', 'skip', 'stop', 'queue', 'leave',
                                  'giftcode_check', 'giftchannel', 'list_gift_channel', 'show_timezone']
                for cmd_name in music_commands:
                    try:
                        bot.tree.remove_command(cmd_name)
                        logger.info(f"Removed music command '{cmd_name}' (maintenance)")
                    except Exception:
                        # Not found or couldn't remove; ignore silently
                        logger.debug(f"Music command '{cmd_name}' not found or could not be removed")
                # Push removal to Discord so the commands disappear from the UI
                try:
                    await bot.tree.sync()
                    logger.info('Synced command removals to Discord (music commands hidden)')
                except Exception as sync_err:
                    logger.error(f'Failed to sync command removals: {sync_err}')
            except Exception as e:
                logger.error(f"Failed to hide music commands: {e}")

        # Start the reminder checking task
        reminder_system.check_reminders.start()

    except Exception as e:
        logger.error(f'Error in on_ready: {e}')

@bot.event
async def on_message(message):
    # Log non-bot messages with guild, channel, author, and content
    if not message.author.bot:
        guild_name = message.guild.name if message.guild else "DM"
        guild_id = message.guild.id if message.guild else "DM"
        channel_name = message.channel.name if hasattr(message.channel, 'name') else "DM"
        channel_id = message.channel.id
        author_name = message.author.display_name
        author_id = message.author.id
        content = (message.content or "").replace('\n', ' ').strip()  # Replace newlines for single line log

        # Collect attachments (URLs) if present
        try:
            attachments = [att.url for att in message.attachments]
        except Exception:
            attachments = []

        # Summarize embeds to keep logs small but useful
        embeds_summary = []
        try:
            for emb in getattr(message, 'embeds', []) or []:
                s = {}
                if getattr(emb, 'title', None):
                    s['title'] = emb.title
                if getattr(emb, 'description', None):
                    s['description'] = (emb.description[:200] + '...') if len(emb.description) > 200 else emb.description
                if getattr(emb, 'url', None):
                    s['url'] = emb.url
                embeds_summary.append(s)
        except Exception:
            embeds_summary = []

        # Reply reference (if message is a reply)
        reply_to = None
        try:
            if message.reference and getattr(message.reference, 'message_id', None):
                reply_to = message.reference.message_id
        except Exception:
            reply_to = None

        # Build structured log entry
        entry = {
            'timestamp': (message.created_at.isoformat() + 'Z') if getattr(message, 'created_at', None) else datetime.utcnow().isoformat() + 'Z',
            'event': 'message',
            'guild': {'id': guild_id, 'name': guild_name},
            'channel': {'id': channel_id, 'name': channel_name},
            'author': {'id': author_id, 'display_name': author_name, 'bot': getattr(message.author, 'bot', False)},
            'message_id': getattr(message, 'id', None),
            'reply_to': reply_to,
            'content': content,
            'attachments': attachments,
            'embeds': embeds_summary,
            'is_dm': isinstance(message.channel, discord.DMChannel)
        }

        # Append structured JSONL entry (best-effort)
        try:
            append_chat_log(entry)
        except Exception:
            # append_chat_log already swallows errors; keep going
            pass

        # Also write a concise human-readable log line for quick inspection
        try:
            summary = f"[GUILD: {guild_name} ({guild_id})] [CHANNEL: {channel_name} ({channel_id})] [AUTHOR: {author_name} ({author_id})] msg_id={entry.get('message_id')} attachments={len(attachments)}"
            logger.info(summary + f" Content: {content[:400]}")
        except Exception:
            # Last resort fallback
            logger.info(f"Message from {author_id} in {channel_id}: {content[:200]}")

    # If this is a DM, handle like /ask but respond in plain text
    if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
        try:
            question = (message.content or "").strip()
            if not question:
                return

            user_id = str(message.author.id)
            user_name = get_known_user_name(user_id) or message.author.display_name or message.author.name

            # Image generation shortcut — use robust detector that matches keywords anywhere
            matched, prompt = detect_image_request(question)
            if matched:
                # If detect_image_request failed to extract a prompt, preserve prior fallback
                if not prompt:
                    prompt = question.split(" ", 3)[-1] if len(question.split(" ", 3)) > 3 else question
                has_hf = any(k.startswith('HUGGINGFACE_API_TOKEN') for k in os.environ.keys())
                has_openai = bool(os.getenv('OPENAI_API_KEY'))
                async with message.channel.typing():
                    try:
                        # Always use Pollinations public endpoint for DM image requests
                        image_bytes = await fetch_pollinations_image(prompt)
                        from io import BytesIO
                        file = discord.File(BytesIO(image_bytes), filename="generated_image.png")
                        # Send a simple DM reply without echoing the user's prompt
                        await message.channel.send(content="Here is your image.", file=file)
                    except Exception as e:
                        logger.error(f"DM image generation failed: {e}")
                        await message.channel.send("❌ Image generation failed. Please try again later.")
                return

            # Prepare conversation messages and call the API
            # If the message appears to be a Bear Trap / Bear Hunt question, answer from local guide using RAG
            try:
                if is_beartrap_question(question):
                    async with message.channel.typing():
                        answer = answer_beartrap_question(question)
                        chunks = [answer[i:i+2000] for i in range(0, len(answer), 2000)]
                        for ch in chunks:
                            await message.channel.send(ch)
                    return
            except Exception as e:
                logger.error(f"Error in beartrap RAG responder (DM): {e}")

            history = conversation_history.get(user_id, [])
            system = {"role": "system", "content": get_system_prompt(user_name)}
            messages = [system] + history[-10:] + [{"role": "user", "content": question}]
            async with message.channel.typing():
                response = await make_request(messages=messages, max_tokens=1000, include_sheet_data=True)

            # Special handling
            if response.startswith("ALLIANCE_MESSAGES:"):
                try:
                    alliance_messages = json.loads(response[len("ALLIANCE_MESSAGES:"):])
                    for msg in alliance_messages:
                        await message.channel.send(msg)
                except Exception as e:
                    logger.error(f"Failed to send alliance messages in DM: {e}")
                    await message.channel.send("❌ Error displaying alliance information. Please try again.")
                return
            if response.startswith("REMINDER_REQUEST:"):
                await message.channel.send("Reminder request received. Please use the dashboard to configure reminders.")
                return

            # Update history
            user_message = {"role": "user", "content": question}
            assistant_message = {"role": "assistant", "content": response}
            history.append(user_message)
            history.append(assistant_message)
            if len(history) > 10:
                history = history[-10:]
            conversation_history[user_id] = history

            # Send plain-text response, chunked to 2000 chars
            chunks = [response[i:i+2000] for i in range(0, len(response), 2000)]
            if chunks:
                # Do not echo the user's question; send only the assistant response
                await message.channel.send(chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
            else:
                await message.channel.send("(No response generated)")

        except Exception as dm_err:
            logger.error(f"Error handling DM message: {dm_err}")
            try:
                await message.channel.send("❌ An error occurred while processing your message. Please try again later.")
            except Exception:
                pass
        return

    # If message is in a guild (not a DM), listen for the bare word 'dice' and trigger the dice flow
    try:
        if not isinstance(message.channel, discord.DMChannel):
            # Ignore messages from bots
            if not message.author.bot:
                content = (message.content or "").strip()
                # Avoid triggering on prefixed commands (they'll be handled by the commands processor)
                if not content.startswith(bot.command_prefix):
                    # If a user mentions 'giftcode' as plain text, show the gift codes but DO NOT delete the user's message
                    if re.search(r"\bgiftcode\b", content, flags=re.I):
                        try:
                            codes = await get_active_gift_codes()
                            if not codes:
                                await message.channel.send("No active gift codes available right now. Check back later! 🎁")
                            else:
                                embed = build_codes_embed(codes)
                                view = GiftCodeView(codes)
                                sent = await message.channel.send(content=f"{message.author.display_name} requested gift codes", embed=embed, view=view)
                                try:
                                    view.message = sent
                                except Exception:
                                    logger.debug("Could not attach message reference to GiftCodeView (message-trigger)")
                        except Exception as e:
                            logger.error(f"Error handling giftcode message trigger: {e}")
                    # Match the bare word "dice" or variants of "roll" (case-insensitive)
                    # matches: dice, roll, rolls, rolled, rolling
                    elif re.search(r"\b(?:dice|roll(?:ed|s|ing)?)\b", content, flags=re.I):
                        try:
                            # If the bot has Manage Messages (or Administrator), delete the triggering user message first
                            try:
                                guild_me = message.guild.me if message.guild else None
                                has_manage = False
                                if guild_me is not None:
                                    perms = message.channel.permissions_for(guild_me)
                                    has_manage = perms.manage_messages or perms.administrator
                                if has_manage:
                                    try:
                                        await message.delete()
                                    except Exception as del_exc:
                                        # Log but continue rolling
                                        logger.debug(f"Failed to delete triggering message: {del_exc}")
                            except Exception:
                                # Non-fatal: continue even if permission checks fail
                                pass

                            rolling_embed = discord.Embed(title=f"{message.author.display_name} rolls the dice...", color=0x2ecc71)
                            rolling_embed.set_image(url=DICE_GIF_URL)
                            rolling_msg = await message.channel.send(embed=rolling_embed)

                            await asyncio.sleep(2.0)

                            result = random.randint(1, 6)
                            result_embed = discord.Embed(title=f"🎲 {message.author.display_name} rolled a {result}!", color=0x2ecc71)
                            result_embed.set_image(url=DICE_FACE_URLS.get(result))

                            try:
                                await rolling_msg.edit(embed=result_embed)
                            except Exception:
                                await message.channel.send(embed=result_embed)
                        except Exception as e:
                            logger.error(f"Error rolling dice on message trigger: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in on_message dice detection: {e}")

    # Process commands
    await bot.process_commands(message)

async def event_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=EVENT_TIPS[event_id]['name'], value=event_id)
        for event_id in EVENT_TIPS.keys()
        if current.lower() in event_id.lower() or current.lower() in EVENT_TIPS[event_id]['name'].lower()
    ]

@bot.tree.command(name="event", description="Get information about an event")
@app_commands.describe(event_name="Type the event name (e.g. bear, foundry)")
@app_commands.autocomplete(event_name=event_autocomplete)
async def event(interaction: discord.Interaction, event_name: str):
    try:
        # Show thinking animation while processing
        await thinking_animation.show_thinking(interaction)

        event_info = get_event_info(event_name.lower())
        if not event_info:
            error_embed = discord.Embed(
                title="❌ Event Not Found",
                description=f"Event '{event_name}' not found. Try using the autocomplete suggestions.",
                color=0xff0000
            )
            # Try to edit animation message with error
            if thinking_animation.animation_message:
                try:
                    await thinking_animation.animation_message.edit(embed=error_embed)
                    logger.info("Successfully edited animation message with event not found error")
                except Exception as edit_error:
                    logger.error(f"Failed to edit animation message with error: {edit_error}")
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"{event_info['name']}",
            color=0x1abc9c
        )

        description = "📚 Resources\n"
        if event_info.get('guide'):
            description += f"📖 Guide: [Click here to view guide]({event_info['guide']})\n"
        if event_info.get('video'):
            description += f"🎬 Video: [Watch tutorial video]({event_info['video']})\n"
        description += "💡 Tips & Strategies\n"
        description += event_info.get('tips', 'Tips coming soon...')

        embed.description = description

        # Stop the animation before editing the message
        await thinking_animation.stop_thinking(interaction, delete_message=False)

        # Edit the animation message with the event information
        if thinking_animation.animation_message:
            try:
                await thinking_animation.animation_message.edit(
                    content=f"{interaction.user.display_name} requested info about: `{event_name}`",
                    embed=embed
                )
                logger.info("Successfully edited animation message with event information")
            except Exception as edit_error:
                logger.error(f"Failed to edit animation message with event info: {edit_error}")
                # Fallback to followup send
                await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in event command: {e}")
        error_embed = discord.Embed(
            title="❌ Error Getting Event Information",
            description="I encountered an error while fetching event information. Please try again.",
            color=0xff0000
        )
        try:
            # Try to edit animation message with error
            if thinking_animation.animation_message:
                await thinking_animation.animation_message.edit(embed=error_embed)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
        except Exception as edit_error:
            logger.error(f"Failed to send error message: {edit_error}")
            # Final fallback
            try:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
            except Exception as final_error:
                logger.error(f"Failed to send final error message: {final_error}")



@bot.tree.command(name="ask", description="Ask a question or get help with anything!")
@app_commands.describe(question="Your question or message")
async def ask(interaction: discord.Interaction, question: str):
    # Show thinking animation while processing
    await thinking_animation.show_thinking(interaction)
    
    try:
        # Get user personalized info
        user_id = str(interaction.user.id)
        user_name = get_known_user_name(user_id) or interaction.user.display_name or interaction.user.name

        # Check if the question is an image creation request using the robust detector
        matched, prompt = detect_image_request(question)
        if matched:
            if not prompt:
                prompt = question.split(" ", 3)[-1] if len(question.split(" ", 3)) > 3 else question

            # Call the imagine command logic directly
            # Show thinking animation while processing
            await thinking_animation.show_thinking(interaction)

            try:
                # Generate the image using Pollinations public endpoint (always use Pollinations for /ask image requests)
                image_data = await fetch_pollinations_image(prompt)

                # Stop the animation and delete the message so image can "pop over"
                await thinking_animation.stop_thinking(interaction, delete_message=True)

                # Wait a moment to ensure animation message is deleted
                await asyncio.sleep(0.1)

                # Create a file from the image data
                from io import BytesIO
                image_file = discord.File(BytesIO(image_data), filename="generated_image.png")

                # Create success embed
                success_embed = discord.Embed(
                    title="🎨 Image Generated Successfully!",
                    description=f"**Prompt:** {prompt}",
                    color=0x00FF7F
                )
                success_embed.set_footer(text=f"Generated for {interaction.user.display_name}")
                success_embed.set_thumbnail(url="https://i.postimg.cc/rmvm9ygB/6a2065b5-1bc3-41db-a5f6-b948e7151810-removebg-preview.png?width=50")

                # Send the image in a new message (animation disappears, image pops over)
                await interaction.followup.send(
                    content=f"{interaction.user.mention}",
                    embed=success_embed,
                    file=image_file
                )
                logger.info("Successfully sent image in new message after animation deletion")

            except Exception as e:
                logger.error(f"Error in image generation from ask command: {str(e)}")
                error_embed = discord.Embed(
                    title="❌ Image Generation Failed",
                    description="Sorry, I couldn't generate your image right now. Please try again later or check your prompt.",
                    color=0xff0000
                )
                try:
                    # Try to edit animation message with error
                    if thinking_animation.animation_message:
                        await thinking_animation.animation_message.edit(embed=error_embed)
                    else:
                        await interaction.followup.send(embed=error_embed)
                except Exception as edit_error:
                    logger.error(f"Failed to send error message: {edit_error}")
                    # Final fallback
                    try:
                        await interaction.followup.send(embed=error_embed)
                    except Exception as final_error:
                        logger.error(f"Failed to send final error message: {final_error}")
            return



        # Get conversation history for this user (last 10 messages, i.e., last 5 conversations)
        history = conversation_history.get(user_id, [])

        # If this looks like a Bear Trap question, reply from local guide (RAG) instead of the LLM
        try:
            if is_beartrap_question(question):
                answer = answer_beartrap_question(question)
                # Stop the thinking animation and send the answer as followup
                await thinking_animation.stop_thinking(interaction, delete_message=True)
                chunks = [answer[i:i+4096] for i in range(0, len(answer), 4096)]
                for idx, ch in enumerate(chunks):
                    if idx == 0:
                        await interaction.followup.send(content=f"{interaction.user.mention}", embed=discord.Embed(description=ch, color=0x9b59b6))
                    else:
                        await interaction.followup.send(embed=discord.Embed(description=ch, color=0x9b59b6))
                return
        except Exception as e:
            logger.error(f"Error in beartrap RAG responder (/ask): {e}")

        # Prepare the API request
        system = {
            "role": "system",
            "content": get_system_prompt(user_name)
        }
        messages = [system] + history[-10:] + [{"role": "user", "content": question}]

        # Make API request
        response = await make_request(
            messages=messages,
            max_tokens=1000,
            include_sheet_data=True  # Include both alliance and event data
        )
        
        # Check if this is a multi-message alliance response
        if response.startswith("ALLIANCE_MESSAGES:"):
            try:
                # Parse the alliance messages
                alliance_messages = json.loads(response[len("ALLIANCE_MESSAGES:"):])
                
                # Send each message in sequence
                for idx, msg in enumerate(alliance_messages):
                    if idx == 0:
                        # For first message, use already deferred response
                        await interaction.followup.send(msg)
                    else:
                        # For subsequent messages, send as followup
                        await interaction.followup.send(f"{msg}")
                return
            except Exception as e:
                logger.error(f"Failed to send alliance messages: {e}", exc_info=True)
                await interaction.followup.send("❌ Error displaying alliance information. Please try again.")
                return

        # Check if response is a reminder request
        if response.startswith("REMINDER_REQUEST:"):
            # Parse the reminder parameters
            try:
                params_str = response[len("REMINDER_REQUEST:"):].strip()
                # Expected format: time=[time], message=[message], channel=[channel], mention=[everyone|user|none]
                params = {}
                for param in params_str.split(", "):
                    if "=" in param:
                        key, value = param.split("=", 1)
                        params[key.strip()] = value.strip()

                time_part = params.get("time", "")
                message_part = params.get("message", "")
                channel_part = params.get("channel", "current")
                mention_part = params.get("mention", "user")

                if not time_part or not message_part:
                    await interaction.followup.send("❌ Invalid reminder format. Please try again.", ephemeral=True)
                    return

                # Determine target channel (optional for /ask command)
                if channel_part and channel_part != "current":
                    # Try to find the channel by name or mention
                    target_channel = None
                    for channel in interaction.guild.channels:
                        if channel.name == channel_part or f"<#{channel.id}>" == channel_part:
                            target_channel = channel
                            break
                    if not target_channel:
                        target_channel = interaction.channel  # fallback
                else:
                    # Default to current channel if no channel specified or "current"
                    target_channel = interaction.channel

                # Determine mention type based on user's input, not AI decision
                user_question_lower = question.lower()
                if "remind everyone" in user_question_lower or "@everyone" in user_question_lower:
                    mention_type = "everyone"
                else:
                    mention_type = "user"

                # Create the reminder with determined mention type
                success = await reminder_system.create_reminder(interaction, time_part, message_part, target_channel, mention=mention_type)
                if success:
                    # Stop the animation and delete the message before sending success
                    await thinking_animation.stop_thinking(interaction, delete_message=True)
                    await interaction.followup.send(f"✅ Reminder set for {time_part}: {message_part} in {target_channel.mention}")
                else:
                    await interaction.followup.send("❌ Failed to set reminder. Please check the time format.", ephemeral=True)
            except Exception as e:
                logger.error(f"Error parsing reminder request: {e}")
                await interaction.followup.send("❌ Error setting reminder. Please try again.", ephemeral=True)
            return

        elif response.startswith("REMINDER_DECLINE:"):
            # Send the decline message
            decline_message = response[len("REMINDER_DECLINE:"):].strip()
            await interaction.followup.send(f"❌ {decline_message}", ephemeral=True)
            return

        # Update conversation history for normal responses
        user_message = {"role": "user", "content": question}
        assistant_message = {"role": "assistant", "content": response}
        history.append(user_message)
        history.append(assistant_message)
        # Keep only last 10 messages (5 conversations)
        if len(history) > 10:
            history = history[-10:]
        conversation_history[user_id] = history

        # Stop the animation and delete the animation message before sending response
        await thinking_animation.stop_thinking(interaction, delete_message=True)

        if len(response) <= 4096:
            # Single embed response - send as followup message
            final_embed = discord.Embed(
                description=response,
                color=0x9b59b6
            )
            final_embed.set_thumbnail(url="https://i.postimg.cc/rmvm9ygB/6a2065b5-1bc3-41db-a5f6-b948e7151810-removebg-preview.png?width=50")

            await interaction.followup.send(
                content=f"{interaction.user.display_name} asked: `{question}`",
                embed=final_embed
            )

        else:
            # Multi-part response for long messages
            chunks = [response[i:i+4096] for i in range(0, len(response), 4096)]

            # Send first chunk as followup
            first_embed = discord.Embed(
                description=chunks[0],
                color=0x9b59b6
            )
            first_embed.set_author(name=f"Response to {interaction.user.display_name}'s question")
            await interaction.followup.send(
                content=f"Question: `{question}`",
                embed=first_embed
            )

            # Send remaining chunks as followups
            for chunk in chunks[1:]:
                chunk_embed = discord.Embed(
                    description=chunk,
                    color=0x9b59b6
                )
                await interaction.followup.send(embed=chunk_embed)

            # Add logo to last message
            if len(chunks) > 1:
                last_embed = discord.Embed(
                    description=f"{chunks[-1]}\n\n⠀",
                    color=0x9b59b6
                )
                last_embed.set_thumbnail(url="https://i.postimg.cc/rmvm9ygB/6a2065b5-1bc3-41db-a5f6-b948e7151810-removebg-preview.png?width=50")
                await interaction.followup.send(embed=last_embed)

    except Exception as e:
        logger.error(f"Error in ask command: {e}")
        error_embed = discord.Embed(
            title="❌ Error Processing Request",
            description="I encountered an error while processing your question. Please try again.",
            color=0xff0000
        )
        try:
            await interaction.followup.send(embed=error_embed)
        except Exception as followup_error:
            logger.error(f"Failed to send error followup: {followup_error}")
            # If followup fails, try response
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed)

@bot.tree.command(name="add_trait", description="Add a personality trait to your profile")
@app_commands.describe(trait="The trait to add to your profile")
async def add_trait(interaction: discord.Interaction, trait: str):
    try:
        user_id = str(interaction.user.id)
        angel_personality.add_user_trait(user_id, trait)
        await interaction.response.send_message(f"Added trait '{trait}' to your profile, {interaction.user.name}! Your Angel responses will now be more personalized.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in add_trait command: {str(e)}")
        await interaction.response.send_message("Sorry, there was an error adding your trait. Please try again.", ephemeral=True)


class GiftCodeView(discord.ui.View):
    """Interactive view for gift code embeds (Copy & Refresh buttons).

    This was previously defined inside the /giftcode command; it's been
    moved to top-level so message-triggered giftcode embeds can reuse it.
    """
    def __init__(self, codes_list):
        super().__init__(timeout=300)
        self.codes = codes_list or []
        self.message = None

    @discord.ui.button(label="Copy Code", style=discord.ButtonStyle.primary, custom_id="giftcode_copy")
    async def copy_button(self, interaction_button: discord.Interaction, button: discord.ui.Button):
        if not self.codes:
            try:
                await interaction_button.response.send_message("No gift codes available to copy.", ephemeral=True)
            except Exception:
                logger.debug("Failed to send ephemeral no-codes message")
            return

        code_list = [c.get('code', '').strip() for c in self.codes if c.get('code')]
        if not code_list:
            try:
                await interaction_button.response.send_message("Couldn't find any codes to copy.", ephemeral=True)
            except Exception:
                logger.debug("Failed to send ephemeral no-code-found message")
            return

        plain_text = "\n".join(code_list)
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
            new_codes = await get_active_gift_codes()
            if not new_codes:
                await interaction_button.followup.send("No active gift codes available right now.", ephemeral=True)
                return

            self.codes = new_codes
            # Use same embed builder from the command; import will capture function in scope
            new_embed = build_codes_embed(self.codes)

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

@bot.tree.command(name="giftcode", description="Get active Whiteout Survival gift codes")
async def giftcode(interaction: discord.Interaction):
    # Show thinking animation while processing
    await thinking_animation.show_thinking(interaction)
    
    try:
        codes = await get_active_gift_codes()
        if not codes:
            await interaction.followup.send("No active gift codes available right now. Check back later! 🎁", ephemeral=False)
            return

        embed = build_codes_embed(codes)

        # Stop the animation and send the embed with the interactive view
        await thinking_animation.stop_thinking(interaction, delete_message=True)
        view = GiftCodeView(codes)
        sent = await interaction.followup.send(
            content=f"{interaction.user.display_name} requested gift codes",
            embed=embed,
            view=view,
            wait=True
        )

        # Save reference to the sent message so the view can edit it later
        try:
            view.message = sent
        except Exception:
            logger.debug("Could not attach message reference to GiftCodeView")
    except Exception as e:
        logger.error(f"Error in giftcode command: {e}")
        await thinking_animation.stop_thinking(interaction, delete_message=True)
        error_embed = discord.Embed(
            title="❌ Error Fetching Gift Codes",
            description="Sorry, I couldn't fetch the gift codes right now. Please try again later.",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="refresh", description="Clears cached alliance data and reloads from Google Sheets.")
@app_commands.default_permissions(administrator=True)  # Only server administrators can use this
async def refresh(interaction: discord.Interaction):
    """Clear the Google Sheets cache to fetch fresh data on next request"""
    # Defer the reply since we're doing an operation
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Reset the cache in our sheets manager
        manager.sheets_manager.reset_cache()
        
        # Send success message
        await interaction.followup.send(
            "♻️ Cache cleared — next request will fetch live data from Google Sheets.",
            ephemeral=True
        )
        logger.info(f"Alliance data cache cleared by {interaction.user.name} ({interaction.user.id})")
        
    except Exception as e:
        # Handle any errors
        error_msg = f"❌ Failed to clear cache: {str(e)}"
        await interaction.followup.send(error_msg, ephemeral=True)
        logger.error(f"Cache clear failed: {e}", exc_info=True)
async def time_autocomplete(interaction: discord.Interaction, current: str):
    """Provide contextual autocomplete suggestions for the time parameter."""
    choices: list[app_commands.Choice] = []
    q = (current or "").strip().lower()

    # common helpful templates - expanded with the requested formats
    templates = [
        # SIMPLE TIMES
        ("5 minutes", "Relative: 5 minutes from now"),
        ("2 hours", "Relative: 2 hours from now"),
        ("1 day", "Relative: 1 day from now"),
        ("today at 8:50 pm", "Today at 8:50 PM"),
        ("today at 20:30", "Today at 20:30 (24h)"),
        ("tomorrow 3pm IST", "Tomorrow at 3:00 PM (IST)"),
        ("tomorrow at 15:30 UTC", "Tomorrow at 15:30 (UTC)"),
        ("at 18:30", "Today at 18:30 (24h)"),
        ("2025-11-05 18:00", "Exact date/time (YYYY-MM-DD HH:MM)"),
        ("next monday 10am", "Next Monday at 10:00 AM"),

        # RECURRING
        ("daily at 9am IST", "Recurring: daily at 9:00 AM (IST)"),
        ("daily at 21:30", "Recurring: daily at 21:30"),
        ("every 2 days at 8pm", "Recurring: every 2 days at 8:00 PM"),
        ("alternate days at 10am", "Recurring: alternate days at 10:00 AM"),
        ("weekly at 15:30", "Recurring: weekly at 15:30"),
        ("every week at 9am EST", "Recurring: weekly at 9:00 AM (EST)"),
    ]

    # If they start with a number suggest relative times
    if q and q[0].isdigit():
        try:
            num = int(''.join(ch for ch in q.split()[0] if ch.isdigit()))
            choices.append(app_commands.Choice(name=f"in {num}m — in {num} minutes", value=f"in {num}m"))
            choices.append(app_commands.Choice(name=f"in {num}h — in {num} hours", value=f"in {num}h"))
        except Exception:
            pass

    # quick starts
    if q.startswith("t"):
        choices.append(app_commands.Choice(name="today 6pm — Today at 6:00 PM", value="today 6pm"))
        choices.append(app_commands.Choice(name="tomorrow 9am — Tomorrow at 9:00 AM", value="tomorrow 9am"))

    # date-like heuristics
    if q and any(c.isdigit() for c in q) and ('-' in q or '/' in q or ':' in q):
        choices.append(app_commands.Choice(name="2025-11-05 18:00 — Exact date/time", value="2025-11-05 18:00"))

    # If the user typed something that can be parsed, show a resolved preview
    try:
        if q:
            parsed_dt, info = TimeParser.parse_time_string(current)
            if parsed_dt:
                # Determine user's preferred timezone for display
                user_tz = get_user_timezone(interaction.user.id) or TimeParser.get_local_timezone()
                local_dt = TimeParser.utc_to_local(parsed_dt, user_tz)
                preview = local_dt.strftime('%b %d, %I:%M %p')
                # prepend to choices so it's prominent
                choices.insert(0, app_commands.Choice(name=f"{current} → {preview} ({user_tz.upper()})", value=current))
    except Exception:
        # parsing failure should not break autocomplete
        pass

    for val, desc in templates:
        if len(choices) >= 25:
            break
        if q == "" or val.startswith(q) or q in val or q in desc.lower():
            choices.append(app_commands.Choice(name=f"{val} — {desc}", value=val))

    return choices[:25]

@bot.tree.command(name="reminder", description="Set a reminder with time and message")
@app_commands.describe(
    time="When to remind you (e.g., '5 minutes', 'tomorrow 3pm IST', 'daily at 9am')",
    message="What to remind you about",
    channel="Channel to send reminder in (required)"
)
@app_commands.autocomplete(time=time_autocomplete)
async def reminder(interaction: discord.Interaction, time: str, message: str, channel: discord.TextChannel):
    await interaction.response.defer(thinking=True)

    try:
        target_channel = channel

        # Display the exact command as entered
        command_text = f"/reminder time: {time} message: {message}"
        if channel:
            command_text += f" channel: {channel.mention}"
        await interaction.followup.send(command_text)

        # Create the reminder
        success = await reminder_system.create_reminder(interaction, time, message, target_channel)

        if not success:
            # Error message already sent by create_reminder
            pass

    except Exception as e:
        logger.error(f"Error in remind command: {str(e)}")
        try:
            await interaction.followup.send("❌ **Error**\n\nSorry, there was an error setting your reminder. Please try again.", ephemeral=True)
        except:
            logger.error("Failed to send error message")





# /show_timezone command removed per user request. Previously showed user's configured timezone.


@bot.tree.command(name="reminderdashboard", description="Open interactive reminder dashboard (list/delete/set timezone)")
async def reminderdashboard(interaction: discord.Interaction):
    """Interactive dashboard that consolidates list/delete/set-timezone into a single UI."""
    # Build a view with buttons that open selects/modals as needed
    class ReminderDeleteSelect(discord.ui.Select):
        def __init__(self, reminders_list: list):
            options = []
            # Build options as numeric index (02-style) with description showing ID and short message
            for idx, r in enumerate(reminders_list):
                rid = str(r.get('id'))
                msg = r.get('message', '')[:60].replace('\n', ' ')
                label = f"{idx+1:02d}"  # shows as 01,02,03...
                desc = (f"ID #{rid} — {msg}") if msg else f"ID #{rid}"
                options.append(discord.SelectOption(label=label, description=desc, value=rid))

            super().__init__(placeholder="Select a reminder to delete", min_values=1, max_values=1, options=options)

        async def callback(self, select_interaction: discord.Interaction):
            try:
                chosen = int(self.values[0])
                # Reuse existing helper to delete and respond
                await reminder_system.delete_user_reminder(select_interaction, chosen)
            except Exception as e:
                logger.error(f"Failed to delete reminder via dashboard: {e}")
                try:
                    await select_interaction.response.send_message("Failed to delete reminder. Try again.", ephemeral=True)
                except Exception:
                    pass

    class TimezoneSelect(discord.ui.Select):
        def __init__(self):
            options = []
            # Add an explicit clear option
            options.append(discord.SelectOption(label="Clear timezone (use default)", value="__clear__"))
            # Map timezone abbreviations to friendly country/region names for display
            tz_countries = {
                'utc': 'Universal',
                'gmt': 'UK/UTC',
                'est': 'United States (Eastern)',
                'cst': 'United States (Central)',
                'mst': 'United States (Mountain)',
                'pst': 'United States (Pacific)',
                'ist': 'India',
                'cet': 'Central Europe',
                'cest': 'Central Europe',
                'jst': 'Japan',
                'aest': 'Australia',
                'bst': 'United Kingdom'
            }
            for tz in sorted(TimeParser.TIMEZONE_MAP.keys()):
                country = tz_countries.get(tz.lower(), '')
                desc = country if country else TimeParser.TIMEZONE_MAP.get(tz.lower(), '')
                # Use TZ abbreviation as the label and country as the description to help selection
                options.append(discord.SelectOption(label=tz.upper(), description=desc, value=tz))
            super().__init__(placeholder="Select timezone (or clear)", min_values=1, max_values=1, options=options)

        async def callback(self, select_interaction: discord.Interaction):
            try:
                val = self.values[0]
                user_id = select_interaction.user.id
                if val == "__clear__":
                    # Clear by setting empty string (get_user_timezone treats falsy as not set)
                    set_user_timezone(user_id, '')
                    await select_interaction.response.send_message("✅ Your timezone has been cleared.", ephemeral=True)
                    return

                # Set timezone
                if val.lower() not in TimeParser.TIMEZONE_MAP:
                    await select_interaction.response.send_message("Unknown timezone selection.", ephemeral=True)
                    return
                set_user_timezone(user_id, val.lower())
                await select_interaction.response.send_message(f"✅ Timezone set to {val.upper()}", ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to set timezone via dashboard: {e}")
                try:
                    await select_interaction.response.send_message("Failed to set timezone. Try again.", ephemeral=True)
                except Exception:
                    pass

    class ReminderDashboardView(discord.ui.View):
        def __init__(self):
            # Keep the view alive for the lifetime of the bot process so buttons remain clickable
            # until the bot restarts. If you want the view to be ephemeral or expire sooner,
            # change this value.
            super().__init__(timeout=None)

        @discord.ui.button(label="List", style=discord.ButtonStyle.primary, custom_id="rd_list", emoji="📝")
        async def list_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            try:
                # Directly call the listing helper which will send the reminders embed.
                # Avoid sending an extra header first because list_user_reminders uses
                # interaction.response.send_message and that will fail if a response
                # has already been sent for this interaction.
                await reminder_system.list_user_reminders(button_interaction)
            except Exception as e:
                logger.error(f"Failed to list reminders via dashboard: {e}")
                try:
                    await button_interaction.response.send_message("Failed to fetch your reminders.", ephemeral=True)
                except Exception:
                    pass

        @discord.ui.button(label="Delete", style=discord.ButtonStyle.secondary, custom_id="rd_delete", emoji="🗑️")
        async def delete_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            try:
                # Fetch user's active reminders
                user_id = str(button_interaction.user.id)
                reminders = reminder_system.storage.get_user_reminders(user_id, limit=25)
                if not reminders:
                    await button_interaction.response.send_message("You don't have any active reminders to delete.", ephemeral=True)
                    return

                # Create a select with reminders and send ephemeral message
                select = ReminderDeleteSelect(reminders)
                v = discord.ui.View()
                v.add_item(select)
                header = discord.Embed(title="🗑️ Delete Reminder", description="Choose the reminder number (left) then confirm.", color=0x2f3136)
                await button_interaction.response.send_message(embed=header, view=v, ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to open delete reminder select: {e}")
                try:
                    await button_interaction.response.send_message("Failed to open reminder deletion UI.", ephemeral=True)
                except Exception:
                    pass

        @discord.ui.button(label="Timezone", style=discord.ButtonStyle.success, custom_id="rd_tz", emoji="🌐")
        async def tz_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            try:
                select = TimezoneSelect()
                v = discord.ui.View()
                v.add_item(select)
                embed = discord.Embed(title="🌐 Select Timezone", description="Choose how times are displayed for your reminders.", color=0x2f3136)
                await button_interaction.response.send_message(embed=embed, view=v, ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to open timezone select: {e}")
                try:
                    await button_interaction.response.send_message("Failed to open timezone selection.", ephemeral=True)
                except Exception:
                    pass

    view = ReminderDashboardView()

    # Build preview items from storage for the renderer
    try:
        raw = reminder_system.storage.get_user_reminders(str(interaction.user.id), limit=8)
    except Exception:
        raw = []

    preview_items = []
    user_tz = get_user_timezone(interaction.user.id) or TimeParser.get_local_timezone()
    for r in raw:
        try:
            rid = r.get('id')
            msg = r.get('message', '')
            rt = r.get('reminder_time')
            # reminder_time stored as naive UTC in DB; convert to display
            if isinstance(rt, str):
                try:
                    from datetime import datetime
                    rt_dt = datetime.fromisoformat(rt)
                except Exception:
                    rt_dt = None
            else:
                rt_dt = rt

            tdisp = ''
            if rt_dt:
                try:
                    local_dt = TimeParser.utc_to_local(rt_dt, user_tz)
                    tdisp = local_dt.strftime('%b %d, %I:%M %p')
                except Exception:
                    tdisp = str(rt_dt)

            preview_items.append({'id': rid, 'message': msg, 'time_display': tdisp})
        except Exception:
            continue

    # Send the original embed-based dashboard (no image) and attach the interactive View
    try:
        embed = discord.Embed(
            title="🎛️ Reminder Dashboard",
            description="Manage your reminders quickly using the buttons below.",
            color=0x2ecc71,
        )
        embed.set_thumbnail(url="https://i.postimg.cc/Fzq03CJf/a463d7c7-7fc7-47fc-b24d-1324383ee2ff-removebg-preview.png")
        # Describe each quick action with a one-line hint
        embed.add_field(
            name="Quick Actions",
            value=(
                "• `List` — Show all your active reminders\n"
                "• `Delete` — Remove a selected reminder\n"
                "• `Timezone` — Set or clear your preferred timezone for display"
            ),
            inline=False,
        )
        embed.add_field(name="Tip", value="Select a reminder under Delete to remove it. Timezone selection changes how times are shown.", inline=False)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        logger.error(f"Failed to send dashboard embed: {e}")
        try:
            await interaction.response.send_message('Open your Reminder Dashboard', view=view, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send('Failed to open reminder dashboard.', ephemeral=True)
            except Exception:
                pass


# /giftchannel command removed per user request. Previously allowed setting gift code posting channel.


# /list_gift_channel command removed per user request. Previously showed configured gift code channel.


# Note: /set_feedback_channel and /unset_feedback_channel removed per user request.


# /giftcode_check command removed per user request. Previously forced a giftcode check and posting.


@bot.tree.command(name="playerinfo", description="Fetch player info by WOS player id")
@app_commands.describe(player_id="Player id to look up")
async def playerinfo(interaction: discord.Interaction, player_id: str):
    await thinking_animation.show_thinking(interaction)
    try:
        # Normalize player id string
        pid = str(player_id).strip()
        info = await fetch_player_info(pid)
        await thinking_animation.stop_thinking(interaction, delete_message=True)
        if not info:
            await interaction.followup.send(f"No public info found for player id {pid}.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Player • {info.get('name') or pid}", color=0x3498db)
        embed.add_field(name="ID", value=str(info.get("id") or pid), inline=True)
        if info.get("level"):
            embed.add_field(name="Level", value=str(info.get("level")), inline=True)
        if info.get("power"):
            embed.add_field(name="Power", value=str(info.get("power")), inline=True)
        if info.get("alliance"):
            embed.add_field(name="Alliance", value=str(info.get("alliance")), inline=True)
        if info.get("source"):
            embed.set_footer(text=f"Source: {info.get('source')}")

        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"playerinfo failed: {e}")
        try:
            await thinking_animation.stop_thinking(interaction, delete_message=True)
        except Exception:
            pass
        await interaction.followup.send("Failed to fetch player info. Check logs.", ephemeral=True)


@bot.tree.command(name="giftcodesettings", description="Open interactive gift code settings dashboard for this server")
@app_commands.default_permissions(administrator=True)
async def giftcodesettings(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        if not interaction.guild:
            await interaction.followup.send("This command must be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        # NOTE: ConfirmClearView removed — clearing sent codes handled elsewhere or disabled from dashboard

        class GiftCodeSettingsView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.button(label="Channel", style=discord.ButtonStyle.primary, custom_id="gcs_channel", emoji="📣")
            async def channel_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    channel_id = giftcode_poster.poster.get_channel(guild_id)
                    if not channel_id:
                        await button_interaction.response.send_message("No gift code channel configured for this server.", ephemeral=True)
                        return
                    ch = interaction.guild.get_channel(channel_id)
                    if not ch:
                        await button_interaction.response.send_message(f"Configured channel (ID: {channel_id}) not found or inaccessible.", ephemeral=True)
                        return
                    await button_interaction.response.send_message(f"Current gift code channel is {ch.mention}", ephemeral=True)
                except Exception as e:
                    logger.error(f"Error showing gift channel via dashboard: {e}")
                    try:
                        await button_interaction.response.send_message("Failed to retrieve gift channel.", ephemeral=True)
                    except Exception:
                        pass

            @discord.ui.button(label="Auto send", style=discord.ButtonStyle.success, custom_id="gcs_set", emoji="✅")
            async def set_here_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    if not button_interaction.user.guild_permissions.administrator:
                        await button_interaction.response.send_message("Only server administrators can set the gift channel.", ephemeral=True)
                        return
                    # Set to the channel where the command was invoked
                    channel = interaction.channel
                    if not isinstance(channel, discord.TextChannel):
                        await button_interaction.response.send_message("This command must be used in a text channel.", ephemeral=True)
                        return
                    giftcode_poster.poster.set_channel(guild_id, channel.id)
                    await button_interaction.response.send_message(f"✅ Gift code channel set to {channel.mention}", ephemeral=True)
                except Exception as e:
                    logger.error(f"Failed to set gift channel via dashboard: {e}")
                    try:
                        await button_interaction.response.send_message("Failed to set gift channel.", ephemeral=True)
                    except Exception:
                        pass

            @discord.ui.button(label="Auto unset", style=discord.ButtonStyle.secondary, custom_id="gcs_unset", emoji="❌")
            async def unset_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    if not button_interaction.user.guild_permissions.administrator:
                        await button_interaction.response.send_message("Only server administrators can unset the gift channel.", ephemeral=True)
                        return
                    giftcode_poster.poster.unset_channel(guild_id)
                    await button_interaction.response.send_message("✅ Gift code posting disabled for this server.", ephemeral=True)
                except Exception as e:
                    logger.error(f"Failed to unset gift channel via dashboard: {e}")
                    try:
                        await button_interaction.response.send_message("Failed to disable gift channel.", ephemeral=True)
                    except Exception:
                        pass

            # "Sent Codes" and "Clear Sent" buttons removed per request

            @discord.ui.button(label="Force Check", style=discord.ButtonStyle.secondary, custom_id="gcs_check", emoji="🔁")
            async def force_check_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    if not button_interaction.user.guild_permissions.administrator:
                        await button_interaction.response.send_message("Only server administrators can run a force check.", ephemeral=True)
                        return
                    await button_interaction.response.defer(ephemeral=True)
                    result = await giftcode_poster.run_now_and_report(bot)
                    posted = result.get('posted', 0)
                    errors = result.get('errors', 0)
                    await button_interaction.followup.send(f"Giftcode check completed. Posted {posted} new codes across configured servers. Errors: {errors}", ephemeral=True)
                except Exception as e:
                    logger.error(f"Failed to run force check via dashboard: {e}")
                    try:
                        await button_interaction.response.send_message("Failed to run force check.", ephemeral=True)
                    except Exception:
                        pass

        view = GiftCodeSettingsView()

        header = discord.Embed(title="🎟️ Gift Code Settings", description="Manage this server's automatic gift code poster and recorded codes.", color=0xffd700)
        # Show current channel if configured
        ch_id = giftcode_poster.poster.get_channel(guild_id)
        if ch_id:
            ch_obj = interaction.guild.get_channel(ch_id)
            header.add_field(name="Configured Channel", value=(ch_obj.mention if ch_obj else f"ID: {ch_id} (not found)"), inline=False)
        else:
            header.add_field(name="Configured Channel", value="Not configured", inline=False)

        await interaction.followup.send(embed=header, view=view)

    except Exception as e:
        logger.error(f"Error in giftcodesettings command: {e}")
        try:
            await interaction.followup.send("❌ Error opening gift code settings.", ephemeral=True)
        except Exception:
            pass

# NOTE: `/delete_reminder` and `/listreminder` commands removed — functionality moved into `/reminderdashboard` UI.




@bot.tree.command(name="imagine", description="Generate AI Images (Pollinations compatibility)")
@app_commands.describe(
    prompt="Prompt of the image you want to generate",
    width="Width of the image (optional)",
    height="Height of the image (optional)",
    model="Model to use (optional)",
    enhance="Enable prompt enhancement (ignored)",
    safe="Safe for work (ignored)",
    cached="Use default seed / caching (ignored)",
    nologo="Remove logo (ignored)",
    private="Send result as ephemeral to only you"
)
@app_commands.choices(
    model=[
        app_commands.Choice(name="flux", value="flux"),
        app_commands.Choice(name="Turbo", value="turbo"),
        app_commands.Choice(name="gptimage", value="gptimage"),
        app_commands.Choice(name="kontext", value="kontext"),
    app_commands.Choice(name="stable-diffusion — UNDER MAINTAINANCE", value="stable-diffusion"),
    ],
)
async def imagine(
    interaction: discord.Interaction,
    prompt: str,
    width: int = None,
    height: int = None,
    model: app_commands.Choice[str] = None,
    enhance: bool = False,
    safe: bool = True,
    cached: bool = False,
    nologo: bool = False,
    private: bool = False,
):
    """Compatibility wrapper for Pollinations' /pollinate command.

    NOTE: This implementation intentionally keeps the backend call simple and
    delegates to the existing `make_image_request(prompt)` in `api_manager.py`.
    Many Pollinations-specific flags are accepted for compatibility but are
    currently ignored by the underlying generator. If you want full feature
    parity (model selection, width/height, caching, etc.) we can extend
    `make_image_request` next.
    """
    # Show thinking animation while processing
    await thinking_animation.show_thinking(interaction)

    try:
    # Note: thinking_animation.show_thinking has already deferred the interaction.
    # Avoid deferring twice which raises "already responded".

        # Basic validation (non-blocking). Only allow reasonable sizes if provided.
        if width is not None and (width <= 0 or width > 2048):
            raise ValueError("Width must be a positive integer <= 2048")
        if height is not None and (height <= 0 or height > 2048):
            raise ValueError("Height must be a positive integer <= 2048")

        # Resolve model choice value
        model_val = (model.value if hasattr(model, 'value') else model)

        # Determine available backends
        has_hf = any(k.startswith('HUGGINGFACE_API_TOKEN') for k in os.environ.keys())
        has_openai = bool(os.getenv('OPENAI_API_KEY'))

        # Generate a seed for deterministic-looking results and measure processing time
        seed = random.randint(0, 2**31 - 1)
        start_time = time.time()

        # Auto-fallback: if no HF or OpenAI keys are configured, use Pollinations public endpoint
        if not has_hf and not has_openai:
            image_data = await fetch_pollinations_image(
                prompt,
                width=width,
                height=height,
                model_name=(model.value if hasattr(model, 'value') else model),
                seed=seed,
            )
            processing_time = time.time() - start_time
            view = PollinateButtonView()
        else:
            # Branch: if user selected stable-diffusion, use Hugging Face backend
            if model_val == 'stable-diffusion':
                # Use environment HUGGINGFACE_MODEL unless a full model string provided
                hf_model = os.getenv('HUGGINGFACE_MODEL', 'stabilityai/stable-diffusion-xl-base-1.0')
                image_data = await make_image_request(prompt, width=width, height=height, model=hf_model)
                processing_time = time.time() - start_time
                # For HF-generated images, don't provide the Edit button view
                view = PollinateNoEditView()
            else:
                # Use Pollinations public API for other models
                image_data = await fetch_pollinations_image(
                    prompt,
                    width=width,
                    height=height,
                    model_name=model_val,
                    seed=seed,
                )
                processing_time = time.time() - start_time

        # Build pollinations URL for embedding/bookmarking (for non-HF models)
        base = "https://image.pollinations.ai/prompt/"
        encoded = quote(prompt, safe='')
        pollinate_url = base + encoded
        params = []
        if width:
            params.append(f"width={int(width)}")
        if height:
            params.append(f"height={int(height)}")
        if model_val and model_val != 'stable-diffusion':
            params.append(f"model={quote(model_val, safe='')}")
        if seed is not None:
            params.append(f"seed={int(seed)}")
        if params:
            pollinate_url = pollinate_url + "?" + "&".join(params)

        # Create a file from the image data
        from io import BytesIO
        image_file = discord.File(BytesIO(image_data), filename="pollinated_image.png")

        # Build a small embed mirroring Pollinations style and include metadata fields
        success_embed = discord.Embed(
            title="🪐 Image",
            description=f"",
            color=0x00FF7F,
            url=pollinate_url,
            timestamp=datetime.utcnow(),
        )
        # Author line similar to Pollinations UI
        try:
            avatar_url = interaction.user.display_avatar.url
        except Exception:
            avatar_url = None
        success_embed.set_author(name=f"Generated by {interaction.user.display_name}", icon_url=avatar_url)
        # Add metadata fields
        use_model = (model.value if hasattr(model, 'value') else model) or os.getenv('HUGGINGFACE_MODEL', 'flux')
        is_xl = 'xl' in (use_model or '').lower()
        default_w = 1024 if is_xl else 512
        default_h = 1024 if is_xl else 512
        use_w = int(width) if width else default_w
        use_h = int(height) if height else default_h

        # Layout: Prompt (full width), then a single code-block with details (seed, time, model, dimensions)
        success_embed.add_field(name="Prompt", value=f"```{prompt}```", inline=False)
        details = (
            f"Seed: {seed}\n"
            f"Processing Time: {processing_time:.2f} s\n"
            f"Model: {use_model}\n"
            f"Dimensions: {use_w}x{use_h}"
        )
        success_embed.add_field(name="Details", value=f"```\n{details}\n```", inline=False)
        success_embed.set_footer(text=f"Generated for {interaction.user.display_name}")
        # Ensure embed displays the attached image
        success_embed.set_image(url="attachment://pollinated_image.png")

        # Stop the animation and delete the message so image can "pop over"
        await thinking_animation.stop_thinking(interaction, delete_message=True)

        # Send result (ephemeral or public based on `private`) with interactive buttons
        # For stable-diffusion (HF) we use PollinateNoEditView which omits the Edit button
        if model_val == 'stable-diffusion':
            if private:
                await interaction.followup.send(embed=success_embed, file=image_file, ephemeral=True)
            else:
                await interaction.followup.send(content=f"{interaction.user.mention}", embed=success_embed, file=image_file, view=PollinateNoEditView())
        else:
            if private:
                await interaction.followup.send(embed=success_embed, file=image_file, ephemeral=True)
            else:
                await interaction.followup.send(content=f"{interaction.user.mention}", embed=success_embed, file=image_file, view=PollinateButtonView())

        logger.info("Successfully sent imagine image")

    except Exception as e:
        logger.error(f"Error in imagine command: {str(e)}")
        error_embed = discord.Embed(
            title="❌ Image Generation Failed",
            description="Sorry, I couldn't generate your image right now. Please try again later or check your prompt.",
            color=0xff0000,
        )
        try:
            if thinking_animation.animation_message:
                await thinking_animation.animation_message.edit(embed=error_embed)
            else:
                await interaction.followup.send(embed=error_embed)
        except Exception as edit_error:
            logger.error(f"Failed to send imagine error message: {edit_error}")
            try:
                await interaction.followup.send(embed=error_embed)
            except Exception:
                logger.error("Failed final imagine error followup")

@bot.tree.command(name="serverstats", description="Show detailed server statistics")
async def serverstats(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    # Show thinking animation while processing
    await thinking_animation.show_thinking(interaction)

    try:
        embed = discord.Embed(title=f"📊 {guild.name} Server Stats", color=0x3498db)
        embed.add_field(name="👥 Members", value=guild.member_count, inline=True)
        embed.add_field(name="📅 Created", value=guild.created_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        text_channels = len([c for c in guild.channels if isinstance(c, discord.TextChannel)])
        voice_channels = len([c for c in guild.channels if isinstance(c, discord.VoiceChannel)])
        categories = len([c for c in guild.channels if isinstance(c, discord.CategoryChannel)])
        embed.add_field(name="💬 Text Channels", value=text_channels, inline=True)
        embed.add_field(name="🔊 Voice Channels", value=voice_channels, inline=True)
        embed.add_field(name="📁 Categories", value=categories, inline=True)
        embed.add_field(name="🎭 Roles", value=len(guild.roles), inline=True)
        # Count bots by checking for "Bot" role first, fallback to bot flag
        bot_role = discord.utils.get(guild.roles, name="Bot") or discord.utils.get(guild.roles, name="bot")
        if bot_role:
            bots = len(bot_role.members)
        else:
            bots = len([m for m in guild.members if m.bot])
        humans = guild.member_count - bots
        embed.add_field(name="👤 Humans", value=humans, inline=True)
        embed.add_field(name="🤖 Bots", value=bots, inline=True)
        online = len([m for m in guild.members if m.status in [discord.Status.online, discord.Status.idle, discord.Status.dnd]])
        embed.add_field(name="🟢 Online", value=online, inline=True)
        embed.add_field(name="⚫ Offline", value=guild.member_count - online, inline=True)
        embed.add_field(name="🚫 Content Filter", value=str(guild.explicit_content_filter).title(), inline=True)
        if guild.premium_tier > 0:
            embed.add_field(name="🚀 Boost Level", value=guild.premium_tier, inline=True)
            embed.add_field(name="💎 Boosts", value=guild.premium_subscription_count, inline=True)
        # Find most active user in "💬┃main-chat" channel (excluding bots)
        chats_channel = discord.utils.get(guild.channels, name="💬┃main-chat")
        if chats_channel and isinstance(chats_channel, discord.TextChannel):
            logger.info(f"Channel found: {chats_channel.name} (ID: {chats_channel.id})")
            try:
                message_counts = {}
                message_count = 0
                async for message in chats_channel.history(limit=1000):
                    if not message.author.bot:  # Exclude bot messages
                        author_id = message.author.id
                        message_counts[author_id] = message_counts.get(author_id, 0) + 1
                    message_count += 1
                logger.info(f"Fetched {message_count} total messages from channel")
                if message_counts:
                    top_user_id, count = max(message_counts.items(), key=lambda x: x[1])
                    top_user = guild.get_member(top_user_id)
                    if top_user and not top_user.bot:
                        logger.info(f"Top user: {top_user.display_name} with {count} messages")
                        embed.add_field(name="Most Active User", value=f"{top_user.display_name} ({count} messages)", inline=True)
                    else:
                        logger.warning("Top user is a bot or not found in guild")
                else:
                    logger.warning("No non-bot messages found in channel history")
            except Exception as e:
                logger.error(f"Error fetching message history from {chats_channel.name}: {e}")
        else:
            logger.warning("💬┃main-chat channel not found or not a text channel")

        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        embed.set_footer(text=f"Server ID: {guild.id}")

        # Stop the animation before editing the message
        await thinking_animation.stop_thinking(interaction, delete_message=False)

        # Edit the animation message with the result
        if thinking_animation.animation_message:
            try:
                await thinking_animation.animation_message.edit(embed=embed)
                logger.info("Successfully edited animation message with serverstats results")
            except Exception as edit_error:
                logger.error(f"Failed to edit animation message with serverstats results: {edit_error}")
                # Fallback to followup send
                await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in serverstats command: {e}")
        error_embed = discord.Embed(
            title="❌ Error Fetching Server Statistics",
            description="I encountered an error while fetching server statistics. Please try again.",
            color=0xff0000
        )
        try:
            # Try to edit animation message with error
            if thinking_animation.animation_message:
                await thinking_animation.animation_message.edit(embed=error_embed)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
        except Exception as edit_error:
            logger.error(f"Failed to send error message: {edit_error}")
            # Final fallback
            try:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
            except Exception as final_error:
                logger.error(f"Failed to send final error message: {final_error}")

@bot.tree.command(name="mostactive", description="Show the top 3 most active users and activity graph based on messages in the current month")
async def mostactive(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    # Show thinking animation while processing
    await thinking_animation.show_thinking(interaction)

    # Use the channel where the command was invoked
    chats_channel = interaction.channel

    try:
        # Get start of current month
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)

        message_counts = {}
        date_counts = {}
        async for message in chats_channel.history(limit=10000, after=start_of_month):
            if not message.author.bot:  # Exclude bot messages
                author_id = message.author.id
                message_counts[author_id] = message_counts.get(author_id, 0) + 1
                date = message.created_at.date()
                date_counts[date] = date_counts.get(date, 0) + 1

        if not message_counts:
            await interaction.followup.send(f"No messages found in {now.strftime('%B %Y')}.", ephemeral=True)
            return

        # Get top 3 users sorted by message count descending
        sorted_users = sorted(message_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        top_users = []
        for i, (user_id, count) in enumerate(sorted_users, 1):
            user = guild.get_member(user_id)
            if user and not user.bot:
                top_users.append((user, count, i))

        if not top_users:
            await interaction.followup.send(f"No valid users found in {now.strftime('%B %Y')}.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏆 Top Active Users",
            description=f"Based on messages in {now.strftime('%B %Y')} in {chats_channel.mention}",
            color=0x3498db
        )

        medals = ["🥇", "🥈", "🥉"]
        for user, count, rank in top_users:
            medal = medals[rank - 1] if rank <= 3 else "🏅"
            embed.add_field(
                name=f"{medal} {rank}st Place",
                value=f"{user.display_name} ({count} messages)",
                inline=False
            )

        # If fewer than 3, note it
        if len(top_users) < 3:
            embed.add_field(
                name="ℹ️ Note",
                value=f"Only {len(top_users)} active users found in {now.strftime('%B %Y')}.",
                inline=False
            )

        embed.set_footer(text=f"Server: {guild.name}")

        # Stop the animation before editing the message
        await thinking_animation.stop_thinking(interaction, delete_message=False)

        # Edit the animation message with the result
        if thinking_animation.animation_message:
            try:
                await thinking_animation.animation_message.edit(embed=embed)
                logger.info("Successfully edited animation message with mostactive results")
            except Exception as edit_error:
                logger.error(f"Failed to edit animation message with mostactive results: {edit_error}")
                # Fallback to followup send
                await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(embed=embed)

        # Send the activity graph if data is available
        if date_counts:
            dates = sorted(date_counts.keys())
            counts = [date_counts[d] for d in dates]
            total_messages = sum(counts)
            average = total_messages / len(dates) if dates else 0
            start_date = dates[0].strftime('%Y-%m-%d') if dates else 'N/A'
            end_date = dates[-1].strftime('%Y-%m-%d') if dates else 'N/A'

            plt.figure(figsize=(12,6))
            bars = plt.bar(dates, counts, color='skyblue', edgecolor='black', alpha=0.7)
            # Highlight bars above average in orange
            for bar, count in zip(bars, counts):
                if count > average:
                    bar.set_color('orange')
            plt.axhline(y=average, color='red', linestyle='--', linewidth=2, label=f'Average: {average:.1f} msgs/day')
            plt.grid(True, alpha=0.3)
            plt.title(f'Daily Message Activity ({now.strftime("%B %Y")}: {total_messages} msgs from {start_date} to {end_date})', fontsize=14, fontweight='bold')
            plt.xlabel('Date', fontsize=12)
            plt.ylabel('Number of Messages', fontsize=12)
            plt.legend()
            # Format x-axis dates
            plt.gca().xaxis.set_major_formatter(DateFormatter('%Y-%m-%d'))
            plt.xticks(rotation=45, ha='right')
            plt.tight_layout()
            # Add top user annotation
            if top_users:
                top_user, top_count, _ = top_users[0]
                plt.text(0.02, 0.98, f'Top User: {top_user.display_name} ({top_count} msgs)', transform=plt.gca().transAxes,
                         fontsize=10, verticalalignment='top', bbox=dict(boxstyle='round,pad=0.3', facecolor='wheat', alpha=0.8))
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100)
            buf.seek(0)
            file = discord.File(buf, 'activity_graph.png')
            plt.close()
            await interaction.followup.send(file=file)

    except Exception as e:
        logger.error(f"Error in mostactive command: {e}")
        error_embed = discord.Embed(
            title="❌ Error Fetching Message History",
            description="I encountered an error while fetching message history. Please try again.",
            color=0xff0000
        )
        try:
            # Try to edit animation message with error
            if thinking_animation.animation_message:
                await thinking_animation.animation_message.edit(embed=error_embed)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
        except Exception as edit_error:
            logger.error(f"Failed to send error message: {edit_error}")
            # Final fallback
            try:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
            except Exception as final_error:
                logger.error(f"Failed to send final error message: {final_error}")

@bot.tree.command(name="help", description="Show information about available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Bot Commands",
        description=(
            "**🎮 Games & Fun**\n"
            "• **/dice** - Roll a six-sided dice (slash + message trigger)\n"
            "• **/dicebattle [opponent]** - Challenge another player to a dice battle (interactive roll buttons)\n"
            "• **/imagine [prompt]** - Generate an AI image from a prompt\n"
            "• **/ask [question]** - Ask the bot a question or get help\n\n"
            "**🎁 Gift Codes & Server Tools**\n"
            "• **/giftcode** - Show active Whiteout Survival gift codes\n"
            "• **/giftcodesettings** - Open the server gift code settings dashboard (admin)\n"
            "• **/refresh** - Refresh cached alliance/gift code data from Sheets\n\n"
            "**⏰ Reminders & Time**\n"
            "• **/reminder [time] [message] [channel]** - Create a timed reminder\n"
            "• **/reminderdashboard** - Open interactive reminder dashboard (list/delete/timezone)\n\n"
            "**👥 Player & Server**\n"
            "• **/playerinfo [player_id]** - Fetch Whiteout Survival player info-UNDER developement\n"
            "• **/serverstats** - View server statistics and charts\n"
            "• **/mostactive** - Show top active users and activity graph\n\n"
            "**🧭 Profile & Events**\n"
            "• **/add_trait [trait]** - Add a personality trait to your profile\n"
            "• **/event [name]** - Get event details (autocomplete supported)\n\n"
            "**❓ Help**\n"
            "• **/help** - Show this command list"
        ),
        color=0x1abc9c,
    )
    embed.set_thumbnail(url="https://i.postimg.cc/Fzq03CJf/a463d7c7-7fc7-47fc-b24d-1324383ee2ff-removebg-preview.png")
    embed.set_footer(text="Type a command to get started!")
    # Add a feedback button under the help embed
    class FeedbackModal(discord.ui.Modal, title="Your Feedback"):
        feedback = discord.ui.TextInput(label="Your feedback", style=discord.TextStyle.long, placeholder="Share your feedback or a bug report...", required=True, max_length=2000)

        async def on_submit(self, modal_interaction: discord.Interaction):
                # Try to send feedback to configured feedback channel and always also attempt to DM the bot owner
                try:
                    feedback_text = self.feedback.value
                    posted_channel = False
                    posted_owner = False

                    # Prefer persisted feedback channel or environment variable
                    feedback_channel_id = get_feedback_channel_id()
                    if feedback_channel_id:
                        try:
                            ch = modal_interaction.client.get_channel(int(feedback_channel_id))
                            if ch:
                                await ch.send(f"**Feedback from** {modal_interaction.user} (ID: {modal_interaction.user.id}):\n{feedback_text}")
                                posted_channel = True
                        except Exception as e:
                            logger.error(f"Failed to post feedback to channel: {e}")

                    # Always attempt to DM the configured bot owner (if set)
                    owner_id = os.getenv('BOT_OWNER_ID')
                    if owner_id:
                        try:
                            owner = modal_interaction.client.get_user(int(owner_id))
                            if owner is None:
                                try:
                                    owner = await modal_interaction.client.fetch_user(int(owner_id))
                                except Exception as e:
                                    logger.error(f"Failed to fetch owner user object: {e}")

                            if owner:
                                try:
                                    await owner.send(f"**Feedback from** {modal_interaction.user} (ID: {modal_interaction.user.id}):\n{feedback_text}")
                                    posted_owner = True
                                except Exception as e:
                                    logger.error(f"Failed to DM owner with feedback: {e}")
                                    # fallback: if we have a feedback channel, post there as an alert
                                    if feedback_channel_id and not posted_channel:
                                        try:
                                            ch = modal_interaction.client.get_channel(int(feedback_channel_id))
                                            if ch:
                                                await ch.send(f"⚠️ Could not DM configured owner (ID: {owner_id}). Feedback from {modal_interaction.user} (ID: {modal_interaction.user.id}):\n{feedback_text}")
                                                posted_channel = True
                                        except Exception as e2:
                                            logger.error(f"Failed to post fallback notification to feedback channel: {e2}")
                        except Exception as e:
                            logger.error(f"Unexpected error while trying to deliver feedback to owner: {e}")

                    # Persist the feedback to disk for audit/backup
                    try:
                        append_feedback_log(modal_interaction.user, modal_interaction.user.id, feedback_text, posted_channel=posted_channel, posted_owner=posted_owner)
                    except Exception:
                        logger.exception("Failed to append feedback to log file")

                    posted = posted_channel or posted_owner
                    logger.info(f"Received feedback from {modal_interaction.user} (posted_channel={posted_channel}, posted_owner={posted_owner})")
                    try:
                        await modal_interaction.response.send_message("Thanks — your feedback has been submitted.", ephemeral=True)
                    except Exception:
                        logger.debug("Could not send ephemeral confirmation for feedback")
                except Exception as e:
                    logger.error(f"Error handling feedback modal submit: {e}")

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Share Feedback", style=discord.ButtonStyle.primary, custom_id="share_feedback")
        async def share_feedback(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            try:
                await button_interaction.response.send_modal(FeedbackModal())
            except Exception as e:
                logger.error(f"Failed to open feedback modal: {e}")
                try:
                    await button_interaction.response.send_message("Couldn't open feedback form right now.", ephemeral=True)
                except Exception:
                    pass

    view = HelpView()
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)
    except Exception as e:
        logger.error(f"Failed to send help embed: {e}")
        try:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)
        except Exception as e2:
            logger.error(f"Failed to send help embed via followup: {e2}")


# --- Dice battle: a two-player roll with buttons ---
class DiceBattleView(discord.ui.View):
    """View that manages a two-player dice battle. Each player has one Roll button
    that only they can press. After both roll, the view declares a winner and
    disables the buttons."""
    def __init__(self, challenger: discord.Member, opponent: discord.Member, *, timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.challenger = challenger
        self.opponent = opponent
        # store results as {user_id: int or None}
        self.results = {challenger.id: None, opponent.id: None}
        self.message: discord.Message | None = None
        # Customize button labels and styles so each shows the player's name and different colors
        try:
            # short helper to trim long names for the button
            def _short(name: str, limit: int = 18) -> str:
                n = (name or "").strip()
                if len(n) <= limit:
                    return n
                return n[: limit - 1].rstrip() + "…"

            for child in list(self.children):
                cid = getattr(child, 'custom_id', '')
                if cid == 'dicebattle_roll_challenger':
                    child.label = f"Roll\n{_short(self.challenger.display_name)}"
                    child.style = discord.ButtonStyle.primary
                elif cid == 'dicebattle_roll_opponent':
                    child.label = f"Roll\n{_short(self.opponent.display_name)}"
                    # make opponent a different color
                    child.style = discord.ButtonStyle.success
        except Exception:
            # non-fatal: if UI objects aren't ready yet, ignore
            pass

    def build_embed(self) -> discord.Embed:
        """Create an embed showing both players and current results."""
        e = discord.Embed(title=f"🎲 Dice Battle: {self.challenger.display_name} vs {self.opponent.display_name}", color=0x3498db)
        # Challenger as author with avatar
        try:
            e.set_author(name=self.challenger.display_name, icon_url=self.challenger.display_avatar.url)
        except Exception:
            e.set_author(name=self.challenger.display_name)

        # Opponent avatar as thumbnail
        try:
            e.set_thumbnail(url=self.opponent.display_avatar.url)
        except Exception:
            pass

        # Fields for results
        cres = self.results.get(self.challenger.id)
        ores = self.results.get(self.opponent.id)
        e.add_field(name=f"Challenger — {self.challenger.display_name}", value=(str(cres) if cres is not None else "Not rolled"), inline=True)
        e.add_field(name=f"Opponent — {self.opponent.display_name}", value=(str(ores) if ores is not None else "Not rolled"), inline=True)

        if all(v is not None for v in self.results.values()):
            # Both rolled — determine winner
            a = self.results[self.challenger.id]
            b = self.results[self.opponent.id]
            if a > b:
                e.title = f"🏆 {self.challenger.display_name} wins!"
                e.color = 0x2ecc71
                e.description = f"**{self.challenger.display_name}** wins the dice battle with a roll of **{a}** against **{b}**. Congratulations!"
                try:
                    e.set_thumbnail(url=self.challenger.display_avatar.url)
                except Exception:
                    pass
            elif b > a:
                e.title = f"🏆 {self.opponent.display_name} wins!"
                e.color = 0x2ecc71
                e.description = f"**{self.opponent.display_name}** wins the dice battle with a roll of **{b}** against **{a}**. Congratulations!"
                try:
                    e.set_thumbnail(url=self.opponent.display_avatar.url)
                except Exception:
                    pass
            else:
                e.title = f"🤝 It's a tie!"
                e.color = 0xf1c40f
                e.description = f"Both players rolled **{a}** — it's a draw!"

            # Add a result field summarizing both rolls
            e.add_field(name="Result", value=f"{self.challenger.display_name}: **{a}**\n{self.opponent.display_name}: **{b}**", inline=False)

        return e

    async def create_battle_image(self, left_face_url: str = None, right_face_url: str = None) -> discord.File:
        """Create a composite image showing both players' avatars with a crossed-swords
        emblem in the middle and optionally overlay dice-face images for left/right.
        Returns a discord.File ready to send as attachment.
        """
    # Default canvas sizes
        width = 900
        height = 360
        left_size = right_size = 320

        # Helper to fetch binary data for an avatar URL
        async def fetch_bytes(url: str) -> bytes:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
            return None

        # Get avatar URLs (use display_avatar which is HTTP(s) URL)
        left_url = getattr(self.challenger.display_avatar, 'url', None) or getattr(self.challenger.avatar, 'url', None)
        right_url = getattr(self.opponent.display_avatar, 'url', None) or getattr(self.opponent.avatar, 'url', None)

        left_bytes = None
        right_bytes = None
        try:
            left_bytes, right_bytes = await asyncio.gather(fetch_bytes(left_url), fetch_bytes(right_url))
        except Exception:
            # Fallback: try sequentially
            try:
                left_bytes = await fetch_bytes(left_url)
            except Exception:
                left_bytes = None
            try:
                right_bytes = await fetch_bytes(right_url)
            except Exception:
                right_bytes = None

        # Load images (fallback to plain color if fetch failed)
        try:
            if left_bytes:
                left_img = Image.open(io.BytesIO(left_bytes)).convert('RGBA')
            else:
                left_img = Image.new('RGBA', (left_size, left_size), (200, 200, 200))
        except Exception:
            left_img = Image.new('RGBA', (left_size, left_size), (200, 200, 200))

        try:
            if right_bytes:
                right_img = Image.open(io.BytesIO(right_bytes)).convert('RGBA')
            else:
                right_img = Image.new('RGBA', (right_size, right_size), (180, 180, 180))
        except Exception:
            right_img = Image.new('RGBA', (right_size, right_size), (180, 180, 180))

        # Resize avatars to square
        left_img = left_img.resize((left_size, left_size), Image.LANCZOS)
        right_img = right_img.resize((right_size, right_size), Image.LANCZOS)

        # Use the provided remote URLs for assets (Render-friendly)
        default_bg_url = "https://cdn.discordapp.com/attachments/1435569370389807144/1435702034497278142/2208_w026_n002_2422b_p1_2422.jpg?ex=690ced37&is=690b9bb7&hm=04cdb75f595c5babb52fc3210fa548a02d3680e518728a1856429028ad5a3b65"
        default_sword_url = "https://cdn.discordapp.com/attachments/1435569370389807144/1435693707276845096/pngtree-crossed-swords-icon-combat-with-melee-weapons-duel-king-protect-vector-png-image_48129218-removebg-preview_2.png?ex=690ce575&is=690b93f5&hm=b564d747bfadcd5631911ce5e53710b70c7607410145e3c5ecc41a76fa55d5e8"
        default_logo_url = "https://cdn.discordapp.com/attachments/1435569370389807144/1435683133319282890/unnamed_3.png?ex=690cdb9c&is=690b8a1c&hm=e605500d0e061ee4983c68c30b68d3e285b03a88d31605ac65abf2b4df0ae028"

        canvas = Image.new('RGBA', (width, height), (40, 44, 52, 255))

        # Try to fetch and draw the background image (remote)
        try:
            bg_bytes = await fetch_bytes(default_bg_url)
            if bg_bytes:
                bg_img = Image.open(io.BytesIO(bg_bytes)).convert('RGBA')
                bg_img = bg_img.resize((width, height), Image.LANCZOS)
                canvas.paste(bg_img, (0, 0))
        except Exception:
            # ignore background failures
            pass

        draw = ImageDraw.Draw(canvas)

        # Create circular masks and paste avatars with a white ring
        pad_y = (height - left_size) // 2
        def paste_circular(img: Image.Image, x: int, y: int, size: int):
            try:
                mask = Image.new('L', (size, size), 0)
                mdraw = ImageDraw.Draw(mask)
                mdraw.ellipse((0, 0, size, size), fill=255)

                # create a white ring background
                ring = Image.new('RGBA', (size + 12, size + 12), (255, 255, 255, 0))
                rdraw = ImageDraw.Draw(ring)
                rdraw.ellipse((0, 0, size + 12, size + 12), fill=(255, 255, 255, 200))
                canvas.paste(ring, (x - 6, y - 6), ring)

                # paste avatar
                canvas.paste(img, (x, y), mask)
            except Exception:
                try:
                    canvas.paste(img, (x, y), img)
                except Exception:
                    canvas.paste(img, (x, y))

        left_x = 40
        right_x = width - right_size - 40
        paste_circular(left_img, left_x, pad_y, left_size)
        paste_circular(right_img, right_x, pad_y, right_size)

        # Overlay the crossed-swords PNG centered between avatars and place the supplied logo above it
        try:
            sword_bytes = await fetch_bytes(default_sword_url)
            if sword_bytes:
                sword_img = Image.open(io.BytesIO(sword_bytes)).convert('RGBA')
            else:
                sword_img = None
            if sword_img:
                # remove near-black background from sword image (make it transparent)
                try:
                    sdata = sword_img.getdata()
                    new_sdata = []
                    for item in sdata:
                        if len(item) >= 4:
                            r, g, b, a = item
                        else:
                            r, g, b = item
                            a = 255
                        # treat very dark pixels as transparent
                        if r < 30 and g < 30 and b < 30:
                            new_sdata.append((255, 255, 255, 0))
                        else:
                            new_sdata.append((r, g, b, a))
                    sword_img.putdata(new_sdata)
                except Exception:
                    pass

                # scale sword image to fit between avatars
                max_sword_w = 260
                w_ratio = max_sword_w / sword_img.width
                new_w = int(sword_img.width * w_ratio)
                new_h = int(sword_img.height * w_ratio)
                sword_img = sword_img.resize((new_w, new_h), Image.LANCZOS)
                sx = (width - new_w) // 2
                sy = (height - new_h) // 2
                canvas.paste(sword_img, (sx, sy), sword_img)

                # Now overlay provided logo above the sword (remote)
                try:
                    logo_bytes = await fetch_bytes(default_logo_url)
                    if logo_bytes:
                        logo_img = Image.open(io.BytesIO(logo_bytes)).convert('RGBA')
                    else:
                        logo_img = None
                    if logo_img:
                        # scale logo relative to sword (original size/position)
                        logo_w = int(new_w * 0.5)
                        logo_h = int(logo_img.height * (logo_w / logo_img.width))
                        logo_img = logo_img.resize((logo_w, logo_h), Image.LANCZOS)
                        lx = (width - logo_w) // 2
                        ly = sy - int(logo_h * 0.6)
                        canvas.paste(logo_img, (lx, ly), logo_img)
                except Exception:
                    pass
        except Exception:
            # fallback: draw simple crossed lines
            cx = width // 2
            cy = height // 2
            draw.line((cx - 40, cy - 40, cx + 40, cy + 40), fill=(240, 200, 200, 255), width=6)
            draw.line((cx + 40, cy - 40, cx - 40, cy + 40), fill=(240, 200, 200, 255), width=6)

        # Add small name plates under avatars
        try:
            fn = ImageFont.load_default()
            ln_w, ln_h = draw.textsize(self.challenger.display_name, font=fn)
            draw.rectangle([40, pad_y + left_size + 8, 40 + left_size, pad_y + left_size + 8 + ln_h + 6], fill=(0, 0, 0, 140))
            draw.text((40 + (left_size - ln_w) / 2, pad_y + left_size + 10), self.challenger.display_name, font=fn, fill=(255, 255, 255, 255))

            rn_w, rn_h = draw.textsize(self.opponent.display_name, font=fn)
            draw.rectangle([width - right_size - 40, pad_y + right_size + 8, width - 40, pad_y + right_size + 8 + rn_h + 6], fill=(0, 0, 0, 140))
            draw.text((width - right_size - 40 + (right_size - rn_w) / 2, pad_y + right_size + 10), self.opponent.display_name, font=fn, fill=(255, 255, 255, 255))
        except Exception:
            pass

        # Optionally overlay dice faces near avatars
        try:
            face_size = 110
            async def fetch_face(url: str):
                if not url:
                    return None
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            return await resp.read()
                return None

            if left_face_url:
                fb = None
                try:
                    fb = await fetch_face(left_face_url)
                except Exception:
                    fb = None
                if fb:
                    try:
                        fimg = Image.open(io.BytesIO(fb)).convert('RGBA')
                        fimg = fimg.resize((face_size, face_size), Image.LANCZOS)
                        # position: bottom-right corner of left avatar
                        lx = 40 + left_size - face_size // 2
                        ly = pad_y + left_size - face_size // 2
                        canvas.paste(fimg, (lx, ly), fimg)
                    except Exception:
                        pass

            if right_face_url:
                fb = None
                try:
                    fb = await fetch_face(right_face_url)
                except Exception:
                    fb = None
                if fb:
                    try:
                        fimg = Image.open(io.BytesIO(fb)).convert('RGBA')
                        fimg = fimg.resize((face_size, face_size), Image.LANCZOS)
                        # position: bottom-left corner of right avatar
                        rx = width - right_size - 40 + (right_size - face_size // 2)
                        ry = pad_y + right_size - face_size // 2
                        canvas.paste(fimg, (int(rx), int(ry)), fimg)
                    except Exception:
                        pass
        except Exception:
            pass

        # Export to BytesIO
        bio = io.BytesIO()
        canvas.convert('RGB').save(bio, format='PNG')
        bio.seek(0)
        return discord.File(bio, filename="battle.png")

    async def _handle_roll(self, interaction: discord.Interaction, player: discord.Member, button: discord.ui.Button):
        # Ensure only the intended user can press their button
        if interaction.user.id != player.id:
            await interaction.response.send_message("This roll button isn't for you.", ephemeral=True)
            return

        # Check if already rolled
        if self.results.get(player.id) is not None:
            await interaction.response.send_message("You already rolled.", ephemeral=True)
            return

        # Acknowledge interaction immediately
        try:
            await interaction.response.defer()
        except Exception:
            pass

        # Show rolling animation by editing the original message to use the GIF
        try:
            if self.message:
                anim_embed = self.build_embed()
                anim_embed.set_image(url=DICE_GIF_URL)
                await self.message.edit(embed=anim_embed, view=self)
        except Exception:
            # ignore failures to show animation
            pass

        # Wait a short time to simulate rolling
        try:
            await asyncio.sleep(2.0)
        except Exception:
            pass

        # Determine roll value
        value = random.randint(1, 6)
        self.results[player.id] = value

        # Update button state for that player and keep player's name shown below
        button.disabled = True
        try:
            # short name (match what's used on the initial label)
            pname = getattr(player, 'display_name', '')
            if len(pname) > 18:
                pname = pname[:17].rstrip() + '…'
            button.label = f"Rolled: {value}\n{pname}"
        except Exception:
            button.label = f"Rolled: {value}"

        # Build final composite image with this player's dice face overlaid
        face_url = DICE_FACE_URLS.get(value)
        try:
            if player.id == self.challenger.id:
                img_file = await self.create_battle_image(left_face_url=face_url)
            else:
                img_file = await self.create_battle_image(right_face_url=face_url)
        except Exception:
            img_file = None

        # Send updated message with new composite image and updated view, then remove the old one
        try:
            new_embed = self.build_embed()
            if img_file:
                new_embed.set_image(url="attachment://battle.png")
                new_msg = await self.message.channel.send(embed=new_embed, file=img_file, view=self)
            else:
                # Fallback: set image to the dice face URL directly (will replace center image)
                if face_url:
                    new_embed.set_image(url=face_url)
                new_msg = await self.message.channel.send(embed=new_embed, view=self)

            # Delete previous message to avoid duplication and update stored message reference
            try:
                await self.message.delete()
            except Exception:
                pass
            self.message = new_msg
        except Exception:
            # If sending new message fails, try editing original to show final result as text
            try:
                if self.message:
                    await self.message.edit(embed=self.build_embed(), view=self)
            except Exception:
                try:
                    await interaction.followup.send(embed=self.build_embed())
                except Exception:
                    pass

        # If both have rolled, finalize: disable all buttons and update message title
        if all(v is not None for v in self.results.values()):
            for child in self.children:
                child.disabled = True
            try:
                if self.message:
                    await self.message.edit(embed=self.build_embed(), view=self)
            except Exception:
                try:
                    await interaction.followup.send(embed=self.build_embed())
                except Exception:
                    pass

    @discord.ui.button(label="Roll", style=discord.ButtonStyle.primary, custom_id="dicebattle_roll_challenger")
    async def roll_challenger(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_roll(interaction, self.challenger, button)

    @discord.ui.button(label="Roll", style=discord.ButtonStyle.primary, custom_id="dicebattle_roll_opponent")
    async def roll_opponent(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_roll(interaction, self.opponent, button)


@bot.tree.command(name="dicebattle", description="Challenge someone to a dice battle")
@app_commands.describe(opponent="Member to challenge")
async def dicebattle(interaction: discord.Interaction, opponent: discord.Member):
    """Slash command to start a two-player dice battle.

    The challenger (invoker) selects an opponent. Both players will see a Roll
    button under the embed; each button only works for the corresponding player.
    After both click, the higher roll wins.
    """
    try:
        if opponent.bot:
            await interaction.response.send_message("You can't battle a bot.", ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message("You can't battle yourself.", ephemeral=True)
            return

        view = DiceBattleView(interaction.user, opponent)
        # Build embed; defer first because creating the composite image may take >3s
        embed = view.build_embed()
        try:
            # Defer the interaction to buy time for image generation
            try:
                await interaction.response.defer()
            except Exception:
                # ignore if already deferred
                pass

            img_file = await view.create_battle_image()
            embed.set_image(url="attachment://battle.png")
            # send as followup (wait=True returns the sent message)
            sent = await interaction.followup.send(content=f"{interaction.user.mention} challenged {opponent.mention} to a dice battle!", embed=embed, file=img_file, view=view, wait=True)
            try:
                view.message = sent
            except Exception:
                view.message = None
        except Exception:
            # If image creation or sending fails, ensure we still respond
            try:
                # If we already deferred above, use followup; else fallback to response
                if interaction.response.is_done():
                    sent = await interaction.followup.send(content=f"{interaction.user.mention} challenged {opponent.mention} to a dice battle!", embed=embed, view=view, wait=True)
                else:
                    await interaction.response.send_message(content=f"{interaction.user.mention} challenged {opponent.mention} to a dice battle!", embed=embed, view=view)
                    sent = await interaction.original_response()
                try:
                    view.message = sent
                except Exception:
                    view.message = None
            except Exception:
                # Final fallback: attempt an ephemeral error message
                try:
                    await interaction.followup.send("Failed to start dice battle.", ephemeral=True)
                except Exception:
                    pass
        # store message reference for future edits
        try:
            view.message = await interaction.original_response()
        except Exception:
            # In some cases original_response may fail; try to fetch last message in channel
            try:
                channel = interaction.channel
                async for msg in channel.history(limit=5):
                    if msg.author == bot.user and msg.embeds and msg.embeds[0].title and interaction.user.display_name in msg.embeds[0].title:
                        view.message = msg
                        break
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Error in /dicebattle command: {e}")
        try:
            await interaction.response.send_message("Failed to start dice battle.", ephemeral=True)
        except Exception:
            pass



import sys, traceback, time

try:
    bot.run(TOKEN)
except BaseException as e:
    # Catch BaseException so we also capture SystemExit and KeyboardInterrupt
    logger.error(f"Bot exited with: {type(e).__name__}: {e}", exc_info=True)
    traceback.print_exc()
    # keep the process alive briefly for inspection
    for i in range(30):
        logger.error(f"Bot exited — sleeping for inspection ({i+1}/30)")
        time.sleep(1)
    # re-raise to preserve original behavior after inspection
    raise
