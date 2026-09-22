"""The one keyword that differs between a send and an edit (12, T-11).

Its own file because it is a test about discord.py's API rather than about Aureon's logic, and
because the failure it guards is silent: discord.py accepts ``file=`` on an ``edit`` and ignores
it. Get the keyword backwards and a card's WORDS update while its PICTURE stays the old one — a
chart of twenty candles ago under a state badge that says CONFIRMED, which is the most misleading
thing this system could put in a channel and the hardest for a reader to diagnose.
"""

from __future__ import annotations

from aureon.discord.bot import _attachment


def test_a_send_takes_file() -> None:
    keywords = _attachment(b"\x89PNG", "chart.png")
    assert set(keywords) == {"file"}


def test_an_edit_takes_attachments() -> None:
    keywords = _attachment(b"\x89PNG", "chart.png", editing=True)
    assert set(keywords) == {"attachments"}
    assert len(keywords["attachments"]) == 1


def test_a_send_with_no_chart_passes_nothing() -> None:
    """Not ``file=None``: discord.py treats an explicit None differently from an absent keyword
    on some versions, and a card without a picture should look like a card without a picture."""
    assert _attachment(None, None) == {}
    assert _attachment(b"", "chart.png") == {}
    assert _attachment(b"\x89PNG", None) == {}


def test_an_edit_with_no_chart_clears_the_old_one() -> None:
    """Deliberate: a setup whose chart can no longer be drawn should LOSE its picture rather
    than keep one from twenty candles ago beside a state badge that has moved on."""
    assert _attachment(None, None, editing=True) == {"attachments": []}


def test_the_filename_reaches_the_attachment() -> None:
    """The embed points at ``attachment://<filename>``, so a mismatch renders an empty image
    frame -- which looks like a broken chart rather than a missing one."""
    keywords = _attachment(b"\x89PNG", "XAUUSD_M5_abcd1234.png", editing=True)
    assert keywords["attachments"][0].filename == "XAUUSD_M5_abcd1234.png"
