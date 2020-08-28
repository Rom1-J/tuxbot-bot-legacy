import asyncio
import datetime
import logging
import sys
from typing import List, Union

import discord
from discord.ext import commands
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, TextColumn, BarColumn
from rich.table import Table
from rich.traceback import install
from tuxbot import version_info

from . import Config
from .data_manager import logs_data_path

from . import __version__, ExitCodes
from .utils.functions.extra import ContextPlus

log = logging.getLogger("tuxbot")
console = Console()
install(console=console)

NAME = r"""
  _____           _           _        _           _   
 |_   _|   ___  _| |__   ___ | |_     | |__   ___ | |_ 
   | || | | \ \/ / '_ \ / _ \| __|____| '_ \ / _ \| __|
   | || |_| |>  <| |_) | (_) | ||_____| |_) | (_) | |_ 
   |_| \__,_/_/\_\_.__/ \___/ \__|    |_.__/ \___/ \__|                                    
"""

packages: List[str] = ["jishaku", "tuxbot.cogs.warnings", "tuxbot.cogs.admin"]


class Tux(commands.AutoShardedBot):
    _loading: asyncio.Task
    _progress = {
        'main': Progress(
            TextColumn("[bold blue]{task.fields[task_name]}", justify="right"),
            BarColumn()
        ),
        'tasks': {}
    }

    def __init__(self, *args, cli_flags=None, **kwargs):
        # by default, if the bot shutdown without any intervention,
        # it's a crash
        self.shutdown_code = ExitCodes.CRITICAL
        self.cli_flags = cli_flags
        self.instance_name = self.cli_flags.instance_name
        self.last_exception = None
        self.logs = logs_data_path(self.instance_name)

        self.config = Config(self.instance_name)

        async def _prefixes(bot, message) -> List[str]:
            prefixes = self.config("core").get("prefixes")

            prefixes.extend(self.config.get_prefixes(message.guild))

            if self.config("core").get("mentionable"):
                return commands.when_mentioned_or(*prefixes)(bot, message)
            return prefixes

        if "command_prefix" not in kwargs:
            kwargs["command_prefix"] = _prefixes

        if "owner_ids" in kwargs:
            kwargs["owner_ids"] = set(kwargs["owner_ids"])
        else:
            kwargs["owner_ids"] = self.config.owners_id()

        message_cache_size = 100_000
        kwargs["max_messages"] = message_cache_size
        self.max_messages = message_cache_size

        self.uptime = None
        self._app_owners_fetched = False  # to prevent abusive API calls

        super().__init__(*args, help_command=None, **kwargs)

    async def load_packages(self):
        if packages:
            with Progress() as progress:
                task = progress.add_task(
                    "Loading packages...",
                    total=len(packages)
                )

                for package in packages:
                    try:
                        self.load_extension(package)
                        progress.console.print(f"{package} loaded")
                    except Exception as e:
                        log.exception(
                            f"Failed to load package {package}",
                            exc_info=e
                        )
                        progress.console.print(
                            f"[red]Failed to load package {package} "
                            f"[i](see "
                            f"{str((self.logs / 'tuxbot.log').resolve())} "
                            f"for more details)[/i]"
                        )

                    progress.advance(task)

    async def on_ready(self):
        self.uptime = datetime.datetime.now()
        self._progress.get("main").stop_task(
            self._progress.get("tasks")["connecting"]
        )
        self._progress.get("main").remove_task(
            self._progress.get("tasks")["connecting"]
        )
        console.clear()

        console.print(
            Panel(f"[bold blue]Tuxbot V{version_info.major}", style="blue"),
            justify="center"
        )
        console.print()

        columns = Columns(expand=True, padding=2, align="center")

        table = Table(
            style="dim", border_style="not dim",
            box=box.HEAVY_HEAD
        )
        table.add_column(
            "INFO",
        )
        table.add_row(str(self.user))
        table.add_row(f"Prefixes: {', '.join(self.config('core').get('prefixes'))}")
        table.add_row(f"Language: {self.config('core').get('locale')}")
        table.add_row(f"Tuxbot Version: {__version__}")
        table.add_row(f"Discord.py Version: {discord.__version__}")
        table.add_row(f"Shards: {self.shard_count}")
        table.add_row(f"Servers: {len(self.guilds)}")
        table.add_row(f"Users: {len(self.users)}")
        columns.add_renderable(table)

        table = Table(
            style="dim", border_style="not dim",
            box=box.HEAVY_HEAD
        )
        table.add_column(
            "COGS",
        )
        for extension in packages:
            if extension in self.extensions:
                status = f"[green]:heavy_check_mark: {extension} "
            else:
                status = f"[red]:cross_mark: {extension} "

            table.add_row(status)
        columns.add_renderable(table)

        console.print(columns)
        console.print()

    async def is_owner(self,
                       user: Union[discord.User, discord.Member]) -> bool:
        """Determines if the user is a bot owner.

        Parameters
        ----------
        user: Union[discord.User, discord.Member]

        Returns
        -------
        bool
        """
        if user.id in self.config.owners_id():
            return True

        owner = False
        if not self._app_owners_fetched:
            app = await self.application_info()
            if app.team:
                ids = [m.id for m in app.team.members]
                await self.config.update("core", "owners_id", ids)
                owner = user.id in ids
            self._app_owners_fetched = True

        return owner

    async def get_context(self, message: discord.Message, *, cls=None):
        return await super().get_context(message, cls=ContextPlus)

    async def process_commands(self, message: discord.Message):
        """Check for blacklists.

        """
        if message.author.bot:
            return

        if (
                message.guild.id in self.config.get_blacklist("guild")
                or message.channel.id in self.config.get_blacklist("channel")
                or message.author.id in self.config.get_blacklist("user")
        ):
            return

        ctx = await self.get_context(message)

        if ctx is None or ctx.valid is False:
            self.dispatch("message_without_command", message)
        else:
            await self.invoke(ctx)

    async def on_message(self, message: discord.Message):
        await self.process_commands(message)

    async def start(self, token, bot):
        """Connect to Discord and start all connections.

        Todo: add postgresql connect here
        """
        with self._progress.get("main") as pg:
            task_id = self._progress.get("tasks")["connecting"] = pg.add_task(
                "connecting",
                task_name="Connecting to Discord...", start=False
            )
            pg.update(task_id)
            await super().start(token, bot=bot)

    async def logout(self):
        """Disconnect from Discord and closes all actives connections.

        Todo: add postgresql logout here
        """
        for task in self._progress.get("tasks").keys():
            self._progress.get("main").log("Shutting down", task)

            self._progress.get("main").stop_task(
                self._progress.get("tasks")[task]
            )
            self._progress.get("main").remove_task(
                self._progress.get("tasks")["connecting"]
            )
        self._progress.get("main").stop()

        pending = [
            t for t in asyncio.all_tasks() if
            t is not asyncio.current_task()
        ]

        for task in pending:
            console.log("Canceling", task.get_name(), f"({task.get_coro()})")
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        await super().logout()

    async def shutdown(self, *, restart: bool = False):
        """Gracefully quit.

        Parameters
        ----------
        restart:bool
            If `True`, systemd or the launcher gonna see custom exit code
            and reboot.

        """
        if not restart:
            self.shutdown_code = ExitCodes.SHUTDOWN
        else:
            self.shutdown_code = ExitCodes.RESTART

        await self.logout()
        sys.exit(self.shutdown_code)