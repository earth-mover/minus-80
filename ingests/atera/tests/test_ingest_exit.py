import json

import pytest

import ingest


def _refuse():
    raise ValueError("the table differs")


def test_a_refusal_of_the_ingest_ends_with_its_message_alone(monkeypatch):
    monkeypatch.setattr(ingest, "run", _refuse)
    with pytest.raises(SystemExit, match="the table differs"):
        ingest.main()


def test_a_value_error_from_another_library_keeps_its_traceback(monkeypatch):
    monkeypatch.setattr(ingest, "run", lambda: json.loads("{"))
    with pytest.raises(json.JSONDecodeError):
        ingest.main()
