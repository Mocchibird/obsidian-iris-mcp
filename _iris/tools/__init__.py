"""Tool registry — importing this package registers every @mcp.tool() with the FastMCP server.

Adding a new tool module:
    1. Create _iris/tools/<name>.py
    2. Import the shared mcp instance: ``from .. import mcp``
    3. Define your @mcp.tool() functions
    4. Add ``from . import <name>`` below
"""

# Import order matters only when modules depend on each other at import time.
# All decorators run on import, so each ``from . import X`` line registers
# every @mcp.tool() inside X.

from . import sqlite        # noqa: F401
from . import files         # noqa: F401
from . import notes         # noqa: F401
from . import search        # noqa: F401
from . import tasks         # noqa: F401
from . import calendar      # noqa: F401
from . import links         # noqa: F401
from . import analysis      # noqa: F401
from . import people        # noqa: F401
from . import anime         # noqa: F401
from . import vocab         # noqa: F401
from . import warranties    # noqa: F401
from . import import_export # noqa: F401
from . import routines      # noqa: F401
from . import web           # noqa: F401
