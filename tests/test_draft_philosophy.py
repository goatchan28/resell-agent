"""What a listing may say, and what stops it saying it.

The rule has not changed: a listing may only assert what the evidence supports.
What changed is the reading of "assert". A regulated word is regulated because a
buyer can rely on it -- "vintage" claims an age, "mint" claims a condition -- and
the same letters inside a name the record already holds claim nothing at all.

MP-000018 is the case that forced it. A paperback whose *publisher* is Vintage
Contemporaries had its draft refused, its repair refused, and the run died with an
empty title: the operator was asked to write a listing from scratch over an
imprint's name.
"""

from __future__ import annotations

import pytest

from resell.reasoning.listing import ListingDraft, review_draft


def review(title, description, record, *, support=frozenset({"condition"}),
           marketing=""):
    return review_draft(
        ListingDraft(title=title, description=description, marketing_copy=marketing),
        supported_text=record, valid_evidence_ids={1}, available_support=support,
    )


# --- metadata and proper nouns from the record -------------------------------------


BOOK = "Author Sandra Cisneros Publisher Vintage Contemporaries Format Paperback"


def test_the_vintage_contemporaries_case():
    """The regression example. It is publisher metadata, not an age claim."""
    r = review(
        "The House on Mango Street - Sandra Cisneros - Vintage Contemporaries",
        "Published by Vintage Contemporaries in paperback.", BOOK,
    )
    assert r.ok, r.problems


@pytest.mark.parametrize("record,title,description", [
    ("Colour Mint Green Brand Nike Size 10",
     "Nike Trainers - Mint Green - Size 10", "A pair in Mint Green."),
    ("Material Genuine Leather Brand Coach",
     "Coach Bag - Genuine Leather", "A Coach bag in Genuine Leather."),
    ("Publisher The Criterion Collection Format Blu-ray",
     "Seven Samurai - The Criterion Collection",
     "Released by The Criterion Collection."),
    ("Book Title 1984 Series Vintage Classics Author George Orwell",
     "1984 - Vintage Classics", "Part of the Vintage Classics series."),
])
def test_a_name_the_record_holds_is_not_a_claim(record, title, description):
    """Colour names, materials, imprints and series all collide with regulated
    words, and every one of them used to refuse a listing that said nothing
    misleading."""
    assert review(title, description, record).ok


def test_the_record_has_to_actually_hold_it():
    """A two-word phrase nobody recorded is a flourish, not metadata."""
    assert not review(
        "A vintage classic", "This vintage edition is lovely.",
        "Author George Orwell Format Paperback",
    ).ok


def test_the_word_alone_is_never_a_name():
    """The record containing the bare word must not license the bare word: that is
    what `available_support` is for, and a second looser route would hollow it."""
    assert not review(
        "A vintage paperback", "Vintage.", "Condition notes vintage",
    ).ok


# --- factual assertions still need their evidence ----------------------------------


def test_one_bare_use_spoils_the_excuse():
    """Naming the publisher must not license an age claim two sentences later."""
    r = review(
        "The House on Mango Street - Vintage Contemporaries",
        "Published by Vintage Contemporaries. A lovely vintage find.", BOOK,
    )
    assert not r.ok
    assert any("requires age evidence" in p for p in r.problems)


@pytest.mark.parametrize("word,needs", [
    ("rare", "scarcity"),
    ("mint", "unworn_condition"),
    ("authentic", "authentication"),
    ("handmade", "manufacture"),
])
def test_the_regulated_words_are_still_regulated(word, needs):
    r = review(f"A {word} item", f"This is {word}.", "Brand Nike")
    assert not r.ok
    assert any(needs in p for p in r.problems)


def test_evidence_still_licenses_them():
    assert review(
        "A vintage paperback", "A vintage copy.", BOOK,
        support=frozenset({"condition", "age"}),
    ).ok


def test_market_claims_are_refused_wherever_they_appear():
    """`PROHIBITED_TERMS` are claims about the market, not the object. No record
    value and no amount of marketing framing makes them supportable."""
    r = review("A bag", "A great bag.", "Brand Coach",
               marketing="An investment piece that will only go up.")
    assert not r.ok


# --- subjective copy is not a factual assertion ------------------------------------


def test_ordinary_selling_language_passes():
    """The product goal is listings written without the operator. Copy that
    persuades and asserts nothing checkable is the point, not a risk."""
    r = review(
        "Nike Air Max 90 - Mint Green - Size 10",
        "A versatile pair in Mint Green, great for everyday wear.",
        "Colour Mint Green Brand Nike Model Air Max 90 Size 10",
        marketing="Effortless, understated and perfect for anyone after a classic.",
    )
    assert r.ok, r.problems
