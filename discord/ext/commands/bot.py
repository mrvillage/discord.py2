"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import collections
import collections.abc
import importlib.util
import inspect
import sys
import traceback
import types
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import discord

from . import errors
from .cog import Cog
from .context import Context
from .core import Command, CommandType, GroupMixin
from .help import DefaultHelpCommand, HelpCommand
from .view import StringView

if TYPE_CHECKING:
    import importlib.machinery

    from discord.http import HTTPClient
    from discord.message import Message
    from discord.types import interactions

    from ._types import Check, CoroFunc

__all__ = (
    "when_mentioned",
    "when_mentioned_or",
    "Bot",
    "AutoShardedBot",
)

MISSING: Any = discord.utils.MISSING

T = TypeVar("T")
CFT = TypeVar("CFT", bound="CoroFunc")
CXT = TypeVar("CXT", bound="Context")


def when_mentioned(bot: Union[Bot, AutoShardedBot], msg: Message) -> List[str]:
    """A callable that implements a command prefix equivalent to being mentioned.

    These are meant to be passed into the :attr:`.Bot.command_prefix` attribute.
    """
    # bot.user will never be None when this is called
    return [f"<@{bot.user.id}> ", f"<@!{bot.user.id}> "]  # type: ignore


def when_mentioned_or(
    *prefixes: str,
) -> Callable[[Union[Bot, AutoShardedBot], Message], List[str]]:
    """A callable that implements when mentioned or other prefixes provided.

    These are meant to be passed into the :attr:`.Bot.command_prefix` attribute.

    Example
    --------

    .. code-block:: python3

        bot = commands.Bot(command_prefix=commands.when_mentioned_or('!'))


    .. note::

        This callable returns another callable, so if this is done inside a custom
        callable, you must call the returned callable, for example:

        .. code-block:: python3

            async def get_prefix(bot, message):
                extras = await prefixes_for(message.guild) # returns a list
                return commands.when_mentioned_or(*extras)(bot, message)


    See Also
    ----------
    :func:`.when_mentioned`
    """

    def inner(bot, msg):
        r = list(prefixes)
        r = when_mentioned(bot, msg) + r
        return r

    return inner


def _is_submodule(parent: str, child: str) -> bool:
    return parent == child or child.startswith(parent + ".")


def _convert_application_command_option_type(type_: type) -> int:
    if type_ is str:
        return 3
    if type_ is int:
        return 4
    if type_ is bool:
        return 5
    if type_ in {discord.User, discord.Member}:
        return 6
    if type_ in {
        discord.TextChannel,
        discord.VoiceChannel,
        discord.CategoryChannel,
        discord.Thread,
        discord.StageChannel,
    }:
        return 7
    if type_ is discord.Role:
        return 8
    if type_ is float:
        return 10
    return 3


def _convert_param(
    param: inspect.Parameter,
) -> Union[int, Tuple[int, List[Dict[str, Union[str, int, float]]]]]:
    annotation = param.annotation
    if annotation == param.empty:
        return 3
    origin = getattr(annotation, "__origin__", None)
    if origin is Union:
        if len(annotation.__args__) == 2 and any(
            getattr(i, "__origin__", None) is Literal for i in annotation.__args__
        ):
            annotation = next(
                i
                for i in annotation.__args__
                if getattr(i, "__origin__", None) is Literal
            )
            if isinstance(annotation.__args__[0], str):
                return (
                    3,
                    [{"name": str(i), "value": str(i)} for i in annotation.__args__],
                )
            if isinstance(annotation.__args__[0], int):
                return (
                    4,
                    [{"name": str(i), "value": int(i)} for i in annotation.__args__],
                )
            if isinstance(annotation.__args__[0], float):
                return (
                    10,
                    [{"name": str(i), "value": float(i)} for i in annotation.__args__],
                )
        types = [
            _convert_application_command_option_type(i)
            for i in annotation.__args__
            if i is not type(None)
        ]
        if all(i == types[0] for i in types):
            return types[0]
        if all(i in {6, 7, 8} for i in types):
            return 9
        if all(i in {4, 10} for i in types):
            return 10
        return 3
    elif origin is Literal:
        if isinstance(annotation.__args__[0], str):
            return (3, [{"name": str(i), "value": str(i)} for i in annotation.__args__])
        if isinstance(annotation.__args__[0], int):
            return (4, [{"name": str(i), "value": int(i)} for i in annotation.__args__])
        if isinstance(annotation.__args__[0], float):
            return (
                10,
                [{"name": str(i), "value": float(i)} for i in annotation.__args__],
            )
    return _convert_application_command_option_type(annotation)


