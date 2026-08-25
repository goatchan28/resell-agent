"""Test-wide guards.

The one that matters: **no test may call a model provider for real.**

This is not hypothetical. The UI's upload handler runs the orchestrator, the
orchestrator runs the observation stage, and the stage builds its adapter from
`ANTHROPIC_API_KEY` -- which `load_config` loads out of `.env` when pytest starts.
Injecting a credential-free `Config` into the Flask app does not touch that, so
four UI tests were quietly making paid API calls and taking five seconds each.
Nothing failed; the bill would simply have arrived later.

So the registry is replaced for the whole session with an adapter that raises. A
test that genuinely wants a model supplies its own through the `adapter=` argument
every stage already accepts, or patches `ADAPTERS` itself with `monkeypatch.setitem`,
which shadows this for that test only.
"""

from __future__ import annotations

import pytest


class NoNetworkAdapter:
    """Stands in for every provider. Fails loudly rather than costing money."""

    provider = "blocked-in-tests"
    model = "blocked-in-tests"

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def _refuse(self, *_args, **_kwargs):
        raise RuntimeError(
            "a test tried to call a real model provider. Pass an adapter into the "
            "stage, or patch resell.reasoning.adapters.ADAPTERS for this test."
        )

    run = _refuse
    rates = _refuse
    estimate_input_tokens = _refuse


@pytest.fixture(autouse=True, scope="session")
def _block_real_model_calls():
    """Session-wide, autouse, and deliberately not overridable by accident."""
    from resell.reasoning import adapters

    original = dict(adapters.ADAPTERS)
    adapters.ADAPTERS.clear()
    adapters.ADAPTERS.update({name: NoNetworkAdapter for name in original})
    adapters.ADAPTERS["anthropic"] = NoNetworkAdapter
    yield
    adapters.ADAPTERS.clear()
    adapters.ADAPTERS.update(original)


@pytest.fixture(autouse=True)
def _block_real_http(monkeypatch):
    """The other half: no outbound HTTP from a test, whoever initiates it.

    `httpx.post` and `httpx.Client.send` cover the eBay client and the page
    fetcher. Tests that need either inject a fake transport or a fake client,
    which both already accept, so nothing legitimate goes through here.
    """
    import httpx

    def refuse(*_args, **_kwargs):
        raise RuntimeError(
            "a test tried to make a real HTTP request. Inject a transport or a "
            "client instead."
        )

    monkeypatch.setattr(httpx, "post", refuse, raising=False)
    monkeypatch.setattr(httpx, "get", refuse, raising=False)
    monkeypatch.setattr(httpx.Client, "send", refuse, raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_search_config(monkeypatch):
    """Tests must not inherit the developer's search configuration.

    `.env` is loaded into `os.environ` by `load_config`, so the moment any test
    touches config the whole suite sees whatever backend the machine has set up.
    That is not a test-only annoyance: it made the suite's result depend on a file
    that is not in the repository, and it passed locally for whoever had no key
    and failed for whoever did.

    The same reasoning as the network and model-adapter guards above. A test that
    wants a backend says so with `monkeypatch.setenv`.
    """
    for name in ("RESELL_SEARCH_BACKEND", "BRAVE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# --- who the web tests are ------------------------------------------------------------
#
# Every route now needs an authenticated address (see `webui/access.py`), which
# in a real deployment arrives in a Cloudflare Access header and here comes from
# `RESELL_DEV_EMAIL`. Without it every request is a 403 and the suite tests the
# identity gate eighty times over instead of what it meant to test.
#
# The address is also an admin, so tests that create items directly through the
# gateway -- with no owner, because the gateway is not the consumer route -- can
# still read them back. Ownership *scoping* is not tested by that arrangement and
# is not meant to be: `test_beta_access.py` sets its own addresses and drives the
# real routes to check who can see what.

TEST_EMAIL = "operator@example.test"


@pytest.fixture(autouse=True)
def _authenticated(monkeypatch):
    monkeypatch.setenv("RESELL_DEV_EMAIL", TEST_EMAIL)
    monkeypatch.setenv("RESELL_ADMIN_EMAILS", TEST_EMAIL)
