"""A choice the files do not make, and the evidence for making it.

Every ingest writes something into its store that no file states: a pixel
size where the file's tag is a placeholder, three samples read as one colour, a
thumbnail excluded from the images, a suffix on a name, a statement that two
images are one acquisition. Those are the parts of an ingest a reader
cannot check against the source, so they are the parts that most need saying.

A judgement call nobody hears about is indistinguishable from a fact. So each
is one :class:`Decision` object, and the store attribute, the per-study record
and ``LAYOUTS.md`` are three renderings of it rather than three hand-written
copies that drift.

Two distinctions the object keeps that prose loses:

**Silence is not disagreement.** ``source_said=None`` means the file said
nothing, and a value written over silence is a different act from a value
written over a number the file got wrong.

**A number is only as good as where it was read.** ``locus`` says that: a pixel
size from TIFF tags 282/283 with no ``ResolutionUnit``, from OME-XML's
``PhysicalSizeX``, from an Aperio ``MPP=`` inside ``ImageDescription``, from
IDR's annotation table, or from the paper are five different kinds of evidence
that look identical once reduced to a float. Where a value was inherited from
another file, ``locus`` names that file.

``scope`` lets a study-wide call and a per-file exception live in one list: a
decision scoped to nothing applies to every image, and one scoped to a source
filename applies to that file alone.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

#: The kinds of call a study makes. Named so a record can group them, and so
#: that adding a sixth is a deliberate act rather than a new string.
KINDS = {
    "value": "a number written instead of the one the file states, or in its silence",
    "interpretation": "what the file's structure means, where it does not say",
    "inclusion": "a file in the study that is not an image here, and why",
    "naming": "a name that departs from the layout's own rule",
    "relationship": "what another image in the store is to this one",
}


def _quantity(value, unit: str) -> str:
    """A value as the decision quotes it: per axis when it has axes."""
    if isinstance(value, Mapping):
        if not value:
            return "nothing"
        body = ", ".join(f"{axis}={size:g}" for axis, size in value.items())
        return f"{body} {unit}".strip()
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value} {unit}".strip() if unit else str(value)


@dataclasses.dataclass(frozen=True)
class Decision:
    """One judgement call, with everything a reader needs to audit it.

    ``field`` names what was decided and becomes the attribute key,
    ``<field>_decision``. ``wrote`` is the value written and ``source_said``
    what the source stated, or ``None`` where it stated nothing. ``because`` is
    the evidence, in the study's own words.
    """

    field: str
    wrote: object
    because: str
    kind: str = "value"
    source_said: object = None
    #: Where ``source_said`` was read: the tag, the document, the table, or the
    #: other file it was inherited from.
    locus: str = ""
    #: The unit both values are in, where they are quantities.
    unit: str = ""
    #: The source filename this applies to. Empty means the whole study.
    scope: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.field}: unknown kind {self.kind!r}; "
                             f"one of {sorted(KINDS)}")

    @property
    def key(self) -> str:
        """The attribute this is written under, beside the value it explains."""
        return f"{self.field}_decision"

    def against(self, stated) -> Decision:
        """The same call, against what the file turned out to say.

        A study declares what it expects to find; the ingest finds out. The
        store records what was actually there, so a source that changes under
        us shows up as a decision quoting a value nobody wrote down.
        """
        return dataclasses.replace(self, source_said=stated)

    def prose(self) -> str:
        """The decision as the store carries it: one paragraph.

        Assembled rather than written, so every study states a call the same
        way round and the record and the layout notes come off the same object.

        Only a ``value`` call has a number the source could have disagreed
        about, so only it gets the "source said, we wrote" framing. The other
        four decide what the source's own statement *means*, or what it leaves
        out, and for those the evidence is the whole statement; where the value
        was read stays structured, in :meth:`as_record`, rather than being
        forced into a sentence that reads like a correction.
        """
        if self.kind != "value":
            return self.because
        origin = f" ({self.locus})" if self.locus else ""
        if self.source_said is None:
            opening = f"no file states a {self.field.replace('_', ' ')}{origin}"
        else:
            opening = f"file states {_quantity(self.source_said, self.unit)}{origin}"
        return f"{opening}; written as {_quantity(self.wrote, self.unit)}. {self.because}"

    def as_record(self) -> dict:
        """The decision as structured data, for the per-study record."""
        return {"field": self.field, "kind": self.kind, "scope": self.scope or "study",
                "wrote": self.wrote, "source_said": self.source_said,
                "locus": self.locus or None, "because": self.because}


def applicable(decisions, name: str) -> list[Decision]:
    """The decisions bearing on one source file: the study's, then its own.

    Study-wide first, so a file's own call is written last and wins where both
    name the same field.
    """
    return ([d for d in decisions if not d.scope]
            + [d for d in decisions if d.scope == name])
