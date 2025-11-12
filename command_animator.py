"""
Universal Command Animation System
Adds loading animations to any Discord command with a single decorator or call.
"""

import discord
import asyncio
from thinking_animation import ThinkingAnimation

# Global animation instance
_thinking_animation = ThinkingAnimation()


class CommandAnimator:
    """Manages animations for any command"""
    
    def __init__(self):
        self.active_animations = {}  # Track running animations
    
    async def show_loading(self, interaction: discord.Interaction, message: str = "Loading..."):
        """Show a loading animation for a command"""
        try:
            await _thinking_animation.show_thinking(interaction)
            return _thinking_animation.animation_message
        except Exception as e:
            print(f"Error showing loading animation: {e}")
            return None
    
    async def stop_loading(self, interaction: discord.Interaction, delete: bool = False):
        """Stop the loading animation"""
        try:
            await _thinking_animation.stop_thinking(interaction, delete_message=delete)
        except Exception as e:
            print(f"Error stopping loading animation: {e}")
    
    async def animate_command(self, interaction: discord.Interaction, coro):
        """
        Wrapper to run a command with loading animation.
        
        Usage:
            result = await animator.animate_command(interaction, fetch_some_data())
        """
        try:
            # Show loading animation
            await self.show_loading(interaction)
            
            # Run the actual command
            result = await coro
            
            # Stop animation
            await self.stop_loading(interaction, delete=True)
            
            return result
        except Exception as e:
            await self.stop_loading(interaction, delete=True)
            raise


# Global instance for use throughout the app
animator = CommandAnimator()


def command_animation(func):
    """
    Decorator to add animation to any command function.
    
    Usage:
        @bot.tree.command(name="mycommand")
        @command_animation
        async def mycommand(interaction: discord.Interaction):
            # Your command code here
            await interaction.followup.send("Done!")
    """
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        try:
            # Defer and show animation
            await interaction.response.defer(thinking=True)
            await _thinking_animation.show_thinking(interaction)
            
            # Run the actual command
            result = await func(interaction, *args, **kwargs)
            
            # Stop animation
            await _thinking_animation.stop_thinking(interaction, delete_message=True)
            
            return result
        except Exception as e:
            try:
                await _thinking_animation.stop_thinking(interaction, delete_message=True)
            except:
                pass
            raise
    
    return wrapper
