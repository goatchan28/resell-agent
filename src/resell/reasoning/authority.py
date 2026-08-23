"""Where a document came from, decided from its URL and nothing else.

`donation_scope` multiplies match strength by source authority, so this function
decides how much a retrieved page is allowed to contribute to the identification of
a physical object. That makes it the wrong thing to ask a model. The research loop
already reads authority from storage rather than from the matcher, on the grounds
that provenance is a fact about retrieval and not a judgement the matcher should be
making about its own evidence; this is the same principle one step earlier.

Allowlist only, and it fails closed. A domain nobody has classified resolves to
`UNKNOWN`, which ranks zero, which donates nothing at any match strength. The cost
is that a genuinely authoritative site contributes nothing until it is added here.
That is the right way round: the alternative is a heuristic that guesses upward and
silently widens what an unfamiliar page may donate.

Adding a domain is a deliberate act, so the table is the audit trail for it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from resell.reasoning.research import SourceAuthority

__all__ = ["AUTHORITY_BY_DOMAIN", "authority_for_url", "registered_domains"]


# Keys match a host or any of its subdomains: "beatsbydre.com" also covers
# "www.beatsbydre.com". Listed under the authority the *site* carries, which is not
# always what it feels like -- a brand's own store is `manufacturer` for identity
# facts about its own products and nothing more; it is not an authority on anybody
# else's.
AUTHORITY_BY_DOMAIN: dict[str, SourceAuthority] = {
    # --- manufacturers ------------------------------------------------------
    "apple.com": SourceAuthority.MANUFACTURER,
    "beatsbydre.com": SourceAuthority.MANUFACTURER,
    "brooksbrothers.com": SourceAuthority.MANUFACTURER,
    "bose.com": SourceAuthority.MANUFACTURER,
    "sony.com": SourceAuthority.MANUFACTURER,
    "jbl.com": SourceAuthority.MANUFACTURER,
    "sonos.com": SourceAuthority.MANUFACTURER,
    "nike.com": SourceAuthority.MANUFACTURER,
    "patagonia.com": SourceAuthority.MANUFACTURER,
    "levi.com": SourceAuthority.MANUFACTURER,
    "uniqlo.com": SourceAuthority.MANUFACTURER,
    "ralphlauren.com": SourceAuthority.MANUFACTURER,
    # --- reference works ----------------------------------------------------
    # Curated databases and registries. Good on identity, no commercial interest
    # in the answer.
    "gs1.org": SourceAuthority.REFERENCE,
    "fccid.io": SourceAuthority.REFERENCE,
    "openlibrary.org": SourceAuthority.REFERENCE,
    "discogs.com": SourceAuthority.REFERENCE,
    "wikipedia.org": SourceAuthority.REFERENCE,
    # --- resellers ----------------------------------------------------------
    # Third-party listings. They may have transcribed a code off a photograph or
    # be describing a different variant, which is exactly the case
    # `donation_scope` refuses to let donate specifics.
    "amazon.com": SourceAuthority.RESELLER,
    "walmart.com": SourceAuthority.RESELLER,
    "target.com": SourceAuthority.RESELLER,
    "bestbuy.com": SourceAuthority.RESELLER,
    "poshmark.com": SourceAuthority.RESELLER,
    "grailed.com": SourceAuthority.RESELLER,
    "therealreal.com": SourceAuthority.RESELLER,
    "mercari.com": SourceAuthority.RESELLER,
    # Deliberately absent: ebay.com and its regional domains. Their agreement
    # restricts Restricted API data from reaching a third-party AI and prohibits
    # LLM-driven scraping of the site, so no adapter fetches it and no authority is
    # asserted for it. Absence here means UNKNOWN, which donates nothing.
}


# Hosts no adapter may fetch, whatever an operator pastes and whatever a planner
# proposes. This is not a quality judgement -- it is a licensing one, and it is
# enforced at the fetcher rather than at each caller because a rule that depends on
# every future adapter remembering it is not a rule.
#
# Comp research makes this sharper than identity research did: comparable sold
# listings are eBay's core data, so the temptation to fetch is structural. The
# operator can still read a page themselves and transcribe it -- that is a person
# using a site they are entitled to use, and it is recorded as their account.
FORBIDDEN_DOMAINS: dict[str, str] = {
    "ebay.com": "eBay's agreement restricts Restricted API data from reaching a "
                "third-party AI and prohibits LLM-driven scraping of the site",
    "ebay.co.uk": "same eBay agreement",
    "ebay.de": "same eBay agreement",
    "ebay.com.au": "same eBay agreement",
    "ebay.ca": "same eBay agreement",
    "ebay.fr": "same eBay agreement",
    "ebay.it": "same eBay agreement",
    "ebay.es": "same eBay agreement",
}


def registered_domains() -> tuple[str, ...]:
    return tuple(sorted(AUTHORITY_BY_DOMAIN))


def fetch_permitted(url: str) -> tuple[bool, str]:
    """Whether any adapter may retrieve this URL at all.

    Separate from authority and asked first. Authority answers "how much is this
    page worth"; this answers "may we load it", and a no here is not overridable by
    a stronger match or a better source -- there is no combination of evidence that
    makes fetching a forbidden host acceptable.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return True, "no host to check"
    labels = host.split(".")
    for cut in range(len(labels) - 1):
        suffix = ".".join(labels[cut:])
        reason = FORBIDDEN_DOMAINS.get(suffix)
        if reason is not None:
            return False, (
                f"{suffix} must not be fetched: {reason}. Read the page yourself "
                f"and transcribe it if you need it."
            )
    return True, "not a forbidden host"


def authority_for_url(url: str) -> tuple[SourceAuthority, str]:
    """The authority for a URL, and why it got that answer.

    The reason is returned rather than logged because it is what an operator needs
    when a page they consider authoritative donates nothing: the answer is almost
    always "that host is not in the table", which is actionable, and not "the model
    disagreed with you", which is not.

    Matching walks up the host's labels, so a subdomain inherits its parent. It does
    not walk down: listing "apple.com" says nothing about "apple.com.evil.example",
    which is a different registrable domain and is exactly the shape a spoof takes.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return SourceAuthority.UNKNOWN, f"no host in {url!r}"

    labels = host.split(".")
    for cut in range(len(labels) - 1):
        suffix = ".".join(labels[cut:])
        found = AUTHORITY_BY_DOMAIN.get(suffix)
        if found is not None:
            via = "" if suffix == host else f" (via {suffix})"
            return found, f"{host} is a known {found} source{via}"

    return SourceAuthority.UNKNOWN, (
        f"{host} is not in the authority table, so it donates nothing. Add it to "
        f"AUTHORITY_BY_DOMAIN if it should carry weight."
    )
