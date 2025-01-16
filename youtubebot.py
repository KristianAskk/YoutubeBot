import os
import sys
import asyncio
import shutil
import discord
import yt_dlp
from discord.ext import commands
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import re

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
PREFIX = os.getenv('BOT_PREFIX', '.')
YTDL_FORMAT = os.getenv('YTDL_FORMAT', 'worstaudio')
PRINT_STACK_TRACE = os.getenv('PRINT_STACK_TRACE', '1').lower() in ('true', 't', '1')
BOT_REPORT_COMMAND_NOT_FOUND = os.getenv('BOT_REPORT_COMMAND_NOT_FOUND', '1').lower() in ('true', 't', '1')
BOT_REPORT_DL_ERROR = os.getenv('BOT_REPORT_DL_ERROR', '0').lower() in ('true', 't', '1')

try:
    COLOR = int(os.getenv('BOT_COLOR', 'ff0000'), 16)
except ValueError:
    print('the BOT_COLOR in .env is not a valid hex color, using default ff0000')
    COLOR = 0xff0000

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

class GuildAudioState:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.voice_client: discord.VoiceClient = None
        self.loop = False
        self.now_playing = None

    def is_playing(self) -> bool:
        return self.voice_client and self.voice_client.is_playing()

guild_states = {}

thread_pool = ThreadPoolExecutor(max_workers=2)

async def download_audio(server_id: int, query: str):
    """
    Runs the youtube-dl/yt-dlp download in a separate thread, returning
    the local file path and the info dict.
    """
    ydl_opts = {
        'format': YTDL_FORMAT,
        'source_address': '0.0.0.0',
        'default_search': 'ytsearch',
        'outtmpl': '%(id)s.%(ext)s',
        'noplaylist': True,
        'allow_playlist_files': False,
        'paths': {'home': f'./dl/{server_id}'},
    }

    def _blocking_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if 'entries' in info:
                info = info['entries'][0]
            ydl.download([info['webpage_url']])
            return info

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(thread_pool, _blocking_download)
    local_path = f'./dl/{server_id}/{info["id"]}.{info["ext"]}'
    return local_path, info

def after_play(error, guild_id, last_track):
    """
    Called when ffmpeg finishes or errors out for the last track.
    We schedule the next track in the event loop.
    """
    if error:
        print(f"Playback error in guild {guild_id}: {error}", file=sys.stderr)

    # Schedule the next track (don't block here)
    bot.loop.call_soon_threadsafe(
        lambda: asyncio.create_task(next_track(guild_id, last_track))
    )


async def next_track(guild_id: int, last_track):
    """
    Called after a track finishes. If loop is on, requeue the last track.
    Then pop a new one from the queue and play it.
    """
    state = guild_states.get(guild_id)
    if not state or not state.voice_client:
        return

    if state.loop and last_track is not None:
        await state.queue.put(last_track)

    try:
        local_path, info = state.queue.get_nowait()
    except asyncio.QueueEmpty:
        # Queue is empty, no more tracks to play
        state.now_playing = None
        return

    state.now_playing = (local_path, info)
    _play_audio(guild_id, local_path, info)


def _play_audio(guild_id: int, local_path: str, info: dict):
    """
    Actually plays the audio file in the voice client, attaching the callback.
    """
    state = guild_states[guild_id]
    vc = state.voice_client
    if not vc or not vc.is_connected():
        return

    vc.play(
        discord.FFmpegOpusAudio(local_path),
        after=lambda err: after_play(err, guild_id, (local_path, info))
    )