def _convert_options(
    command: Union[Command, GroupMixin], group: int = 0
) -> List[interactions.ApplicationCommandOption]:
    options = []
    if isinstance(command, GroupMixin) and group != 1:
        commands = command.commands

        for com in commands:
            if len(com.type) > 1 or next(iter(com.type)) is not CommandType.default:
                updated_group = 2 if group == 0 and isinstance(com, GroupMixin) else 1
                opt = _convert_options(com, updated_group)
                if opt:
                    options.append(
                        {
                            "type": updated_group,
                            "name": com.name,  # type: ignore
                            "description": com.brief or "No description provided",  # type: ignore
                            "options": opt,
                        }
                    )
                else:
                    options.append(
                        {
                            "type": updated_group,
                            "name": com.name,  # type: ignore
                            "description": com.brief or "No description provided",  # type: ignore
                        }
                    )
        return options
    if TYPE_CHECKING:
        assert isinstance(command, Command)
    iterator = iter(command.params.items())

    if command.cog is not None:
        # we have 'self' as the first parameter so just advance
        # the iterator and resume parsing
        try:
            next(iterator)
        except StopIteration:
            raise discord.ClientException(
                f'Callback for {command.name} command is missing "self" parameter.'
            )

    # next we have the 'ctx' as the next parameter
    try:
        next(iterator)
    except StopIteration:
        raise discord.ClientException(
            f'Callback for {command.name} command is missing "ctx" parameter.'
        )
    for name, param in iterator:
        type_ = _convert_param(param)
        # this is split in duplicates for easy edit comparisons
        if param.default == param.empty:
            if isinstance(type_, tuple):  # type: ignore
                options.append(
                    {
                        "type": type_[0],  # type: ignore
                        "name": name,
                        "description": command.descriptions.get(
                            name, "No description provided"
                        ),
                        "required": True,
                        "choices": type_[1],  # type: ignore
                    }
                )
            else:
                options.append(
                    {
                        "type": type_,
                        "name": name,
                        "description": command.descriptions.get(
                            name, "No description provided"
                        ),
                        "required": True,
                    }
                )
        elif isinstance(type_, tuple):  # type: ignore
            options.append(
                {
                    "type": type_[0],  # type: ignore
                    "name": name,
                    "description": command.descriptions.get(
                        name, "No description provided"
                    ),
                    "choices": type_[1],  # type: ignore
                }
            )
        else:
            options.append(
                {
                    "type": type_,
                    "name": name,
                    "description": command.descriptions.get(
                        name, "No description provided"
                    ),
                }
            )
    return options


def _check_options(current: Dict[str, Any], new: Dict[str, Any]) -> bool:
    # sourcery skip: remove-redundant-if
    if (current and not new) or (new and not current):
        return False
    if not new and not current:
        return True
    if any(i.get("options", []) for i in new):  # type: ignore
        d = {i.get("name"): i.get("options", []) for i in current}  # type: ignore
        return all(
            _check_options(
                d.get(i.get("name"), []),  # type: ignore
                i.get("options", []),  # type: ignore
            )
            for i in new
        )
    return all(any(i.get("name") == j.get("name") and i.get("description") == j.get("description") and _check_options(j.get("options", []), i.get("options", [])) and i.get("default_permissions", True) is j.get("default_permissions", True) and _check_choices(j.get("choices", []), i.get("choices", [])) for i in new) for j in current) and all(any(i.get("name") == j.get("name") and i.get("description") == j.get("description") and _check_options(j.get("options", []), i.get("options", [])) and i.get("default_permissions", True) is j.get("default_permissions", True) and _check_choices(j.get("choices", []), i.get("choices", [])) for j in current) for i in new)  # type: ignore


def _check_choices(current: Dict[str, Any], new: Dict[str, Any]) -> bool:
    # sourcery skip: remove-redundant-if
    if (current and not new) or (new and not current):
        return False
    if not new and not current:
        return True
    return all(any(i.get("name") == j.get("name") and i.get("value") == j.get("value") for i in new) for j in current) and all(any(i.get("name") == j.get("name") and i.get("value") == j.get("value") for j in current) for i in new)  # type: ignore


class _DefaultRepr:
    def __repr__(self):
        return "<default-help-command>"


_default = _DefaultRepr()


