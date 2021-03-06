import asyncio
import logging
from json import dumps
from typing import Union, List

import aiohttp

import discord

# noinspection PyPep8Naming
from discord import Client as C, AutoShardedClient as AC

# noinspection PyPep8Naming
from discord.ext.commands import Bot as B, AutoShardedBot as AB
from discord.ext.tasks import loop

from .errors import ToppyError, Forbidden, TopGGServerError, Ratelimited, NotFound
from .models import Bot, SimpleUser, BotStats, User, BotSearchResults
from .ratelimiter import routes

__version__ = "1.1.2"
__api_version__ = "v0"
_base_ = "https://top.gg/api"
logger = logging.getLogger(__name__)


class TopGG:
    r"""
    The client class for the top.gg API.

    This class handles everything for the top.gg API, APART FROM voting webhooks - Those are handled by the server
    class.
    """

    def __init__(
        self, bot: Union[C, B, AC, AB], *, token: str, autopost: bool = True, ignore_local_ratelimit: bool = False
    ):
        r"""
        Initialises an instance of the top.gg client. Please don't call this multiple times, it WILL break stuff.

        Parameters
        ----------
        bot:
            The bot instance to use. Can be client or bot, and their auto-sharded equivalents.
        token: :class:`str`
            Your bot's API token from top.gg.
        autopost: :class:`bool`
            Whether to automatically post server count every 30 minutes or not.
        ignore_local_ratelimit: :class:`bool`
            If true, this will ignore experimental on-site ratelimiting, which is an internalised way of preventing 429s
        """
        self.bot = bot
        self.token = token
        self.ignore_local_ratelimit = ignore_local_ratelimit
        # noinspection PyTypeChecker
        self.session = None  # type: aiohttp.ClientSession

        async def set_session():
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": f"top.py (version {__version__}, https://github.com/dragdev-studios/top.py)",
                    "Authorization": self.token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )

        # Why is this here?
        # There's an undesirable warning if you make a session from outside an async function. Kinda crap but meh.
        if not self.bot.loop.is_running():
            logger.debug("Stealing idle loop to create aiohttp session.")
            self.bot.loop.run_until_complete(set_session())
        else:
            logger.debug("Loop is in use - creating task to create aiohttp session.")
            self._session_setter = self.bot.loop.create_task(set_session())

        if autopost:
            logger.debug("Starting autopost task.")
            self.autopost.start()

        # Function aliases
        self.vote_check = self.upvote_check
        self.has_upvoted = self.upvote_check
        self.get_user_vote = self.upvote_check

        logger.debug(
            f"TopGG [top.py] has been initialised:\n\t- bot: {id(self.bot)}\n\t- API Token: {self.token[:5]}...\n\t"
            f"- Session: {id(self.session) if self.session else None}\n\t- Autoposting stats? "
            f"{ {True: 'Y', False: 'N'}[autopost]}\n"
        )

    def __del__(self):
        r"""Lower-level garbage collection function fired when the variable is discarded, performs cleanup."""
        logger.debug(f"{id(self)} __del__ called - Stopping autopost task")
        self.autopost.stop()

    @property
    def vote_url(self) -> str:
        r"""Just gives you a link to your bot's vote page."""
        if not self.bot.is_ready():
            raise TypeError("Bot is not ready, can't produce a vote URL.")
        return f"https://top.gg/bot/{self.bot.user.id}/vote"

    @property
    def invite_url(self) -> str:
        r"""Just gives you a link to your bot's invite page."""
        if not self.bot.is_ready():
            raise TypeError("Bot is not ready, can't produce an invite URL.")
        return f"https://top.gg/bot/{self.bot.user.id}/invite"

    async def _wf_s(self):
        if not self.session:
            logger.warning("self does not contain an aiohttp session. Forcing session setter task to run now.")
            await self._session_setter
        return

    @loop(minutes=30)
    async def autopost(self):
        r"""The task that automatically posts our stats to top.gg."""
        result = await self.post_stats()
        self.bot.dispatch("toppy_stat_autopost", result)

    async def _request(self, method: str, uri: str, **kwargs) -> dict:
        # Hello fellow code explorer!
        # Yes, this is the function that single-handedly carries this module
        # Yes, it's a bit jank
        # Yes, you're welcome to tidy it up
        # No, there's no need to change anything
        # It works perfectly fine
        # JUST DON'T *TRY* TO BREAK IT
        # Many thanks, eek
        if not self.ignore_local_ratelimit:
            if "/bots/" in uri:
                rlc = routes["/bots/*"]
                if rlc.ratelimited:
                    logger.warning(f"Ratelimted for {rlc.retry_after*1000}ms. Handled under the bucket /bots/*.")
                    raise Ratelimited(rlc.retry_after, internal=True)
            if routes["*"].ratelimited:
                logger.warning(
                    f"Ratelimited for {routes['*'].retry_after*1000}ms. Handled under the bucket /*."
                    f" Perhaps review how many requests you're sending?"
                )
                raise Ratelimited(routes["*"].retry_after, internal=True)

        if kwargs.get("data") and isinstance(kwargs["data"], dict):
            kwargs["data"] = dumps(kwargs["data"])

        fail_if_timeout = kwargs.pop("fail_if_timeout", True)
        expected_codes = kwargs.pop("expected_codes", [200])
        url = _base_ + uri
        await self._wf_s()

        logger.info('Sending "{} {}"...'.format(method, url))
        async with self.session.request(method, url, **kwargs) as response:
            if response.status in range(500, 600):
                raise TopGGServerError()
            else:
                # NOTE: This has moved from just before the return since the hits count as soon as a response
                # is generated (unless it's 5xx).
                if "/bots/" in uri:
                    routes["/bots/*"].add_hit()
                routes["*"].add_hit()

            if "application/json" not in response.headers.get("content-type", "none").lower():
                logger.warning(f"Got unexpected mime type {response.headers['Content-Type']} from top.gg.")
                raise ToppyError("Unexpected response from server.")
            if response.status in [403, 401]:
                raise Forbidden()
            if response.status == 429:
                logging.warning("Unexpected ratelimit. Re-syncing internal ratelimit handler (unless that's disabled).")
                data = await response.json()
                if not self.ignore_local_ratelimit:
                    if "/bots/" in uri:
                        routes["/bots/*"].sync_from_ratelimit(data["retry-after"])
                    routes["*"].sync_from_ratelimit(data["retry-after"])

                # NOTE: This is a bit of a whack way to deal with this.
                # There should definitely be only one way to handle a ratelimit
                # however not every user wants to handle an exception.
                # We'll keep this for now, however it will definitely change when top.gg releases v[1|2] of their
                # API.
                if fail_if_timeout:
                    raise Ratelimited(data["retry-after"])
                else:
                    logger.debug("Instructed to continue if ratelimited. Waiting and retrying...")
                    await asyncio.sleep(data.get("retry-after", 3600))
                    return await self._request(method, uri, **kwargs)
            if response.status == 404:
                raise NotFound()
            if response.status not in expected_codes:
                raise ToppyError("Unexpected status code '{}'".format(str(response.status)))

            data = await response.json()
            # inject metadata
            if isinstance(data, dict):
                data["_toppy_meta"] = {"headers": response.headers, "status": response.status}
        return data

    async def fetch_bot(self, bot_id: int, *, fail_if_ratelimited: bool = True) -> Bot:
        r"""
        Fetches a bot from top.gg

        :param bot_id: The bot's client ID
        :param fail_if_ratelimited: Whether to raise an error if we get ratelimited or just wait an hour
        :return: A :class:`toppy.models.Bot` model
        """
        response = await self._request("GET", "/bots/" + str(bot_id), fail_if_timeout=fail_if_ratelimited)
        response["state"] = self.bot
        logger.debug(f"Response from fetch_bot: {response}")
        return Bot(**response)

    async def fetch_bots(
        self, limit: int = 50, offset: int = 0, search: dict = None, sort: str = None, fail_if_ratelimited: bool = True
    ) -> BotSearchResults:
        r"""
        Fetches up to :limit: bots from top.gg

        :param limit: How many bots to fetch.
        :param offset: How many bots to "skip" (pagination)
        :param search: Search pairs (e.g. {"library": "discord.py"})
        :param sort: What field to sort by. Prefix with dash to reverse results.
        :param fail_if_ratelimited: Deprecated.
        :return: A BotSearchResults object
        """
        limit = max(2, min(500, limit))
        uri = "/bots?limit=" + str(limit)
        if search:
            search = ", ".join(f"{x}: {y}" for x, y in search.items())
            uri += "&search=" + search
        if sort:
            uri += "&sort=" + sort
        if offset:
            uri += "&offset=" + str(offset)
        result = await self._request("GET", "/bots", fail_if_timeout=fail_if_ratelimited)
        logger.debug(f"Response from fetching bots: {result}")
        new_results = []
        for bot in result["results"]:
            bot["state"] = self.bot
            new_results.append(Bot(**bot))
        return BotSearchResults(*new_results, limit=limit, offset=offset)

    async def bulk_fetch_bots(self, limit: int = 500, *args) -> dict:
        r"""Similar to fetch_bots, except allows for requesting more than 500.

        This is equivalent to:
        ```python
        batch_one = await TopGG.fetch_bots(500)
        batch_two = await TopGG.fetch_bots(500, offset=500)
        batch_three = await TopGG.fetch_bots(500, offset=1000)
        ...
        ```
        """
        results = {}
        remaining = limit
        for i in range(0, limit, 500):
            amount = min(500, remaining)
            batch_results = await self.fetch_bots(amount, offset=i, *args)
            remaining -= amount
            results = {**results, **batch_results}
        return results

    async def fetch_votes(self, *, fail_if_ratelimited: bool = True) -> List[SimpleUser]:
        r"""Fetches the last 1000 voters for your bot."""
        if not self.bot.is_ready():
            await self.bot.wait_until_ready()
        raw_users = await self._request("GET", f"/bots/{self.bot.user.id}/votes", fail_if_timeout=fail_if_ratelimited)
        resolved = list(map(lambda u: SimpleUser(**u), raw_users))
        logger.debug(f"Response from fetching votes: {resolved}")
        return resolved

    async def upvote_check(self, user_id: int, *, fail_if_ratelimited: bool = True) -> bool:
        r"""Checks to see if the provided user has voted for your bot in the pas 12 hours."""
        if not self.bot.is_ready():
            await self.bot.wait_until_ready()
        uri = f"/bots/{self.bot.user.id}/check?userId={user_id}"
        raw_users = await self._request("GET", uri, fail_if_timeout=fail_if_ratelimited)
        logger.debug(f"Response from fetching upvote check: {raw_users}")
        # Ah yes, three pieces of recycled code. How cool.
        return raw_users["voted"] == 1

    async def get_stats(self, bot_id: int, *, fail_if_ratelimited: bool = True) -> BotStats:
        r"""Fetches the server & shard count for a bot.

        NOTE: this does NOT fetch votes. Use the fetch_bot function for that."""
        uri = f"/bots/{bot_id}/stats"
        raw_stats = await self._request("GET", uri, fail_if_timeout=fail_if_ratelimited)
        logger.debug(f"Response from fetching stats: {raw_stats}")
        return BotStats(**raw_stats)

    async def post_stats(self, stats: dict = None, *, fail_if_ratelimited: bool = True) -> int:
        r"""
        Posts your bot's current statistics to top.gg

        :param stats: Use these stats instead of auto-generated ones. Not recommended.
        :param fail_if_ratelimited: Whether to raise an error if ratelimited or wait an hour
        :return: an integer of how many servers got posted.
        """
        if not self.bot.is_ready():
            await self.bot.wait_until_ready()
        override = stats is not None
        stats = stats or {}
        if not override:
            stats["server_count"] = len(self.bot.guilds)
            if hasattr(self.bot, "shards") and self.bot.shards:
                shards = []
                for shard in self.bot.shards.values():
                    shards.append(len([x for x in self.bot.guilds if x.shard_id == shard.id]))
                stats["shards"] = shards
                stats["shard_count"] = self.bot.shard_count

        response = await self._request(
            "POST", f"/bots/{self.bot.user.id}/stats", data=dumps(stats), fail_if_timeout=fail_if_ratelimited
        )
        logger.debug(f"Response from fetching posting stats: {response}")
        self.bot.dispatch("guild_post", stats)
        return stats["server_count"]

    async def is_weekend(self) -> bool:
        r"""Returns True or False, depending on if it's a "weekend".

        If it's a weekend, votes count as double."""
        data = await self._request("GET", f"/weekend")
        return data["is_weekend"]

    async def fetch_user(self, user: Union[discord.User, discord.Object]) -> User:
        """
        Fetches a user's profile from top.gg.

        :param user: Union[discord.User, discord.Member] - Who's top.gg profile to fetch.
        :raises toppy.errors.Forbidden: - Your API token was invalid.
        :raises toppy.errors.NotFound: - The user who you requested does not have a top.gg profile.
        :returns toppy.models.User: The fetched user's profile
        """
        data = await self._request("GET", f"/users/{user.id}")
        return User(**data, state=self.bot)