@bot.command(name='play', aliases=['p'])
async def play_cmd(ctx: commands.Context, *, query: str):
    """
    Download or search for audio, enqueue it, and start playback if idle.
    """
    voice_state = ctx.author.voice
    if not voice_state or not voice_state.channel:
        return await ctx.send("You must be in a voice channel.")

    guild_id = ctx.guild.id
    # Ensure we have a GuildAudioState
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildAudioState()
    state = guild_states[guild_id]

    if not state.voice_client or not state.voice_client.is_connected():
        try:
            state.voice_client = await voice_state.channel.connect()
        except discord.ClientException:
            state.voice_client = get_voice_client_from_channel_id(voice_state.channel.id)

    await ctx.send(f"Downloading `{query}`...")

    try:
        local_path, info = await download_audio(guild_id, query)
    except yt_dlp.utils.DownloadError as err:
        if BOT_REPORT_DL_ERROR:
            sanitized = re.compile(r'\x1b[^m]*m').sub('', str(err)).strip()
            if sanitized.lower().startswith("error"):
                sanitized = sanitized[5:].strip(" :")
            await ctx.send(f"**Download error**: {sanitized}")
        else:
            await ctx.send("Failed to download that video.")
        return

    title = info.get("title", "Unknown Title")
    await ctx.send(f"Queued: **{title}**")

    await state.queue.put((local_path, info))

    if not state.is_playing():
        await next_track(guild_id, None)


@bot.command(name='skip', aliases=['s'])
async def skip_cmd(ctx: commands.Context):
    """
    Skip the currently playing track.
    """
    guild_id = ctx.guild.id
    voice_state = ctx.author.voice
    if not voice_state or not voice_state.channel:
        return await ctx.send("You must be in a voice channel to skip.")

    state = guild_states.get(guild_id)
    if not state or not state.voice_client:
        return await ctx.send("Nothing is playing here.")

    if state.voice_client.is_playing():
        state.voice_client.stop()
        await ctx.send("Skipped the current track.")
    else:
        await ctx.send("No audio is currently playing.")


@bot.command(name='queue', aliases=['q'])
async def queue_cmd(ctx: commands.Context):
    """
    Show the queued items (not including what's currently playing).
    """
    guild_id = ctx.guild.id
    state = guild_states.get(guild_id)
    if not state:
        return await ctx.send("No queue in this server.")

    items = list(state.queue._queue)  # Careful, it's a private member
    if not items:
        return await ctx.send("The queue is empty.")

    desc = "\n".join(f"{idx+1}. {info.get('title','???')}" for idx, (path, info) in enumerate(items))
    embed = discord.Embed(title="Queue", description=desc, color=COLOR)

    if state.now_playing:
        embed.add_field(name="Now Playing", value=state.now_playing[1].get('title'), inline=False)

    await ctx.send(embed=embed)


@bot.command(name='loop')
async def loop_cmd(ctx: commands.Context):
    """
    Toggle single-track looping (the current track gets re-queued if it's on).
    """
    guild_id = ctx.guild.id
    state = guild_states.get(guild_id)
    if not state:
        return await ctx.send("Nothing is playing here.")

    state.loop = not state.loop
    await ctx.send(f"Looping is now **{state.loop}**.")


@bot.command(name='stop')
async def stop_cmd(ctx: commands.Context):
    """
    Stop playing and clear the queue. The bot remains in the channel.
    """
    guild_id = ctx.guild.id
    state = guild_states.get(guild_id)
    if not state:
        return await ctx.send("Nothing to stop here.")

    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.stop()
    # Clear the queue
    while not state.queue.empty():
        state.queue.get_nowait()
    state.now_playing = None

    await ctx.send("Playback stopped and queue cleared.")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """
    If the bot leaves/kicked from a channel, cleanup that guild's folder and state.
    """
    if member != bot.user:
        return

    if before.channel is not None and after.channel is None:
        # Bot disconnected
        server_id = before.channel.guild.id
        if server_id in guild_states:
            try:
                shutil.rmtree(f'./dl/{server_id}/')
            except FileNotFoundError:
                pass
            guild_states.pop(server_id, None)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        if BOT_REPORT_COMMAND_NOT_FOUND:
            await ctx.send(f"Command not recognized. Type `{PREFIX}help` to see available commands.")
    else:
        if PRINT_STACK_TRACE:
            raise error
        else:
            print(f"Unhandled command error: {error}", file=sys.stderr)

def get_voice_client_from_channel_id(channel_id: int):
    for vc in bot.voice_clients:
        if vc.channel and vc.channel.id == channel_id:
            return vc

def main():
    if not TOKEN:
        print("No token provided. Please set BOT_TOKEN=... in your .env file.")
        return 1
    bot.run(TOKEN)

if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemError as error:
        if PRINT_STACK_TRACE:
            raise
        else:
            print(error)