class BotBase(GroupMixin):
    # just to make the type checker shut up for application commands
    http: HTTPClient

    def __init__(
        self, command_prefix, help_command=_default, description=None, **options
    ):
        super().__init__(**options)
        self.command_prefix = command_prefix
        self.extra_events: Dict[str, List[CoroFunc]] = {}
        self.__cogs: Dict[str, Cog] = {}
        self.__extensions: Dict[str, types.ModuleType] = {}
        self._checks: List[Check] = []
        self._check_once = []
        self._before_invoke = None
        self._after_invoke = None
        self._help_command = None
        self.description = inspect.cleandoc(description) if description else ""
        self.owner_id = options.get("owner_id")
        self.owner_ids = options.get("owner_ids", set())
        self.strip_after_prefix = options.get("strip_after_prefix", False)

        if self.owner_id and self.owner_ids:
            raise TypeError("Both owner_id and owner_ids are set.")

        if self.owner_ids and not isinstance(
            self.owner_ids, collections.abc.Collection
        ):
            raise TypeError(
                f"owner_ids must be a collection not {self.owner_ids.__class__!r}"
            )

        if help_command is _default:
            self.help_command = DefaultHelpCommand()
        else:
            self.help_command = help_command
        self.debug = options.get("debug", False)
        self.debug_guild_id = options.get("debug_guild_id")
        self.debug_command_prefix = options.get("debug_command_prefix", command_prefix)
        if self.debug:
            self.command_prefix = self.debug_command_prefix

    # internal helpers

    def dispatch(self, event_name: str, *args: Any, **kwargs: Any) -> None:
        # super() will resolve to Client
        super().dispatch(event_name, *args, **kwargs)  # type: ignore
        ev = "on_" + event_name
        for event in self.extra_events.get(ev, []):
            self._schedule_event(event, ev, *args, **kwargs)  # type: ignore

    @discord.utils.copy_doc(discord.Client.close)
    async def close(self) -> None:
        for extension in tuple(self.__extensions):
            try:
                self.unload_extension(extension)
            except Exception:
                pass

        for cog in tuple(self.__cogs):
            try:
                self.remove_cog(cog)
            except Exception:
                pass

        await super().close()  # type: ignore

    async def on_command_error(
        self, context: Context, exception: errors.CommandError
    ) -> None:
        """|coro|

        The default command error handler provided by the bot.

        By default this prints to :data:`sys.stderr` however it could be
        overridden to have a different implementation.

        This only fires if you do not specify any listeners for command error.
        """
        if self.extra_events.get("on_command_error", None):
            return

        command = context.command
        if command and command.has_error_handler():
            return

        cog = context.cog
        if cog and cog.has_error_handler():
            return

        print(f"Ignoring exception in command {context.command}:", file=sys.stderr)
        traceback.print_exception(
            type(exception), exception, exception.__traceback__, file=sys.stderr
        )

    # global check registration

    def check(self, func: T) -> T:
        r"""A decorator that adds a global check to the bot.

        A global check is similar to a :func:`.check` that is applied
        on a per command basis except it is run before any command checks
        have been verified and applies to every command the bot has.

        .. note::

            This function can either be a regular function or a coroutine.

        Similar to a command :func:`.check`\, this takes a single parameter
        of type :class:`.Context` and can only raise exceptions inherited from
        :exc:`.CommandError`.

        Example
        ---------

        .. code-block:: python3

            @bot.check
            def check_commands(ctx):
                return ctx.command.qualified_name in allowed_commands

        """
        # T was used instead of Check to ensure the type matches on return
        self.add_check(func)  # type: ignore
        return func

    def add_check(self, func: Check, *, call_once: bool = False) -> None:
        """Adds a global check to the bot.

        This is the non-decorator interface to :meth:`.check`
        and :meth:`.check_once`.

        Parameters
        -----------
        func
            The function that was used as a global check.
        call_once: :class:`bool`
            If the function should only be called once per
            :meth:`.invoke` call.
        """

        if call_once:
            self._check_once.append(func)
        else:
            self._checks.append(func)

    def remove_check(self, func: Check, *, call_once: bool = False) -> None:
        """Removes a global check from the bot.

        This function is idempotent and will not raise an exception
        if the function is not in the global checks.

        Parameters
        -----------
        func
            The function to remove from the global checks.
        call_once: :class:`bool`
            If the function was added with ``call_once=True`` in
            the :meth:`.Bot.add_check` call or using :meth:`.check_once`.
        """
        l = self._check_once if call_once else self._checks

        try:
            l.remove(func)
        except ValueError:
            pass

    def check_once(self, func: CFT) -> CFT:
        r"""A decorator that adds a "call once" global check to the bot.

        Unlike regular global checks, this one is called only once
        per :meth:`.invoke` call.

        Regular global checks are called whenever a command is called
        or :meth:`.Command.can_run` is called. This type of check
        bypasses that and ensures that it's called only once, even inside
        the default help command.

        .. note::

            When using this function the :class:`.Context` sent to a group subcommand
            may only parse the parent command and not the subcommands due to it
            being invoked once per :meth:`.Bot.invoke` call.

        .. note::

            This function can either be a regular function or a coroutine.

        Similar to a command :func:`.check`\, this takes a single parameter
        of type :class:`.Context` and can only raise exceptions inherited from
        :exc:`.CommandError`.

        Example
        ---------

        .. code-block:: python3

            @bot.check_once
            def whitelist(ctx):
                return ctx.message.author.id in my_whitelist

        """
        self.add_check(func, call_once=True)
        return func

    async def can_run(self, ctx: Context, *, call_once: bool = False) -> bool:
        if ctx.command_type not in ctx.command.type:  # type: ignore
            raise TypeError

        data = self._check_once if call_once else self._checks

        if len(data) == 0:
            return True

        # type-checker doesn't distinguish between functions and methods
        return await discord.utils.async_all(f(ctx) for f in data)  # type: ignore

    async def is_owner(self, user: discord.User) -> bool:
        """|coro|

        Checks if a :class:`~discord.User` or :class:`~discord.Member` is the owner of
        this bot.

        If an :attr:`owner_id` is not set, it is fetched automatically
        through the use of :meth:`~.Bot.application_info`.

        .. versionchanged:: 1.3
            The function also checks if the application is team-owned if
            :attr:`owner_ids` is not set.

        Parameters
        -----------
        user: :class:`.abc.User`
            The user to check for.

        Returns
        --------
        :class:`bool`
            Whether the user is the owner.
        """

        if self.owner_id:
            return user.id == self.owner_id
        elif self.owner_ids:
            return user.id in self.owner_ids
        else:

            app = await self.application_info()  # type: ignore
            if app.team:
                self.owner_ids = ids = {m.id for m in app.team.members}
                return user.id in ids
            else:
                self.owner_id = owner_id = app.owner.id
                return user.id == owner_id

    def before_invoke(self, coro: CFT) -> CFT:
        """A decorator that registers a coroutine as a pre-invoke hook.

        A pre-invoke hook is called directly before the command is
        called. This makes it a useful function to set up database
        connections or any type of set up required.

        This pre-invoke hook takes a sole parameter, a :class:`.Context`.

        .. note::

            The :meth:`~.Bot.before_invoke` and :meth:`~.Bot.after_invoke` hooks are
            only called if all checks and argument parsing procedures pass
            without error. If any check or argument parsing procedures fail
            then the hooks are not called.

        Parameters
        -----------
        coro: :ref:`coroutine <coroutine>`
            The coroutine to register as the pre-invoke hook.

        Raises
        -------
        TypeError
            The coroutine passed is not actually a coroutine.
        """
        if not asyncio.iscoroutinefunction(coro):
            raise TypeError("The pre-invoke hook must be a coroutine.")

        self._before_invoke = coro
        return coro

    def after_invoke(self, coro: CFT) -> CFT:
        r"""A decorator that registers a coroutine as a post-invoke hook.

        A post-invoke hook is called directly after the command is
        called. This makes it a useful function to clean-up database
        connections or any type of clean up required.

        This post-invoke hook takes a sole parameter, a :class:`.Context`.

        .. note::

            Similar to :meth:`~.Bot.before_invoke`\, this is not called unless
            checks and argument parsing procedures succeed. This hook is,
            however, **always** called regardless of the internal command
            callback raising an error (i.e. :exc:`.CommandInvokeError`\).
            This makes it ideal for clean-up scenarios.

        Parameters
        -----------
        coro: :ref:`coroutine <coroutine>`
            The coroutine to register as the post-invoke hook.

        Raises
        -------
        TypeError
            The coroutine passed is not actually a coroutine.
        """
        if not asyncio.iscoroutinefunction(coro):
            raise TypeError("The post-invoke hook must be a coroutine.")

        self._after_invoke = coro
        return coro

    # listener registration

    def add_listener(self, func: CoroFunc, name: str = MISSING) -> None:
        """The non decorator alternative to :meth:`.listen`.

        Parameters
        -----------
        func: :ref:`coroutine <coroutine>`
            The function to call.
        name: :class:`str`
            The name of the event to listen for. Defaults to ``func.__name__``.

        Example
        --------

        .. code-block:: python3

            async def on_ready(): pass
            async def my_message(message): pass

            bot.add_listener(on_ready)
            bot.add_listener(my_message, 'on_message')

        """
        name = func.__name__ if name is MISSING else name

        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Listeners must be coroutines")

        if name in self.extra_events:
            self.extra_events[name].append(func)
        else:
            self.extra_events[name] = [func]

    def remove_listener(self, func: CoroFunc, name: str = MISSING) -> None:
        """Removes a listener from the pool of listeners.

        Parameters
        -----------
        func
            The function that was used as a listener to remove.
        name: :class:`str`
            The name of the event we want to remove. Defaults to
            ``func.__name__``.
        """

        name = func.__name__ if name is MISSING else name

        if name in self.extra_events:
            try:
                self.extra_events[name].remove(func)
            except ValueError:
                pass

    def listen(self, name: str = MISSING) -> Callable[[CFT], CFT]:
        """A decorator that registers another function as an external
        event listener. Basically this allows you to listen to multiple
        events from different places e.g. such as :func:`.on_ready`

        The functions being listened to must be a :ref:`coroutine <coroutine>`.

        Example
        --------

        .. code-block:: python3

            @bot.listen()
            async def on_message(message):
                print('one')

            # in some other file...

            @bot.listen('on_message')
            async def my_message(message):
                print('two')

        Would print one and two in an unspecified order.

        Raises
        -------
        TypeError
            The function being listened to is not a coroutine.
        """

        def decorator(func: CFT) -> CFT:
            self.add_listener(func, name)
            return func

        return decorator

    # cogs

    def add_cog(self, cog: Cog, *, override: bool = False) -> None:
        """Adds a "cog" to the bot.

        A cog is a class that has its own event listeners and commands.

        .. versionchanged:: 2.0

            :exc:`.ClientException` is raised when a cog with the same name
            is already loaded.

        Parameters
        -----------
        cog: :class:`.Cog`
            The cog to register to the bot.
        override: :class:`bool`
            If a previously loaded cog with the same name should be ejected
            instead of raising an error.

            .. versionadded:: 2.0

        Raises
        -------
        TypeError
            The cog does not inherit from :class:`.Cog`.
        CommandError
            An error happened during loading.
        .ClientException
            A cog with the same name is already loaded.
        """

        if not isinstance(cog, Cog):
            raise TypeError("cogs must derive from Cog")

        cog_name = cog.__cog_name__
        existing = self.__cogs.get(cog_name)

        if existing is not None:
            if not override:
                raise discord.ClientException(f"Cog named {cog_name!r} already loaded")
            self.remove_cog(cog_name)

        cog = cog._inject(self)
        self.__cogs[cog_name] = cog

    def get_cog(self, name: str) -> Optional[Cog]:
        """Gets the cog instance requested.

        If the cog is not found, ``None`` is returned instead.

        Parameters
        -----------
        name: :class:`str`
            The name of the cog you are requesting.
            This is equivalent to the name passed via keyword
            argument in class creation or the class name if unspecified.

        Returns
        --------
        Optional[:class:`Cog`]
            The cog that was requested. If not found, returns ``None``.
        """
        return self.__cogs.get(name)

    def remove_cog(self, name: str) -> Optional[Cog]:
        """Removes a cog from the bot and returns it.

        All registered commands and event listeners that the
        cog has registered will be removed as well.

        If no cog is found then this method has no effect.

        Parameters
        -----------
        name: :class:`str`
            The name of the cog to remove.

        Returns
        -------
        Optional[:class:`.Cog`]
             The cog that was removed. ``None`` if not found.
        """

        cog = self.__cogs.pop(name, None)
        if cog is None:
            return

        help_command = self._help_command
        if help_command and help_command.cog is cog:
            help_command.cog = None
        cog._eject(self)

        return cog

    @property
    def cogs(self) -> Mapping[str, Cog]:
        """Mapping[:class:`str`, :class:`Cog`]: A read-only mapping of cog name to cog."""
        return types.MappingProxyType(self.__cogs)

    # extensions

    def _remove_module_references(self, name: str) -> None:
        # find all references to the module
        # remove the cogs registered from the module
        for cogname, cog in self.__cogs.copy().items():
            if _is_submodule(name, cog.__module__):
                self.remove_cog(cogname)

        # remove all the commands from the module
        for cmd in self.all_commands.copy().values():
            if cmd.module is not None and _is_submodule(name, cmd.module):
                if isinstance(cmd, GroupMixin):
                    cmd.recursively_remove_all_commands()
                self.remove_command(cmd.name)

        # remove all the listeners from the module
        for event_list in self.extra_events.copy().values():
            remove = []
            for index, event in enumerate(event_list):
                if event.__module__ is not None and _is_submodule(
                    name, event.__module__
                ):
                    remove.append(index)

            for index in reversed(remove):
                del event_list[index]

    def _call_module_finalizers(self, lib: types.ModuleType, key: str) -> None:
        try:
            func = getattr(lib, "teardown")
        except AttributeError:
            pass
        else:
            try:
                func(self)
            except Exception:
                pass
        finally:
            self.__extensions.pop(key, None)
            sys.modules.pop(key, None)
            name = lib.__name__
            for module in list(sys.modules.keys()):
                if _is_submodule(name, module):
                    del sys.modules[module]

    def _load_from_module_spec(
        self, spec: importlib.machinery.ModuleSpec, key: str
    ) -> None:
        # precondition: key not in self.__extensions
        lib = importlib.util.module_from_spec(spec)
        sys.modules[key] = lib
        try:
            spec.loader.exec_module(lib)  # type: ignore
        except Exception as e:
            del sys.modules[key]
            raise errors.ExtensionFailed(key, e) from e

        try:
            setup = getattr(lib, "setup")
        except AttributeError:
            del sys.modules[key]
            raise errors.NoEntryPointError(key)

        try:
            setup(self)
        except Exception as e:
            del sys.modules[key]
            self._remove_module_references(lib.__name__)
            self._call_module_finalizers(lib, key)
            raise errors.ExtensionFailed(key, e) from e
        else:
            self.__extensions[key] = lib

    def _resolve_name(self, name: str, package: Optional[str]) -> str:
        try:
            return importlib.util.resolve_name(name, package)
        except ImportError:
            raise errors.ExtensionNotFound(name)

    def load_extension(self, name: str, *, package: Optional[str] = None) -> None:
        """Loads an extension.

        An extension is a python module that contains commands, cogs, or
        listeners.

        An extension must have a global function, ``setup`` defined as
        the entry point on what to do when the extension is loaded. This entry
        point must have a single argument, the ``bot``.

        Parameters
        ------------
        name: :class:`str`
            The extension name to load. It must be dot separated like
            regular Python imports if accessing a sub-module. e.g.
            ``foo.test`` if you want to import ``foo/test.py``.
        package: Optional[:class:`str`]
            The package name to resolve relative imports with.
            This is required when loading an extension using a relative path, e.g ``.foo.test``.
            Defaults to ``None``.

            .. versionadded:: 1.7

        Raises
        --------
        ExtensionNotFound
            The extension could not be imported.
            This is also raised if the name of the extension could not
            be resolved using the provided ``package`` parameter.
        ExtensionAlreadyLoaded
            The extension is already loaded.
        NoEntryPointError
            The extension does not have a setup function.
        ExtensionFailed
            The extension or its setup function had an execution error.
        """

        name = self._resolve_name(name, package)
        if name in self.__extensions:
            raise errors.ExtensionAlreadyLoaded(name)

        spec = importlib.util.find_spec(name)
        if spec is None:
            raise errors.ExtensionNotFound(name)

        self._load_from_module_spec(spec, name)

    def unload_extension(self, name: str, *, package: Optional[str] = None) -> None:
        """Unloads an extension.

        When the extension is unloaded, all commands, listeners, and cogs are
        removed from the bot and the module is un-imported.

        The extension can provide an optional global function, ``teardown``,
        to do miscellaneous clean-up if necessary. This function takes a single
        parameter, the ``bot``, similar to ``setup`` from
        :meth:`~.Bot.load_extension`.

        Parameters
        ------------
        name: :class:`str`
            The extension name to unload. It must be dot separated like
            regular Python imports if accessing a sub-module. e.g.
            ``foo.test`` if you want to import ``foo/test.py``.
        package: Optional[:class:`str`]
            The package name to resolve relative imports with.
            This is required when unloading an extension using a relative path, e.g ``.foo.test``.
            Defaults to ``None``.

            .. versionadded:: 1.7

        Raises
        -------
        ExtensionNotFound
            The name of the extension could not
            be resolved using the provided ``package`` parameter.
        ExtensionNotLoaded
            The extension was not loaded.
        """

        name = self._resolve_name(name, package)
        lib = self.__extensions.get(name)
        if lib is None:
            raise errors.ExtensionNotLoaded(name)

        self._remove_module_references(lib.__name__)
        self._call_module_finalizers(lib, name)

    def reload_extension(self, name: str, *, package: Optional[str] = None) -> None:
        """Atomically reloads an extension.

        This replaces the extension with the same extension, only refreshed. This is
        equivalent to a :meth:`unload_extension` followed by a :meth:`load_extension`
        except done in an atomic way. That is, if an operation fails mid-reload then
        the bot will roll-back to the prior working state.

        Parameters
        ------------
        name: :class:`str`
            The extension name to reload. It must be dot separated like
            regular Python imports if accessing a sub-module. e.g.
            ``foo.test`` if you want to import ``foo/test.py``.
        package: Optional[:class:`str`]
            The package name to resolve relative imports with.
            This is required when reloading an extension using a relative path, e.g ``.foo.test``.
            Defaults to ``None``.

            .. versionadded:: 1.7

        Raises
        -------
        ExtensionNotLoaded
            The extension was not loaded.
        ExtensionNotFound
            The extension could not be imported.
            This is also raised if the name of the extension could not
            be resolved using the provided ``package`` parameter.
        NoEntryPointError
            The extension does not have a setup function.
        ExtensionFailed
            The extension setup function had an execution error.
        """

        name = self._resolve_name(name, package)
        lib = self.__extensions.get(name)
        if lib is None:
            raise errors.ExtensionNotLoaded(name)

        # get the previous module states from sys modules
        modules = {
            name: module
            for name, module in sys.modules.items()
            if _is_submodule(lib.__name__, name)
        }

        try:
            # Unload and then load the module...
            self._remove_module_references(lib.__name__)
            self._call_module_finalizers(lib, name)
            self.load_extension(name)
        except Exception:
            # if the load failed, the remnants should have been
            # cleaned from the load_extension function call
            # so let's load it from our old compiled library.
            lib.setup(self)  # type: ignore
            self.__extensions[name] = lib

            # revert sys.modules back to normal and raise back to caller
            sys.modules.update(modules)
            raise

    @property
    def extensions(self) -> Mapping[str, types.ModuleType]:
        """Mapping[:class:`str`, :class:`py:types.ModuleType`]: A read-only mapping of extension name to extension."""
        return types.MappingProxyType(self.__extensions)

    # help command stuff

    @property
    def help_command(self) -> Optional[HelpCommand]:
        return self._help_command

    @help_command.setter
    def help_command(self, value: Optional[HelpCommand]) -> None:
        if value is not None:
            if not isinstance(value, HelpCommand):
                raise TypeError("help_command must be a subclass of HelpCommand")
            if self._help_command is not None:
                self._help_command._remove_from_bot(self)
            self._help_command = value
            value._add_to_bot(self)
        elif self._help_command is not None:
            self._help_command._remove_from_bot(self)
            self._help_command = None
        else:
            self._help_command = None

    # command processing

    async def get_prefix(self, message: Message) -> Union[List[str], str]:
        """|coro|

        Retrieves the prefix the bot is listening to
        with the message as a context.

        Parameters
        -----------
        message: :class:`discord.Message`
            The message context to get the prefix of.

        Returns
        --------
        Union[List[:class:`str`], :class:`str`]
            A list of prefixes or a single prefix that the bot is
            listening for.
        """
        prefix = ret = self.command_prefix
        if callable(prefix):
            ret = await discord.utils.maybe_coroutine(prefix, self, message)

        if not isinstance(ret, str):
            try:
                ret = list(ret)
            except TypeError:
                # It's possible that a generator raised this exception.  Don't
                # replace it with our own error if that's the case.
                if isinstance(ret, collections.abc.Iterable):
                    raise

                raise TypeError(
                    "command_prefix must be plain string, iterable of strings, or callable "
                    f"returning either of these, not {ret.__class__.__name__}"
                )

            if not ret:
                raise ValueError(
                    "Iterable command_prefix must contain at least one prefix"
                )

        return ret

    async def get_context(self, message: Message, *, cls: Type[CXT] = Context) -> CXT:
        r"""|coro|

        Returns the invocation context from the message.

        This is a more low-level counter-part for :meth:`.process_commands`
        to allow users more fine grained control over the processing.

        The returned context is not guaranteed to be a valid invocation
        context, :attr:`.Context.valid` must be checked to make sure it is.
        If the context is not valid then it is not a valid candidate to be
        invoked under :meth:`~.Bot.invoke`.

        Parameters
        -----------
        message: :class:`discord.Message`
            The message to get the invocation context from.
        cls
            The factory class that will be used to create the context.
            By default, this is :class:`.Context`. Should a custom
            class be provided, it must be similar enough to :class:`.Context`\'s
            interface.

        Returns
        --------
        :class:`.Context`
            The invocation context. The type of this can change via the
            ``cls`` parameter.
        """

        view = StringView(message.content)
        ctx = cls(prefix=None, view=view, bot=self, message=message)

        if message.author.id == self.user.id:  # type: ignore
            return ctx

        prefix = await self.get_prefix(message)
        invoked_prefix = prefix

        if isinstance(prefix, str):
            if not view.skip_string(prefix):
                return ctx
        else:
            try:
                # if the context class' __init__ consumes something from the view this
                # will be wrong.  That seems unreasonable though.
                if message.content.startswith(tuple(prefix)):
                    invoked_prefix = discord.utils.find(view.skip_string, prefix)
                else:
                    return ctx

            except TypeError:
                if not isinstance(prefix, list):
                    raise TypeError(
                        "get_prefix must return either a string or a list of string, "
                        f"not {prefix.__class__.__name__}"
                    )

                # It's possible a bad command_prefix got us here.
                for value in prefix:
                    if not isinstance(value, str):
                        raise TypeError(
                            "Iterable command_prefix or list returned from get_prefix must "
                            f"contain only strings, not {value.__class__.__name__}"
                        )

                # Getting here shouldn't happen
                raise

        if self.strip_after_prefix:
            view.skip_ws()

        invoker = view.get_word()
        ctx.invoked_with = invoker
        # type-checker fails to narrow invoked_prefix type.
        ctx.prefix = invoked_prefix  # type: ignore
        ctx.command = self.all_commands.get(invoker)
        return ctx

    async def get_interaction_context(
        self,
        interaction: discord.Interaction,
        *,
        cls: Type[Context] = Context,
    ) -> Context:
        invoked_with = interaction.data["name"]  # type: ignore
        command = self.all_commands[invoked_with]
        self.get_command
        invoked_parents = []
        if "options" in interaction.data:  # type: ignore
            data = interaction.data["options"][0]  # type: ignore
            try:
                while data["options"][0]["type"] in {1, 2}:  # type: ignore
                    invoked_parents.append(invoked_with)
                    invoked_with = data["name"]  # type: ignore
                    command = command.all_commands[invoked_with]  # type: ignore
                    data = data["options"][0]  # type: ignore
            except (KeyError, IndexError):
                pass
            if isinstance(command, GroupMixin):
                invoked_parents.append(invoked_with)
                invoked_with = data["name"]  # type: ignore
                invoked_parents.append(invoked_with)
                command = command.all_commands[invoked_with]
                options = data.get("options", [])
            else:
                options = interaction.data.get("options", [])  # type: ignore
        else:
            options = []
        return cls(
            interaction=interaction,
            command=command,
            prefix="/",
            invoked_with=invoked_with,  # type: ignore
            invoked_parents=invoked_parents,
            bot=self,
            options=options,  # type: ignore
        )

    async def invoke(self, ctx: Context) -> None:
        """|coro|

        Invokes the command given under the invocation context and
        handles all the internal event dispatch mechanisms.

        Parameters
        -----------
        ctx: :class:`.Context`
            The invocation context to invoke.
        """
        # dispatching the command event is duplicated three times to ensure
        # that it's not dispatched if the command is not of a valid type
        if ctx.command is not None:
            try:
                if await self.can_run(ctx, call_once=True):
                    self.dispatch("command", ctx)
                    await ctx.command.invoke(ctx)
                else:
                    self.dispatch("command", ctx)
                    raise errors.CheckFailure("The global check once functions failed.")
            except errors.CommandError as exc:
                self.dispatch("command", ctx)
                await ctx.command.dispatch_error(ctx, exc)
            except TypeError:
                pass
            else:
                self.dispatch("command_completion", ctx)
        elif ctx.invoked_with:
            exc = errors.CommandNotFound(f'Command "{ctx.invoked_with}" is not found')
            self.dispatch("command_error", ctx, exc)

    async def process_commands(
        self, argument: Union[discord.Message, discord.Interaction]
    ) -> None:
        """|coro|

        This function processes the commands that have been registered
        to the bot and other groups. Without this coroutine, none of the
        commands will be triggered.

        By default, this coroutine is called inside the :func:`.on_message`
        event. If you choose to override the :func:`.on_message` event, then
        you should invoke this coroutine as well.

        This is built using other low level tools, and is equivalent to a
        call to :meth:`~.Bot.get_context` followed by a call to :meth:`~.Bot.invoke`.

        This also checks if the message's author is a bot and doesn't
        call :meth:`~.Bot.get_context` or :meth:`~.Bot.invoke` if so.

        Parameters
        -----------
        message: :class:`discord.Message`
            The message to process commands for.
        """
        if isinstance(argument, discord.Message):
            if argument.author.bot:
                return
            ctx = await self.get_context(argument)
        elif argument.type != discord.InteractionType.application_command:
            return
        else:
            ctx = await self.get_interaction_context(argument)

        await self.invoke(ctx)

    async def on_message(self, message: discord.Message):
        await self.process_commands(message)

    async def on_interaction(self, interaction: discord.Interaction):
        await self.process_commands(interaction)

    async def on_ready(self):
        await self.register_application_commands()

    async def register_application_commands(self) -> None:
        command_data = []
        for command in self.commands:
            if (
                len(command.type) > 1
                or next(iter(command.type)) is not CommandType.default
            ):
                options = _convert_options(command)
                for type_ in command.type:
                    if type_ is CommandType.default:
                        continue
                    if type_ is CommandType.chat_input:
                        command_data.append(
                            {
                                "name": command.name,
                                "description": command.brief
                                or "No description provided",
                                "options": options,
                                "type": type_.value,
                            }
                        )
                    elif type_ is CommandType.user or type_ is CommandType.message:
                        command_data.append({"name": command.name, "type": type_.value})
                    else:
                        continue
        if self.debug:
            await self.http.bulk_upsert_guild_commands(
                self.application_id, self.debug_guild_id, command_data
            )
        else:
            await self.http.bulk_upsert_global_commands(
                self.application_id, command_data
            )
        # if self.debug:
        #     raw_application_commands = await self.http.get_guild_commands(self.application_id, 654109011473596417)  # type: ignore
        # else:
        #     raw_application_commands = await self.http.get_global_commands(self.application_id)  # type: ignore
        # application_commands = {i["name"]: i for i in raw_application_commands}
        # application_id = self.application_id
        # for command in self.commands:
        #     if (
        #         len(command.type) > 1
        #         or next(iter(command.type)) is not CommandType.default
        #     ):
        #         options = _convert_options(command)
        #         for type_ in command.type:
        #             if type_ is CommandType.default:
        #                 continue
        #             if type_ is CommandType.chat_input:
        #                 data = {
        #                     "name": command.name,
        #                     "description": command.brief or "\u200b",
        #                     "options": options,
        #                     "type": type_.value,
        #                 }
        #             elif type_ is CommandType.user or type_ is CommandType.message:
        #                 data = {"name": command.name, "type": type_.value}
        #             else:
        #                 continue
        #             application_command = application_commands.get(command.name)
        #             if self.debug:
        #                 if application_command is None:
        #                     await self.http.upsert_guild_command(application_id, self.debug_guild_id, data)  # type: ignore
        #                 else:
        #                     if application_command["description"] == data[
        #                         "description"
        #                     ] and _check_options(
        #                         application_command.get("options", []),  # type: ignore
        #                         data.get("options", []),  # type: ignore
        #                     ):
        #                         continue
        #                     data.pop("type")
        #                     await self.http.edit_guild_command(application_id, self.debug_guild_id, application_command["id"], data)  # type: ignore
        #             elif application_command is None:
        #                 await self.http.upsert_global_command(application_id, data)  # type: ignore
        #             else:
        #                 if application_command["description"] == data[
        #                     "description"
        #                 ] and _check_options(
        #                     application_command.get("options", []), data.get("options", [])  # type: ignore
        #                 ):
        #                     continue
        #                 data.pop("type")
        #                 await self.http.edit_global_command(application_id, application_command["id"], data)  # type: ignore


