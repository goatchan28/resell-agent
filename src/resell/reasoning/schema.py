"""Vocabularies and value types for the reasoning plane.

Pure: no database, no HTTP, no model calls. These are the words the model is
allowed to speak in and the shapes its output must take, so that a free-reasoning
model produces something a deterministic gate can check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Basis(StrEnum):
    """How a claim came to be believed.

    Recorded per evidence record. Deliberately not a ranking: precedence is
    computed where it matters rather than applied silently, because a hidden
    priority order re-introduces unexplained value selection with better paperwork.
    """

    TEXT_READ = "text_read"                  # transcribed from text visible in a photo
    VISUAL_OBSERVATION = "visual_observation"  # directly perceptible property
    MEASUREMENT = "measurement"              # a physical dimension, with method
    INFERENCE = "inference"                  # reasoned from observations + world knowledge
    OPERATOR = "operator"                    # asserted by the human
    EXTERNAL_SOURCE = "external_source"      # from a lookup, with URL and retrieval time


# Only the operator has adjudicated a claim with the object in hand, so an operator
# statement settles a disagreement. Everything else is recorded and surfaced rather
# than silently ranked.
ADJUDICATING_BASES = frozenset({Basis.OPERATOR})


class MeasurementMethod(StrEnum):
    """How a dimension was obtained. `estimated_from_photo` must look weaker."""

    TAPE = "tape"
    STATED_BY_OPERATOR = "stated_by_operator"
    ESTIMATED_FROM_PHOTO = "estimated_from_photo"


class Subject(StrEnum):
    """What a piece of evidence is *about*.

    The field that lets external research join the loop without contaminating it.
    A catalogue page describes a candidate product, not the object on the table;
    connecting the two is itself a claim that needs its own basis. Keeping them
    apart is also what later lets comps distinguish exact-product matches from
    similarity matches instead of blending them.
    """

    THIS_ITEM = "this_item"
    CANDIDATE_PRODUCT = "candidate_product"


class IdentificationMode(StrEnum):
    """How precisely the item is known. A conclusion, not a starting assumption.

    Versioned with the identification rather than fixed on the item: an object can
    begin as a described_object and become exact_product when a model number turns
    up on its base.
    """

    UNRESOLVED = "unresolved"
    # A specific manufacturer product, ideally identifier-verified.
    EXACT_PRODUCT = "exact_product"
    # Brand and model line known; variant, year or colourway uncertain.
    PRODUCT_FAMILY = "product_family"
    # Brand known from a label, but no model or line discoverable. Extremely common
    # and behaves like neither neighbour: there is a real citable brand fact, but
    # no product to match against.
    BRANDED_GENERIC = "branded_generic"
    # No brand discoverable, or brand irrelevant. A successful outcome, not a
    # failure -- most household objects live here.
    DESCRIBED_OBJECT = "described_object"


# Modes that assert no discoverable product identity, and therefore require a cited
# negative finding rather than mere absence of a positive one.
MODES_REQUIRING_NEGATIVE_FINDING = frozenset(
    {IdentificationMode.DESCRIBED_OBJECT, IdentificationMode.BRANDED_GENERIC}
)


class IdentifierScheme(StrEnum):
    UPC = "upc"
    EAN = "ean"
    ISBN = "isbn"
    MPN = "mpn"
    MODEL_NUMBER = "model_number"
    STYLE_NUMBER = "style_number"
    RN_NUMBER = "rn_number"
    CA_NUMBER = "ca_number"
    SERIAL = "serial"
    DATE_CODE = "date_code"
    MAKERS_MARK = "makers_mark"
    EPID = "epid"
    OTHER = "other"


class IdentificationEffort(StrEnum):
    """How hard to look before accepting that no identity is available.

    Scales the negative-evidence requirement rather than the concept: there is
    always a citable reason, but a three-dollar ornament does not earn a forensic
    surface sweep.
    """

    MINIMAL = "minimal"
    STANDARD = "standard"
    THOROUGH = "thorough"


# --- identifier validation ---------------------------------------------------
#
# Check digits are one of the very few places a transcription can be proven wrong
# rather than merely doubted. OCR reliably confuses 0/O, 1/I, 5/S and 8/B, so an
# arithmetic check catches misreads before they reach a listing field or a
# catalogue lookup.

CHECKED_SCHEMES = frozenset(
    {IdentifierScheme.UPC, IdentifierScheme.EAN, IdentifierScheme.ISBN}
)


# Characters that are only formatting and carry no information.
_FORMATTING = re.compile(r"[\s\-.\u2010-\u2015]")

# Glyphs OCR reliably confuses with digits. Substituting these is how a misread is
# distinguished from a genuinely wrong number.
OCR_CONFUSIONS = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "G": "6", "Z": "2"}


def normalize_identifier(scheme: IdentifierScheme, raw: str) -> str:
    """Strip formatting only. Preserves a trailing X for ISBN-10.

    Deliberately does NOT strip letters from numeric schemes. An earlier version
    used a digits-only filter, which turned the OCR misread "O36OOO291452" into
    "36291452" -- silently deleting four characters and shortening the identifier,
    so the failure was reported as a length problem rather than as the substitution
    it actually was. Keeping the characters lets the check-digit stage recognise a
    misread and suggest the correction.
    """
    if scheme in CHECKED_SCHEMES:
        # Hyphens and spaces in a UPC/EAN/ISBN are printing conventions and carry
        # no information, so removing them is safe.
        return _FORMATTING.sub("", raw).strip().upper()
    # Everywhere else the punctuation IS the value. An MPN "TL-4471" is not
    # "TL4471"; stripping the hyphen produces a string that may match nothing in a
    # catalogue, which is worse than not normalising at all.
    return raw.strip()


def ocr_corrected(value: str) -> str:
    return "".join(OCR_CONFUSIONS.get(char, char) for char in value)


def _upc_a_valid(digits: str) -> bool:
    if len(digits) != 12 or not digits.isdigit():
        return False
    values = [int(d) for d in digits]
    total = 3 * sum(values[0:11:2]) + sum(values[1:11:2])
    return (10 - total % 10) % 10 == values[11]


def _ean13_valid(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    values = [int(d) for d in digits]
    total = sum(value * (3 if index % 2 else 1) for index, value in enumerate(values[:12]))
    return (10 - total % 10) % 10 == values[12]


def _isbn10_valid(value: str) -> bool:
    if len(value) != 10 or not re.fullmatch(r"\d{9}[\dX]", value):
        return False
    total = sum(
        (10 if char == "X" else int(char)) * (10 - index)
        for index, char in enumerate(value)
    )
    return total % 11 == 0


def validate_identifier(scheme: IdentifierScheme, raw: str) -> tuple[bool | None, str]:
    """Check an identifier arithmetically where the scheme allows it.

    Returns (valid, explanation). `None` means the scheme carries no check digit,
    which is not the same as passing -- an MPN or model number can only be verified
    against a catalogue, never against itself.
    """
    normalized = normalize_identifier(scheme, raw)
    if scheme not in CHECKED_SCHEMES:
        return None, f"{scheme} carries no check digit; cannot be verified in isolation"

    def passes(value: str) -> str | None:
        if scheme is IdentifierScheme.UPC:
            if _upc_a_valid(value):
                return "UPC-A"
            if len(value) == 13 and _ean13_valid(value):
                return "EAN-13 (UPC read with a leading zero)"
            return None
        if scheme is IdentifierScheme.EAN:
            return "EAN-13" if _ean13_valid(value) else None
        if _isbn10_valid(value):
            return "ISBN-10"
        return "ISBN-13" if _ean13_valid(value) else None

    kind = passes(normalized)
    if kind:
        return True, f"valid {kind} check digit"

    # Before calling it wrong, see whether it is merely misread. A substitution that
    # produces a valid check digit is almost certainly the intended number, and
    # naming it saves the operator squinting at the photo again.
    corrected = ocr_corrected(normalized)
    if corrected != normalized:
        kind = passes(corrected)
        if kind:
            return False, (
                f"check digit failed for {normalized!r}, but {corrected!r} is a valid "
                f"{kind} -- looks like an OCR misread. Confirm against the photo."
            )

    return False, (
        f"{scheme} check digit failed for {normalized!r} "
        f"(length {len(normalized)}; 0/O, 1/I, 5/S and 8/B are the usual misreads)"
    )


# --- typed observations ------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """A citation. `basis` travels with it so resolution can be computed."""

    evidence_id: int
    basis: Basis


@dataclass(frozen=True)
class Observation:
    """One discrete claim about the item, as the model reports it."""

    claim: str
    basis: Basis
    subject: Subject = Subject.THIS_ITEM
    photo_positions: tuple[int, ...] = ()
    confidence: float | None = None          # diagnostic only; never a gate
    measurement_method: MeasurementMethod | None = None
    surface: str | None = None               # where on the object, e.g. "underside"

    def problems(self) -> list[str]:
        issues = []
        if not self.claim.strip():
            issues.append("claim is empty")
        if self.basis in (Basis.TEXT_READ, Basis.VISUAL_OBSERVATION) and not self.photo_positions:
            issues.append(
                f"{self.basis} must cite the photo it came from, so the operator can check it"
            )
        if self.basis is Basis.MEASUREMENT and self.measurement_method is None:
            issues.append("measurement must state its method")
        if self.basis is Basis.EXTERNAL_SOURCE and self.subject is Subject.THIS_ITEM:
            issues.append(
                "an external source describes a candidate product, not this item; "
                "linking the two is a separate claim"
            )
        return issues


@dataclass(frozen=True)
class IdentifierObservation:
    """A product identifier read off the object.

    Maps to the Inventory API's dedicated product fields (upc, ean, isbn, mpn,
    brand, epid) rather than to aspects, so capturing these has direct listing
    value and later enables eBay catalogue matching.
    """

    scheme: IdentifierScheme
    raw_transcription: str
    photo_position: int
    surface: str | None = None
    normalized: str = field(init=False)
    check_digit_valid: bool | None = field(init=False)
    check_explanation: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "normalized", normalize_identifier(self.scheme, self.raw_transcription))
        valid, explanation = validate_identifier(self.scheme, self.raw_transcription)
        object.__setattr__(self, "check_digit_valid", valid)
        object.__setattr__(self, "check_explanation", explanation)

    @property
    def usable(self) -> bool:
        """False only when a check digit was available and failed."""
        return self.check_digit_valid is not False


@dataclass(frozen=True)
class NegativeFinding:
    """A cited reason to believe no product identity is available.

    Required to declare described_object or branded_generic. Without it, a model
    can reach those modes by giving up, and laziness becomes indistinguishable from
    diligence.
    """

    surfaces_examined: tuple[str, ...] = ()
    photos_reviewed: int = 0
    operator_confirmed: bool = False
    note: str = ""
