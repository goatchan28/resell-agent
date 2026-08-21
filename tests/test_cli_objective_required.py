"""`price propose` must be told which strategy it is recording.

`recommend` prints fast_sale, balanced and max_proceeds side by side with their
anchors and net proceeds. Defaulting the objective at proposal time would let a
proposal carry a strategy nobody chose -- and since the objective is inside the
content hash, that silent choice would then be what an approval binds to.
"""

from __future__ import annotations

import sqlite3

import pytest

from resell import cli_price
from resell import store_pricing as sp
from resell.pricing.strategy import SellerObjective

BASE = [
    "--condition-band", "new_with_tags",
    "--identity-resolution", "searched_not_found",
    "--category-id", "57988",
]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE item (sku TEXT PRIMARY KEY, state TEXT)")
    conn.execute("INSERT INTO item VALUES ('MP-000003','pricing')")
    sp.apply_schema(conn)
    return conn


def test_propose_refuses_without_an_objective():
    with pytest.raises(SystemExit) as exc:  # argparse exits 2 on a missing required arg
        cli_price.main(["propose", "MP-000003", *BASE], db())
    assert exc.value.code == 2


def test_the_error_names_the_missing_argument():
    with pytest.raises(SystemExit):
        cli_price.main(["propose", "MP-000003", *BASE], db())


def test_recommend_still_needs_no_objective():
    """It computes all three; asking which one to show would defeat the purpose."""
    parser = cli_price.build_parser()
    args = parser.parse_args(["recommend", "MP-000003", *BASE])
    assert not hasattr(args, "objective")


def test_every_objective_is_accepted_by_name():
    parser = cli_price.build_parser()
    for objective in SellerObjective:
        args = parser.parse_args(
            ["propose", "MP-000003", *BASE, "--objective", str(objective)]
        )
        assert args.objective == str(objective)


def test_an_unknown_objective_is_refused():
    with pytest.raises(SystemExit):
        cli_price.build_parser().parse_args(
            ["propose", "MP-000003", *BASE, "--objective", "whatever_sells"]
        )


def test_no_objective_survives_as_a_default_anywhere_in_the_parser():
    """A default reintroduced later would make the refusal above pass silently."""
    parser = cli_price.build_parser()
    for action in parser._subparsers._group_actions[0].choices["propose"]._actions:
        if action.dest == "objective":
            assert action.required is True
            assert action.default is None
            break
    else:
        raise AssertionError("propose has no --objective argument")
