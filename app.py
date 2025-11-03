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
from reminder_system import ReminderSystem
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


load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix='!', intents=intents)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add file handler for chat logs
file_handler = logging.FileHandler('chat_logs.txt')
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)


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
        
        # Load music cog
        try:
            await bot.load_extension('music_cog')
            logger.info('Music cog loaded successfully')
            
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
        content = message.content.replace('\n', ' ').strip()  # Replace newlines for single line log

        log_message = f"[GUILD: {guild_name} ({guild_id})] [CHANNEL: {channel_name} ({channel_id})] [AUTHOR: {author_name} ({author_id})] Message: {content}"
        logger.info(log_message)

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

        # Check if the question is an image creation request
        if question.lower().startswith("create an image") or question.lower().startswith("generate an image") or question.lower().startswith("make an image"):
            # Extract the prompt from the question
            # For example, "create an image of a sunset over mountains" -> "a sunset over mountains"
            prompt = question.split(" ", 3)[-1] if len(question.split(" ", 3)) > 3 else question

            # Call the imagine command logic directly
            # Show thinking animation while processing
            await thinking_animation.show_thinking(interaction)

            try:
                # Generate the image
                image_data = await make_image_request(prompt)

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

@bot.tree.command(name="giftcode", description="Get active Whiteout Survival gift codes")
async def giftcode(interaction: discord.Interaction):
    # Show thinking animation while processing
    await thinking_animation.show_thinking(interaction)
    
    try:
        codes = await get_active_gift_codes()
        if not codes:
            await interaction.followup.send("No active gift codes available right now. Check back later! 🎁", ephemeral=False)
            return
        # Helper to build the embed from a codes list
        def build_codes_embed(codes_list):
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

        # View with Copy and Refresh buttons
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
                # Append the signature line requested by user
                plain_text += "\n\nGift Code :gift:  STATE #3063"

                # Acknowledge the interaction and attempt to DM the user
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
                        # Fallback: show the full plain text in an ephemeral followup
                        await interaction_button.followup.send(f"Couldn't DM you. Here are the codes:\n\n{plain_text}", ephemeral=True)
                except Exception:
                    logger.debug("Failed to send followup after DM attempt")

            @discord.ui.button(label="Refresh Codes", style=discord.ButtonStyle.secondary, custom_id="giftcode_refresh")
            async def refresh_button(self, interaction_button: discord.Interaction, button: discord.ui.Button):
                # Defer to give us time to fetch and edit
                await interaction_button.response.defer(ephemeral=True)
                try:
                    new_codes = await get_active_gift_codes()
                    if not new_codes:
                        await interaction_button.followup.send("No active gift codes available right now.", ephemeral=True)
                        return

                    self.codes = new_codes
                    new_embed = build_codes_embed(self.codes)

                    # Edit the original message that contains the embed
                    if self.message:
                        try:
                            await self.message.edit(embed=new_embed)
                            await interaction_button.followup.send("Gift codes refreshed.", ephemeral=True)
                        except Exception as edit_err:
                            logger.error(f"Failed to edit gift code message: {edit_err}")
                            await interaction_button.followup.send("Failed to update the gift codes message.", ephemeral=True)
                    else:
                        # If we don't have the message reference, just send a new followup
                        await interaction_button.followup.send(embed=new_embed, ephemeral=False)

                except Exception as e:
                    logger.error(f"Error refreshing gift codes via button: {e}")
                    await interaction_button.followup.send("Error while refreshing gift codes.", ephemeral=True)

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
            # In some cases the followup send returns None; attempt to fetch the last message in channel
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
    try:
        codes = await get_active_gift_codes()
        if not codes:
            await interaction.followup.send("No active gift codes available right now. Check back later! 🎁", ephemeral=False)
            return
        
        embed = discord.Embed(
            title="✨ Active Whiteout Survival Gift Codes ✨",
            color=0xffd700,
            description=f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        embed.set_thumbnail(url="https://i.postimg.cc/s2xHV7N7/Groovy-gift.gif")

        for code in codes[:10]:  # Limit to 10 codes
            name = f"🎟️ Code:"
            value = f"```{code['code']}```\n*Rewards:* {code['rewards'] if 'rewards' in code else 'Rewards not specified'}\n*Expires:* {code.get('expiry', 'Unknown')}"
            embed.add_field(name=name, value=value, inline=False)

        if len(codes) > 10:
            embed.set_footer(text=f"And {len(codes) - 10} more codes...")
        else:
            embed.set_footer(text="Use /giftcode to see all active codes!")

        # Stop the animation before editing the message
        await thinking_animation.stop_thinking(interaction, delete_message=False)

        # Edit the animation message with the gift codes
        if thinking_animation.animation_message:
            try:
                await thinking_animation.animation_message.edit(
                    content=f"{interaction.user.display_name} requested gift codes",
                    embed=embed
                )
                logger.info("Successfully edited animation message with gift codes")
            except Exception as edit_error:
                logger.error(f"Failed to edit animation message with gift codes: {edit_error}")
                # Fallback to followup send
                await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)
    except Exception as e:
        logger.error(f"Error in giftcode command: {str(e)}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Sorry, there was an error fetching gift codes. Please try again later! 🎁", ephemeral=False)
            else:
                await interaction.response.send_message("Sorry, there was an error fetching gift codes. Please try again later! 🎁", ephemeral=False)
        except Exception as e2:
            logger.error(f"Failed to send error message: {str(e2)}")

