"""Local operator UI. Transport only.

Flask's entire job here is HTTP in, template out. Every read goes through
`resell.views`; every write goes through the gateway or the pricing store. There
is no query, no derivation and no policy in this package, and that is a rule
rather than a preference: the CLI and the UI have to be able to disagree about
presentation and never about what is true.

The test for whether a line belongs here is whether the CLI would want it too.
If it would, it belongs in `views.py` -- and if the answer is "the CLI already
has its own version of this", something has already gone wrong.

Local by default. `serve()` binds 127.0.0.1 because this process holds a
read-write handle on the item database and an eBay refresh token sits in the same
file; there is no authentication here and none is planned.
"""

from __future__ import annotations

from resell.webui.app import create_app, serve

__all__ = ["create_app", "serve"]
