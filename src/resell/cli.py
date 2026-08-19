"""Command line interface.

    resell auth login      run the browser consent flow and store tokens
    resell auth status     show what credentials are stored and how long they last
    resell auth refresh    force an access token refresh
    resell auth logout     delete stored tokens for this environment
    resell smoke           call eBay with both token kinds to prove auth works
    resell events          tail the event log

argparse rather than a CLI framework: this is stdlib, and the dependency list for
a personal tool is worth keeping short.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from resell import db
from resell.config import ConfigError, load_config
from resell.ebay import oauth
from resell.ebay.store import TokenStore, describe

STATE_KEY = "oauth.pending_state"


def _open_db_and_config(*, require_credentials: bool = True):
    config = load_config(require_credentials=require_credentials)
    conn = db.connect(config.db_path)
    return config, conn


# --- commands ----------------------------------------------------------------


def cmd_auth_login(args: argparse.Namespace) -> int:
    config, conn = _open_db_and_config()
    from resell.ebay.tokens import UserTokenProvider

    state = oauth.new_state()
    db.kv_set(conn, STATE_KEY, state)
    url = oauth.build_consent_url(config, state, force_login=args.force_login)

    print(f"\nEnvironment: {config.env.name}")
    print(f"Scopes requested ({len(config.scopes)}):")
    for scope in config.scopes:
        print(f"  - {scope}")
    print("\nOpen this URL and grant access:\n")
    print(url)
    print()

    if not args.no_browser:
        webbrowser.open(url)

    print(
        "After you click 'Agree and Continue', the browser will land on your RuName's\n"
        "Auth Accepted URL. That page may well fail to load -- it does not matter.\n"
        "What matters is the address bar: copy the ENTIRE URL and paste it below.\n"
        "\nThe code expires in about 5 minutes, so do this promptly.\n"
    )
    redirect = input("Paste the full redirect URL: ").strip()
    if not redirect:
        print("Nothing pasted; aborted.", file=sys.stderr)
        return 1

    try:
        code = oauth.parse_redirect(redirect, expected_state=state)
    except oauth.OAuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    db.kv_delete(conn, STATE_KEY)

    provider = UserTokenProvider(config, TokenStore(conn, config.env.name), conn)
    try:
        bundle = provider.exchange_code(code)
    except oauth.OAuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print("\nUser token stored.")
    print(json.dumps(describe(bundle), indent=2, default=str))
    print("\nNext: resell smoke")
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    config, conn = _open_db_and_config(require_credentials=False)
    store = TokenStore(conn, config.env.name)
    print(
        json.dumps(
            {
                "environment": config.env.name,
                "marketplace": config.marketplace_id,
                "database": str(config.db_path),
                "runame_configured": bool(config.runame),
                "user": describe(store.load("user")),
                "application": describe(store.load("application")),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_auth_refresh(args: argparse.Namespace) -> int:
    config, conn = _open_db_and_config()
    from resell.ebay.tokens import UserTokenProvider

    provider = UserTokenProvider(config, TokenStore(conn, config.env.name), conn)
    try:
        bundle = provider.refresh()
    except oauth.OAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(describe(bundle), indent=2, default=str))
    return 0


def cmd_auth_logout(args: argparse.Namespace) -> int:
    config, conn = _open_db_and_config(require_credentials=False)
    store = TokenStore(conn, config.env.name)
    for kind in ("user", "application"):
        store.delete(kind)
    db.log_event(conn, "oauth.local_tokens_deleted", {"environment": config.env.name})
    print(
        f"Deleted locally stored tokens for {config.env.name}.\n"
        "Note: this does not revoke anything at eBay. To revoke, use the eBay "
        "account's third-party app access page."
    )
    return 0


def _classify(exc: Exception) -> tuple[str, str]:
    """Map a failure onto (verdict, explanation).

    Three outcomes worth telling apart, because each needs a different response: a
    broken credential is ours to fix, unprovisioned account state is a one-time
    setup step, and a sandbox outage is somebody else's problem entirely. Reporting
    all three as "FAILED" and guessing at scopes sends you hunting in the wrong
    place.
    """
    from resell.ebay.client import EbayApiError

    if isinstance(exc, oauth.NeedsConsent):
        return "AUTH", "No usable token. Run: resell auth login"
    if not isinstance(exc, EbayApiError):
        return "AUTH", str(exc)

    if exc.status_code == 401:
        return "AUTH", "Token rejected. Run: resell auth login"
    if exc.status_code == 403:
        return (
            "SCOPE",
            "Authorized but forbidden -- the scope was probably never granted to this "
            "keyset. Check the OAuth Scopes link on your Application Keys page.",
        )
    if 20403 in exc.error_ids:
        return (
            "SETUP",
            "The seller is not opted in to business policies. Run: resell account optin",
        )
    if exc.status_code >= 500 or 25001 in exc.error_ids:
        return (
            "SANDBOX",
            "eBay-side system error, not a credential problem. Error 25001 against "
            "sandbox inventory endpoints is a well-known intermittent outage. Already "
            "retried; try again later.",
        )
    return "ERROR", "Unexpected failure -- see the error detail above."


def cmd_smoke(args: argparse.Namespace) -> int:
    """Prove the credential paths work, and name the cause when they do not."""
    config, conn = _open_db_and_config()
    from resell.ebay.client import EbayApiError, EbayClient

    results: list[tuple[str, str, str]] = []
    with EbayClient(config, conn) as client:
        checks = (
            (
                "user",
                "sell.account",
                "getPrivileges",
                client.get_privileges,
                lambda r: f"registered: {(r or {}).get('sellerRegistrationCompleted')}",
            ),
            (
                "user",
                "sell.inventory",
                "getInventoryLocations",
                client.get_inventory_locations,
                lambda r: f"locations: {(r or {}).get('total', 0)}",
            ),
            (
                "app",
                "api_scope",
                "getDefaultCategoryTreeId",
                client.get_default_category_tree_id,
                lambda r: f"categoryTreeId: {(r or {}).get('categoryTreeId')}",
            ),
        )
        for token_kind, scope, name, call, summarise in checks:
            print(
                f"[{config.env.name}] {token_kind:<4} token  {scope:<15} {name} ... ",
                end="",
                flush=True,
            )
            try:
                print(f"ok ({summarise(call())})")
                results.append(("OK", name, ""))
            except (EbayApiError, oauth.OAuthError) as exc:
                verdict, explanation = _classify(exc)
                print(verdict)
                for line in str(exc).splitlines():
                    print(f"    {line}", file=sys.stderr)
                results.append((verdict, name, explanation))

    verdicts = {verdict for verdict, _, _ in results}
    print()
    for verdict, name, explanation in results:
        if verdict != "OK":
            print(f"{verdict}  {name}: {explanation}")

    if verdicts & {"AUTH", "SCOPE", "ERROR"}:
        print(
            "\nAuthentication itself is not working. Fix the above before continuing.",
            file=sys.stderr,
        )
        return 1
    if verdicts - {"OK"}:
        print(
            "\nAuthentication IS working -- every failure above is account state or an "
            "eBay-side outage, not a credential problem."
        )
        return 0
    print("Auth is working: both token kinds mint, persist, and authorize real calls.")
    return 0


def cmd_account_optin(args: argparse.Namespace) -> int:
    """Opt the seller into business policies, which publishOffer depends on."""
    config, conn = _open_db_and_config()
    from resell.ebay.client import EbayApiError, EbayClient

    program = "SELLING_POLICY_MANAGEMENT"
    with EbayClient(config, conn) as client:
        try:
            status, _, _ = client.opt_in_to_program(program)
            print(f"opt_in {program} -> HTTP {status}")
        except EbayApiError as exc:
            print(f"opt-in failed:\n{exc}", file=sys.stderr)
            if exc.status_code >= 500:
                print(
                    "\nA 500 here is a known sandbox condition. Retry in a few minutes; "
                    "this call is deliberately not auto-retried because it mutates "
                    "account state.",
                    file=sys.stderr,
                )
            return 1

        try:
            programs = client.get_opted_in_programs()
            enrolled = [
                p.get("programType") for p in (programs or {}).get("programs", [])
            ]
            print(f"opted-in programs: {enrolled or '(none reported)'}")
            if program not in enrolled:
                print(
                    "\nNot listed yet -- enrollment can lag. Re-run this command shortly.",
                    file=sys.stderr,
                )
                return 1
        except EbayApiError as exc:
            print(f"could not confirm enrollment:\n{exc}", file=sys.stderr)
            return 1

    print("\nBusiness policies enabled. Next: resell smoke")
    return 0


def cmd_account_provision(args: argparse.Namespace) -> int:
    """Ensure the three business policies and one inventory location exist."""
    config, conn = _open_db_and_config()
    from resell.ebay.client import EbayClient
    from resell.ebay.provision import provision

    with EbayClient(config, conn) as client:
        steps = provision(client)

    print(f"\nProvisioning {config.env.name} / {config.marketplace_id}\n")
    width = max(len(s.name) for s in steps)
    for step in steps:
        mark = {"exists": "=", "created": "+", "failed": "x"}[step.status]
        line = f"  {mark} {step.name:<{width}}  {step.status:<8}"
        if step.identifier:
            line += f" {step.identifier}"
        print(line)
        if step.detail and step.status != "failed":
            print(f"      {step.detail}")
        elif step.status == "failed":
            for detail_line in step.detail.splitlines():
                print(f"      {detail_line}")

    failed = [s for s in steps if s.status == "failed"]
    if failed:
        print(f"\n{len(failed)} step(s) failed. Publishing is blocked until they pass.")
        return 1
    print("\nAll publish preconditions satisfied. IDs saved for the publish flow.")
    return 0


def cmd_images_check(args: argparse.Namespace) -> int:
    """Validate photos locally. No network, no credentials, no API calls spent."""
    from resell.images import inspect_all

    results, set_errors = inspect_all(args.paths)
    for facts in results:
        mark = "ok " if facts.ok else "BAD"
        size = f"{facts.size_bytes / 1024:.0f} KB"
        print(f"  {mark} {facts.path.name:<28} {str(facts.image_format or '?'):<5} "
              f"{facts.dimensions:<12} {size:>9}")
        for error in facts.errors:
            print(f"      error:   {error}")
        for warning in facts.warnings:
            print(f"      warning: {warning}")
    for error in set_errors:
        print(f"  BAD set: {error}")

    bad = [f for f in results if not f.ok]
    if bad or set_errors:
        print(f"\n{len(bad)} file(s) and {len(set_errors)} set-level problem(s).")
        return 1
    warnings = sum(len(f.warnings) for f in results)
    print(f"\n{len(results)} image(s) valid" + (f", {warnings} warning(s)" if warnings else ""))
    return 0


def cmd_images_upload(args: argparse.Namespace) -> int:
    """Validate, then upload to eBay Picture Services."""
    config, conn = _open_db_and_config()
    from resell.ebay.client import EbayClient
    from resell.ebay.media import EbayMediaUploader, upload_all

    with EbayClient(config, conn) as client:
        uploader = EbayMediaUploader(client, conn, convert=not args.no_convert)
        results = upload_all(uploader, args.paths)

    failures = 0
    for path, outcome in results:
        if isinstance(outcome, Exception):
            failures += 1
            print(f"  FAILED {path.name}")
            for line in str(outcome).splitlines():
                print(f"      {line}", file=sys.stderr)
            continue
        tag = "reused" if outcome.reused else "uploaded"
        print(f"  {tag:<8} {path.name:<28} {outcome.image_id}")
        if outcome.converted_from:
            print(f"      converted: {outcome.converted_from}")
        if outcome.eps_url:
            print(f"      {outcome.eps_url}")
        if outcome.expires_at:
            note = {
                "ebay": "per eBay",
                "assumed": "ASSUMED - eBay returned no date",
                "stored": "recorded at upload",
            }.get(outcome.expiry_source, outcome.expiry_source)
            print(f"      expires {outcome.expires_at.date().isoformat()}  ({note})")
        if not outcome.usable:
            print("      warning: no EPS URL resolved; not usable in a listing yet")

    if failures:
        print(f"\n{failures} of {len(results)} failed.", file=sys.stderr)
        return 1
    print(f"\n{len(results)} image(s) hosted by eBay.")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    config, conn = _open_db_and_config(require_credentials=False)
    for row in reversed(db.recent_events(conn, args.limit)):
        print(f"{row['ts']}  {row['kind']:<32} {row['payload']}")
    return 0


# --- wiring ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resell",
        description="Personal reselling agent: photos + cost -> researched eBay listing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "typical first run:\n"
            "  resell auth login     # browser consent, stores user + refresh token\n"
            "  resell smoke          # verify both token kinds authorize real calls\n"
            "  resell auth status    # expiries and scopes, no secrets printed\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="eBay OAuth management")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    login = auth_sub.add_parser("login", help="run the consent flow")
    login.add_argument(
        "--no-browser", action="store_true", help="print the URL without opening it"
    )
    login.add_argument(
        "--force-login",
        action="store_true",
        help="add prompt=login, to switch sandbox test users",
    )
    login.set_defaults(func=cmd_auth_login)

    auth_sub.add_parser("status", help="show stored credentials").set_defaults(
        func=cmd_auth_status
    )
    auth_sub.add_parser("refresh", help="force an access token refresh").set_defaults(
        func=cmd_auth_refresh
    )
    auth_sub.add_parser("logout", help="delete locally stored tokens").set_defaults(
        func=cmd_auth_logout
    )

    account = subparsers.add_parser("account", help="one-time seller account setup")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    account_sub.add_parser(
        "optin", help="opt the seller into business policies"
    ).set_defaults(func=cmd_account_optin)
    account_sub.add_parser(
        "provision", help="ensure business policies and inventory location exist"
    ).set_defaults(func=cmd_account_provision)

    images = subparsers.add_parser("images", help="listing photo handling")
    images_sub = images.add_subparsers(dest="images_command", required=True)
    check = images_sub.add_parser("check", help="validate photos locally, offline")
    check.add_argument("paths", nargs="+")
    check.set_defaults(func=cmd_images_check)
    upload = images_sub.add_parser("upload", help="upload photos to eBay Picture Services")
    upload.add_argument("paths", nargs="+")
    upload.add_argument(
        "--no-convert",
        action="store_true",
        help="send original bytes even for formats EPS rejects (diagnostic only)",
    )
    upload.set_defaults(func=cmd_images_upload)

    subparsers.add_parser("smoke", help="verify auth against eBay").set_defaults(
        func=cmd_smoke
    )

    events = subparsers.add_parser("events", help="tail the event log")
    events.add_argument("-n", "--limit", type=int, default=20)
    events.set_defaults(func=cmd_events)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
