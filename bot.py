import datetime
import logging
import sys
from collections import deque, Counter

import aiohttp
import discord
import git
from discord.ext import commands

import config
from cogs.utils.config import Config
from cogs.utils.lang import Texts
from cogs.utils.version import Version

description = """
Je suis TuxBot, le bot qui vit de l'OpenSource ! ;)
"""
build = git.Repo(search_parent_directories=True).head.object.hexsha

log = logging.getLogger(__name__)

l_extensions = (
    'cogs.admin',
    'cogs.basics',
    'cogs.utility',
    'cogs.logs',
    'cogs.poll',
    'jishaku',
)


async def _prefix_callable(bot, message: discord.message) -> list:
    extras = ['.']
    if message.guild is not None:
        extras = bot.prefixes.get(str(message.guild.id), [])

    return commands.when_mentioned_or(*extras)(bot, message)


class TuxBot(commands.AutoShardedBot):

    def __init__(self, unload: list, database):
        super().__init__(command_prefix=_prefix_callable, pm_help=None,
                         help_command=None, description=description,
                         help_attrs=dict(hidden=True),
                         activity=discord.Game(
                             name=Texts().get('Starting...'))
                         )

        self.socket_stats = Counter()
        self.command_stats = Counter()

        self.uptime: datetime = datetime.datetime.utcnow()
        self.config = config
        self.database = database
        self._prev_events = deque(maxlen=10)
        self.session = aiohttp.ClientSession(loop=self.loop)

        self.prefixes = Config('prefixes.json')
        self.blacklist = Config('blacklist.json')

        self.version = Version(10, 0, 0, pre_release='a21', build=build)

        for extension in l_extensions:
            if extension not in unload:
                try:
                    self.load_extension(extension)
                except Exception as e:
                    print(Texts().get("Failed to load extension : ")
                          + extension, file=sys.stderr)
                    log.error(Texts().get("Failed to load extension : ")
                              + extension, exc_info=e)

    async def is_owner(self, user: discord.User) -> bool:
        return user.id in config.authorized_id

    async def on_socket_response(self, msg):
        self._prev_events.append(msg)

    async def on_command_error(self, ctx: discord.ext.commands.Context, error):
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.author.send(
                Texts().get("This command cannot be used in private messages.")
            )

        elif isinstance(error, commands.DisabledCommand):
            await ctx.author.send(
                Texts().get(
                    "Sorry. This command is disabled and cannot be used."
                )
            )

    async def process_commands(self, message: discord.message):
        ctx = await self.get_context(message)

        if ctx.command is None:
            return

        await self.invoke(ctx)

    async def on_message(self, message: discord.message):
        if message.author.bot \
                or message.author.id in self.blacklist \
                or message.guild.id in self.blacklist:
            return

        await self.process_commands(message)

    async def on_ready(self):
        if not hasattr(self, 'uptime'):
            self.uptime = datetime.datetime.utcnow()

        print(Texts().get("Ready:") + f' {self.user} (ID: {self.user.id})')
        print(self.version)

        presence: dict = dict(status=discord.Status.dnd)
        if self.config.activity is not None:
            presence.update(activity=discord.Game(name=self.config.activity))

        await self.change_presence(**presence)

    @staticmethod
    async def on_resumed():
        print('resumed...')

    @property
    def logs_webhook(self) -> discord.Webhook:
        logs_webhook = self.config.logs_webhook
        webhook = discord.Webhook.partial(
            id=logs_webhook.get('id'),
            token=logs_webhook.get('token'),
            adapter=discord.AsyncWebhookAdapter(
                self.session
            )
        )

        return webhook

    async def close(self):
        await super().close()

    def run(self):
        super().run(config.token, reconnect=True)