@bot.tree.command(name="reminder", description="Set a reminder with time and message")
@app_commands.describe(
    time="When to remind you (e.g., '5 minutes', 'tomorrow 3pm IST', 'daily at 9am')",
    message="What to remind you about",
    channel="Channel to send reminder in (required)"
)
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

@bot.tree.command(name="delete_reminder", description="Delete a specific reminder")
@app_commands.describe(reminder_id="The ID of the reminder to delete (use /listreminder to see IDs)")
async def delete_reminder(interaction: discord.Interaction, reminder_id: int):
    try:
        await reminder_system.delete_user_reminder(interaction, reminder_id)
    except Exception as e:
        logger.error(f"Error in delete_reminder command: {str(e)}")
        try:
            await interaction.response.send_message("❌ **Error**\n\nSorry, there was an error deleting the reminder. Please try again.", ephemeral=True)
        except Exception as followup_error:
            logger.warning(f"Could not send error response: {followup_error}")
            try:
                await interaction.followup.send("❌ **Error**\n\nSorry, there was an error deleting the reminder. Please try again.", ephemeral=True)
            except Exception as final_error:
                logger.error(f"Failed to send error followup: {final_error}")

@bot.tree.command(name="listreminder", description="View all active reminders in the system (admin)")
async def listreminder(interaction: discord.Interaction):
    try:
        await reminder_system.list_all_active_reminders(interaction)
    except Exception as e:
        logger.error(f"Error in listreminder command: {str(e)}")
        try:
            await interaction.response.send_message("❌ **Error**\n\nSorry, there was an error fetching all reminders. Please try again.", ephemeral=True)
        except Exception as followup_error:
            logger.warning(f"Could not send error response: {followup_error}")
            try:
                await interaction.followup.send("❌ **Error**\n\nSorry, there was an error fetching all reminders. Please try again.", ephemeral=True)
            except Exception as final_error:
                logger.error(f"Failed to send error message: {final_error}")

@bot.tree.command(name="imagine", description="Generate an image using AI based on your description")
@app_commands.describe(prompt="Describe the image you want to generate")
async def imagine(interaction: discord.Interaction, prompt: str):
    # Show thinking animation while processing
    await thinking_animation.show_thinking(interaction)

    try:
        # Generate the image
        image_data = await make_image_request(prompt)

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
        logger.error(f"Error in imagine command: {str(e)}")
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
        description="**🤖 AI & Fun Commands**\n"
                    "• **/imagine [prompt]** - Generate an AI image from your description\n"
                    "• **/ask [question]** - Ask questions or get help with anything\n\n"
                    "**🎁 Gift Codes**\n"
                    "• **/giftcode** - Get active Whiteout Survival gift codes\n\n"
                    "**📅 Reminder Commands**\n"
                    "• **/reminder [time] [message] [channel]** - Set a timed reminder\n"
                    "• **/delete_reminder [id]** - Delete a specific reminder\n"
                    "• **/listreminder** - View all active reminders (admin only)\n\n"
                    "**📊 Server Utility**\n"
                    "• **/serverstats** - View detailed server statistics\n"
                    "• **/mostactive** - See top active users and monthly activity graph\n\n"
                    "**🎪 Events & Personality**\n"
                    "• **/event [name]** - Get info on events (use autocomplete)\n"
                    "• **/add_trait [trait]** - Add a trait to personalize your profile\n\n"
                    "**❓ Help**\n"
                    "• **/help** - Show this command list",
        color=0x1abc9c
    )
    embed.set_thumbnail(url="https://i.postimg.cc/Fzq03CJf/a463d7c7-7fc7-47fc-b24d-1324383ee2ff-removebg-preview.png")
    embed.set_footer(text="Type a command to get started!")
    await interaction.response.send_message(embed=embed, ephemeral=False)


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