class Bot(BotBase, discord.Client):
    """Represents a discord bot.

    This class is a subclass of :class:`discord.Client` and as a result
    anything that you can do with a :class:`discord.Client` you can do with
    this bot.

    This class also subclasses :class:`.GroupMixin` to provide the functionality
    to manage commands.

    Attributes
    -----------
    command_prefix
        The command prefix is what the message content must contain initially
        to have a command invoked. This prefix could either be a string to
        indicate what the prefix should be, or a callable that takes in the bot
        as its first parameter and :class:`discord.Message` as its second
        parameter and returns the prefix. This is to facilitate "dynamic"
        command prefixes. This callable can be either a regular function or
        a coroutine.

        An empty string as the prefix always matches, enabling prefix-less
        command invocation. While this may be useful in DMs it should be avoided
        in servers, as it's likely to cause performance issues and unintended
        command invocations.

        The command prefix could also be an iterable of strings indicating that
        multiple checks for the prefix should be used and the first one to
        match will be the invocation prefix. You can get this prefix via
        :attr:`.Context.prefix`. To avoid confusion empty iterables are not
        allowed.

        .. note::

            When passing multiple prefixes be careful to not pass a prefix
            that matches a longer prefix occurring later in the sequence.  For
            example, if the command prefix is ``('!', '!?')``  the ``'!?'``
            prefix will never be matched to any message as the previous one
            matches messages starting with ``!?``. This is especially important
            when passing an empty string, it should always be last as no prefix
            after it will be matched.
    case_insensitive: :class:`bool`
        Whether the commands should be case insensitive. Defaults to ``False``. This
        attribute does not carry over to groups. You must set it to every group if
        you require group commands to be case insensitive as well.
    description: :class:`str`
        The content prefixed into the default help message.
    help_command: Optional[:class:`.HelpCommand`]
        The help command implementation to use. This can be dynamically
        set at runtime. To remove the help command pass ``None``. For more
        information on implementing a help command, see :ref:`ext_commands_help_command`.
    owner_id: Optional[:class:`int`]
        The user ID that owns the bot. If this is not set and is then queried via
        :meth:`.is_owner` then it is fetched automatically using
        :meth:`~.Bot.application_info`.
    owner_ids: Optional[Collection[:class:`int`]]
        The user IDs that owns the bot. This is similar to :attr:`owner_id`.
        If this is not set and the application is team based, then it is
        fetched automatically using :meth:`~.Bot.application_info`.
        For performance reasons it is recommended to use a :class:`set`
        for the collection. You cannot set both ``owner_id`` and ``owner_ids``.

        .. versionadded:: 1.3
    strip_after_prefix: :class:`bool`
        Whether to strip whitespace characters after encountering the command
        prefix. This allows for ``!   hello`` and ``!hello`` to both work if
        the ``command_prefix`` is set to ``!``. Defaults to ``False``.

        .. versionadded:: 1.7
    """

    pass


class AutoShardedBot(BotBase, discord.AutoShardedClient):
    """This is similar to :class:`.Bot` except that it is inherited from
    :class:`discord.AutoShardedClient` instead.
    """

    pass
