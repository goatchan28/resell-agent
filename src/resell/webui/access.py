"""Who is asking, and what they are allowed to do.

The private beta's whole identity model, and deliberately the smallest one that
works. Cloudflare Access authenticates an email against an invited list and puts
it in a request header; this reads that header, decides whose shelf to show, and
refuses operator screens to everyone but the owner.

**What this is not.** There are no accounts, no passwords, no sessions and no
per-user eBay credentials. Everyone still publishes through one Sandbox seller
account and shares one database. `owner_email` is a label saying whose consumer
screens an item appears on -- it is not a security boundary around the data, and
/ops sees straight through it by design.

**Why the header can be trusted here, and when it stops being enough.** The app
binds to loopback, so the only route to it is the tunnel, and the only thing that
can set this header on a request that arrives is Cloudflare. That holds exactly
as long as nothing else on the machine can reach the port. It is proportionate
for five family members on Sandbox data and it is *not* proportionate for
production eBay or a wider invite list, at which point the JWT in
`Cf-Access-Jwt-Assertion` has to be verified against Cloudflare's public keys --
signature, audience and expiry -- rather than a header being taken at its word.
That is written down in `deploy/beta.md` as a precondition, not a nice-to-have.

Fails closed: no email and no configured fallback means 403, not "everyone".
"""

from __future__ import annotations

import os
from functools import wraps
from urllib.parse import urlparse

from flask import abort, g, request

# What Cloudflare Access puts on an authenticated request.
ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"

# For running the app on the machine it is developed on, where there is no
# tunnel and no header. Never set this on the beta host -- if it is set there,
# anyone reaching the port is that person.
DEV_EMAIL_ENV = "RESELL_DEV_EMAIL"

# Comma-separated. These addresses get /ops and can see every item.
ADMIN_ENV = "RESELL_ADMIN_EMAILS"

# Methods that change something. GET/HEAD/OPTIONS are exempt from the origin
# check because they are not supposed to change anything, and because a
# cross-site GET cannot be prevented this way anyway.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def admin_emails() -> frozenset[str]:
    raw = os.environ.get(ADMIN_ENV, "")
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


def email_for_request() -> str:
    """The authenticated address, or "" when there is none.

    Casefolded, because an address is not case sensitive in the part that matters
    here and two spellings of one tester must not become two shelves.
    """
    header = (request.headers.get(ACCESS_EMAIL_HEADER) or "").strip()
    if header:
        return header.casefold()
    return (os.environ.get(DEV_EMAIL_ENV, "") or "").strip().casefold()


def require_identity() -> None:
    """Every route needs to know who is asking. No email, no app.

    Refusing outright rather than falling back to a shared anonymous shelf: an
    anonymous shelf is one misconfigured tunnel away from being *the* shelf, and
    the failure would look like the product working.
    """
    g.email = email_for_request()
    g.is_admin = bool(g.email) and g.email in admin_emails()
    if not g.email:
        abort(403, "no authenticated user")


def admin_only(view):
    """Operator screens and operator actions, in the app rather than only at the edge.

    Cloudflare Access is configured to keep /ops to one address, and this does not
    trust that it was. Two independent checks, because the failure mode of the
    outer one is silent: a path rule that stops matching after a rename leaves the
    screens open with nothing to show for it.
    """

    @wraps(view)
    def guarded(*args, **kwargs):
        if not getattr(g, "is_admin", False):
            abort(403, "operator access only")
        return view(*args, **kwargs)

    return guarded


def check_origin() -> None:
    """A same-origin check on anything that changes state.

    The app has no CSRF token and no session of its own, so the thing an attacker
    would ride is the Cloudflare Access cookie: a page a tester visits elsewhere
    posts to this host, their browser attaches the Access cookie, and the request
    arrives authenticated. Comparing the declared origin to the host we were
    reached on costs nothing and stops the browser-driven version of that.

    Not a substitute for CSRF tokens, and not pretending to be. It is the
    proportionate half of the fix for a five-person beta on Sandbox data; tokens
    are on the deferred list with sessions, where they belong.
    """
    if request.method not in UNSAFE_METHODS:
        return
    stated = request.headers.get("Origin") or request.headers.get("Referer")
    if not stated:
        # A form post from a browser always sends one. Absence means a client
        # that is not a browser -- curl, a script, a test -- and those do not
        # carry anybody's Access cookie, so there is nothing to ride.
        return
    if urlparse(stated).netloc != request.host:
        abort(403, "cross-origin write refused")
