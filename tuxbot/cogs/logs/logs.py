"""

Based on https://github.com/Rapptz/RoboDanny/blob/3d94e89ef27f702a5f57f432a9131bdfb60bb3ec/cogs/stats.py
Adapted by Romain J.

"""

import asyncio
import datetime
import json
import logging
import textwrap
import traceback
from collections import defaultdict

import discord
import humanize
import psutil
from discord.ext import commands, tasks

from app import TuxBot
from utils.functions.extra import command_extra

log = logging.getLogger(__name__)


class GatewayHandler(logging.Handler):
    def __init__(self, cog):
        self.cog = cog
        super().__init__(logging.INFO)

    def filter(self, record):
        return (
            record.name == "discord.gateway"
            or "Shard ID" in record.msg
            or "Websocket closed " in record.msg
        )

    def emit(self, record):
        self.cog.add_record(record)


class Logs(commands.Cog):
    def __init__(self, bot: TuxBot):
        self.bot = bot
        self.process = psutil.Process()
        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self._data_batch = []
        self._gateway_queue = asyncio.Queue(loop=bot.loop)
        self.gateway_worker.start()

        self._resumes = []
        self._identifies = defaultdict(list)

    def _clear_gateway_data(self):
        one_week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        to_remove = [
            index for index, dt in enumerate(self._resumes) if dt < one_week_ago
        ]
        for index in reversed(to_remove):
            del self._resumes[index]

        for shard_id, dates in self._identifies.items():
            to_remove = [index for index, dt in enumerate(dates) if dt < one_week_ago]
            for index in reversed(to_remove):
                del dates[index]

    @tasks.loop(seconds=0.0)
    async def gateway_worker(self):
        record = await self._gateway_queue.get()
        await self.notify_gateway_status(record)

    async def register_command(self, ctx):
        if ctx.command is None:
            return

        command = ctx.command.qualified_name
        self.bot.command_stats[command] += 1
        message = ctx.message
        if ctx.guild is None:
            destination = "Private Message"
            guild_id = None
        else:
            destination = f"#{message.channel} ({message.guild})"
            guild_id = ctx.guild.id

        log.info(
            f"{message.created_at}: {message.author} "
            f"in {destination}: {message.content}"
        )
        async with self._batch_lock:
            self._data_batch.append(
                {
                    "guild": guild_id,
                    "channel": ctx.channel.id,
                    "author": ctx.author.id,
                    "used": message.created_at.isoformat(),
                    "prefix": ctx.prefix,
                    "command": command,
                    "failed": ctx.command_failed,
                }
            )

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        await self.register_command(ctx)

    @commands.Cog.listener()
    async def on_socket_response(self, msg):
        self.bot.socket_stats[msg.get("t")] += 1

    @property
    def logs(self):
        webhooks = {}

        for key, value in self.bot.logs_channels.items():
            webhooks[key] = discord.Webhook.partial(
                id=value.get("webhook")["id"],
                token=value.get("webhook")["token"],
                adapter=discord.AsyncWebhookAdapter(self.bot.session),
            )

        return webhooks

    async def log_error(self, *, ctx=None, extra=None):
        e = discord.Embed(title="Error", colour=0xDD5F53)
        e.description = f"```py\n{traceback.format_exc()}\n```"
        e.add_field(name="Extra", value=extra, inline=False)
        e.timestamp = datetime.datetime.utcnow()

        if ctx is not None:
            fmt = "{0} (ID: {0.id})"
            author = fmt.format(ctx.author)
            channel = fmt.format(ctx.channel)
            guild = "None" if ctx.guild is None else fmt.format(ctx.guild)

            e.add_field(name="Author", value=author)
            e.add_field(name="Channel", value=channel)
            e.add_field(name="Guild", value=guild)

        await self.logs.get("errors").send(embed=e)

    async def send_guild_stats(self, e, guild):
        e.add_field(name="Name", value=guild.name)
        e.add_field(name="ID", value=guild.id)
        e.add_field(name="Shard ID", value=guild.shard_id or "N/A")
        e.add_field(name="Owner", value=f"{guild.owner} (ID: {guild.owner.id})")

        bots = sum(member.bot for member in guild.members)
        total = guild.member_count
        online = sum(member.status is discord.Status.online for member in guild.members)

        e.add_field(name="Members", value=str(total))
        e.add_field(name="Bots", value=f"{bots} ({bots / total:.2%})")
        e.add_field(name="Online", value=f"{online} ({online / total:.2%})")

        if guild.icon:
            e.set_thumbnail(url=guild.icon_url)

        if guild.me:
            e.timestamp = guild.me.joined_at

        await self.logs.get("guilds").send(embed=e)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.guild):
        e = discord.Embed(colour=0x53DDA4, title="New Guild")  # green colour
        await self.send_guild_stats(e, guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.guild):
        e = discord.Embed(colour=0xDD5F53, title="Left Guild")  # red colour
        await self.send_guild_stats(e, guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.message):
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        if isinstance(message.channel, discord.DMChannel):
            if message.author is self.bot.user:
                e = discord.Embed(
                    title=f"DM to: {message.channel.recipient}",
                    description=message.content,
                    color=0x39E326,
                )
            else:
                e = discord.Embed(
                    title="New DM:", description=message.content, color=0x0A97F5
                )
            e.set_author(
                name=message.channel.recipient,
                icon_url=message.channel.recipient.avatar_url_as(format="png"),
            )

            if message.attachments:
                attachment_url = message.attachments[0].url
                e.set_image(url=attachment_url)

            e.set_footer(text=f"User ID: {message.channel.recipient.id}")

            await self.logs["dm"].send(embed=e)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        await self.register_command(ctx)
        if not isinstance(
            error, (commands.CommandInvokeError, commands.ConversionError)
        ):
            return

        error = error.original
        if isinstance(error, (discord.Forbidden, discord.NotFound)):
            return

        e = discord.Embed(title="Command Error", colour=0xCC3366)
        e.add_field(name="Name", value=ctx.command.qualified_name)
        e.add_field(name="Author", value=f"{ctx.author} (ID: {ctx.author.id})")

        fmt = f"Channel: {ctx.channel} (ID: {ctx.channel.id})"
        if ctx.guild:
            fmt = f"{fmt}\nGuild: {ctx.guild} (ID: {ctx.guild.id})"

        e.add_field(name="Location", value=fmt, inline=False)
        e.add_field(
            name="Content", value=textwrap.shorten(ctx.message.content, width=512)
        )

        exc = "".join(
            traceback.format_exception(
                type(error), error, error.__traceback__, chain=False
            )
        )
        e.description = f"```py\n{exc}\n```"
        e.timestamp = datetime.datetime.utcnow()
        await self.logs.get("errors").send(embed=e)

    @commands.Cog.listener()
    async def on_socket_raw_send(self, data):
        if '"op":2' not in data and '"op":6' not in data:
            return

        back_to_json = json.loads(data)
        if back_to_json["op"] == 2:
            payload = back_to_json["d"]
            inner_shard = payload.get("shard", [0])
            self._identifies[inner_shard[0]].append(datetime.datetime.utcnow())
        else:
            self._resumes.append(datetime.datetime.utcnow())

        self._clear_gateway_data()

    def add_record(self, record):
        self._gateway_queue.put_nowait(record)

    async def notify_gateway_status(self, record):
        types = {"INFO": ":information_source:", "WARNING": ":warning:"}

        emoji = types.get(record.levelname, ":heavy_multiplication_x:")
        dt = datetime.datetime.utcfromtimestamp(record.created)
        msg = f"{emoji} `[{dt:%Y-%m-%d %H:%M:%S}] {record.message}`"
        await self.logs.get("gateway").send(msg)

    @command_extra(name="commandstats")
    @commands.is_owner()
    async def _commandstats(self, ctx, limit=20):
        counter = self.bot.command_stats
        width = len(max(counter, key=len))

        if limit > 0:
            common = counter.most_common(limit)
        else:
            common = counter.most_common()[limit:]

        output = "\n".join(f"{k:<{width}}: {c}" for k, c in common)

        await ctx.send(f"```\n{output}\n```")

    @commands.command("socketstats")
    @commands.is_owner()
    async def _socketstats(self, ctx):
        delta = datetime.datetime.utcnow() - self.bot.uptime
        minutes = delta.total_seconds() / 60
        total = sum(self.bot.socket_stats.values())
        cpm = total / minutes
        await ctx.send(
            f"{total} socket events observed ({cpm:.2f}/minute):\n"
            f"{self.bot.socket_stats}"
        )

    @commands.command("uptime")
    async def _uptime(self, ctx):
        uptime = humanize.naturaltime(datetime.datetime.utcnow() - self.bot.uptime)
        await ctx.send(f"Uptime: **{uptime}**")


async def on_error(self, event, *args):
    e = discord.Embed(title="Event Error", colour=0xA32952)
    e.add_field(name="Event", value=event)
    e.description = f"```py\n{traceback.format_exc()}\n```"
    e.timestamp = datetime.datetime.utcnow()

    args_str = ["```py"]
    for index, arg in enumerate(args):
        args_str.append(f"[{index}]: {arg!r}")
    args_str.append("```")
    e.add_field(name="Args", value="\n".join(args_str), inline=False)

    hook = self.get_cog("Logs").logs.get("errors")
    try:
        await hook.send(embed=e)
    except (
        discord.HTTPException,
        discord.NotFound,
        discord.Forbidden,
        discord.InvalidArgument,
    ):
        pass