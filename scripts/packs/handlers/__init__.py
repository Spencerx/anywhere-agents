"""Kind handlers for the unified pack composer.

Importing this package registers every built-in handler (``skill``,
``hook``, ``permission``, ``command``) into ``scripts.packs.dispatch``'s
``KIND_HANDLERS`` registry. Composer code imports this package once at
startup so ``dispatch_active`` can route without further setup.
"""
from __future__ import annotations

from .. import dispatch

from . import command as _command  # noqa: F401
from . import hook as _hook  # noqa: F401
from . import permission as _permission  # noqa: F401
from . import skill as _skill  # noqa: F401

dispatch.register("skill", _skill.handle_skill)
dispatch.register("hook", _hook.handle_hook)
dispatch.register("permission", _permission.handle_permission)
dispatch.register("command", _command.handle_command)
