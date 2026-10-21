"""A judgement call renders the same way whoever writes it, and keeps its provenance.

The point of the type is that three things -- the store attribute, the study
record and the layout notes -- come off one object instead of being written by
hand three times. These check the renderings, and the two distinctions prose
alone loses: silence against disagreement, and where a number was read.
"""

from __future__ import annotations

import pytest

from virtual_core.decisions import Decision, applicable


def test_a_value_call_quotes_the_source_then_what_was_written():
    call = Decision(field="pixel_size", kind="value", unit="um",
                    wrote={"y": 0.0008, "x": 0.0008},
                    locus="TIFF tags 282/283, ResolutionUnit absent",
                    because="IDR and the paper both state 0.8 nm.")
    assert call.key == "pixel_size_decision"
    assert call.against({"y": 2.7e13, "x": 2.7e13}).prose() == (
        "file states y=2.7e+13, x=2.7e+13 um (TIFF tags 282/283, ResolutionUnit "
        "absent); written as y=0.0008, x=0.0008 um. IDR and the paper both "
        "state 0.8 nm.")


def test_silence_reads_differently_from_disagreement():
    """``source_said=None`` is the file saying nothing, not the file saying zero."""
    call = Decision(field="time_increment", kind="value", wrote=120.0, unit="s",
                    locus="no file carries one", because="The paper says 2 minutes.")
    assert call.prose().startswith("no file states a time increment (no file carries one)")
    assert call.against({}).prose().startswith("file states nothing (no file carries one)")


def test_only_a_value_call_is_framed_as_a_correction():
    """An interpretation decides what the file means; it contradicts no number."""
    call = Decision(field="colour", kind="interpretation", wrote=True,
                    locus="OME-XML, three single-sample channels",
                    because="The scanner's own metadata says RGB.")
    assert call.prose() == "The scanner's own metadata says RGB."
    assert call.as_record()["locus"] == "OME-XML, three single-sample channels"


def test_a_file_specific_call_is_written_after_the_study_wide_one():
    study_wide = Decision(field="pixel_size", wrote=1.0, because="the usual")
    just_one = Decision(field="pixel_size", wrote=2.0, because="not this one",
                        scope="odd.tif")
    assert applicable([just_one, study_wide], "odd.tif") == [study_wide, just_one]
    assert applicable([just_one, study_wide], "ordinary.tif") == [study_wide]


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown kind"):
        Decision(field="pixel_size", wrote=1.0, because="", kind="vibes")
