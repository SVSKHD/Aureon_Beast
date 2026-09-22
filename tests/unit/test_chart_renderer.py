"""The chart: what is on it, what is never on it, and what happens when it cannot be drawn (T-10).

Three kinds of assertion, deliberately separated.

**Structural.** What the figure contains — the annotations, the titles, the footer — asked of the
``Figure`` before it becomes pixels. A perceptual hash tells you the picture changed; it cannot
tell you the invalidation line lost the words "not a stop".

**Behavioural.** Too few bars, no bars, a drawing that raised, a drawing that ran long: each must
produce an image rather than an exception, and the slow one must produce it inside the budget.

**Perceptual.** One golden hash per symbol, with a tolerance, so a change to the drawing that
nothing else notices still has to be looked at. The hashes are regenerated deliberately — see
``test_the_picture_has_not_changed_without_somebody_noticing``.
"""

from __future__ import annotations

import io
import json
import pathlib
import time
from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.market import SymbolInfo
from aureon.visuals import chart_renderer as cr

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "chart_hashes.json"

START = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)


def spec_for(symbol: str) -> SymbolInfo:
    point, digits = (0.01, 2) if symbol == "XAUUSD" else (0.001, 3)
    return SymbolInfo(
        symbol=symbol,
        point=point,
        digits=digits,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )


def bars(symbol: str = "XAUUSD", count: int = 60) -> tuple[cr.ChartBar, ...]:
    """A scripted, entirely deterministic walk. No randomness anywhere: a golden image over a
    random series is a golden image of a seed.

    The two symbols trace DIFFERENT shapes, not one shape at two scales. The first version drew
    the same path for both and the two golden hashes came out one bit apart — because the only
    thing that differed was the width of the axis labels, which a 16x16 perceptual hash cannot
    see. Two golden images of the same picture are one golden image with extra steps.

    Each step also carries the instrument's own precision — gold two decimals, silver three — as
    their quotes do, because the renderer's fallback reads the data.
    """
    if symbol == "XAUUSD":
        base, step, turn, rhythm = 2400.0, 0.25, 0.6, 3
    else:
        base, step, turn, rhythm = 31.0, 0.005, 0.3, 2
    built = []
    for index in range(count):
        # A rise, then a turn. Gold turns late and silver early, so the two pictures differ in
        # shape and not only in the digits on the axis.
        drift = index * step * (1 if index < count * turn else -1.4)
        open_ = base + drift
        close = open_ + (step if index % rhythm else -step)
        built.append(
            cr.ChartBar(
                at=START + timedelta(minutes=5 * index),
                open=round(open_, 5),
                high=round(max(open_, close) + step, 5),
                low=round(min(open_, close) - step, 5),
                close=round(close, 5),
                tick_volume=100 + (index % 7) * 13,
            )
        )
    return tuple(built)


def overlays_for(symbol: str, count: int = 60) -> cr.Overlays:
    made = bars(symbol, count)
    closes = [bar.close for bar in made]
    fast = [None] * 5 + [sum(closes[i - 4 : i + 1]) / 5 for i in range(4, len(closes) - 1)]
    slow = [None] * 10 + [sum(closes[i - 9 : i + 1]) / 10 for i in range(9, len(closes) - 1)]
    return cr.Overlays(
        title=f"{symbol} M5 · liquidity reversal · bullish",
        subtitle="WATCH",
        ema_fast=tuple(fast),
        ema_slow=tuple(slow),
        levels=(
            cr.ChartLevel(price=made[0].high, label="session high"),
            cr.ChartLevel(price=made[0].low, label="previous day low"),
        ),
        value_area=(made[0].low, made[0].high),
        poc_price=made[0].close,
        anchor_price=made[10].high,
        invalidation_price=made[10].low,
        detections=(
            cr.ChartMark(
                at=made[20].at, price=made[20].close, label="wick", direction_context="bullish"
            ),
        ),
        events=(cr.ChartMark(at=made[25].at, price=made[25].close, label="proximity"),),
        notes=("historical reference · measured cohort · research context",),
    )


def figure_for(symbol: str = "XAUUSD"):
    return cr.build(symbol, "M5", bars(symbol), spec_for(symbol), overlays_for(symbol))


def texts(figure) -> list[str]:
    """Every string the figure will draw, from the figure and from each axis."""
    found = [t.get_text() for t in figure.texts]
    for axes in figure.axes:
        found.extend(t.get_text() for t in axes.texts)
        found.append(axes.get_title(loc="left"))
        found.append(axes.get_title(loc="right"))
        found.append(axes.get_ylabel())
        legend = axes.get_legend()
        if legend is not None:
            found.extend(t.get_text() for t in legend.get_texts())
    return [t for t in found if t]


# ── it produces an image ──────────────────────────────────────────────────────


def test_a_render_is_a_png() -> None:
    png = cr.render(
        symbol="XAUUSD",
        timeframe="M5",
        bars=bars(),
        spec=spec_for("XAUUSD"),
        overlays=overlays_for("XAUUSD"),
    )
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 5_000


def test_two_renders_of_the_same_bars_are_byte_identical() -> None:
    """The property a golden comparison rests on. Matplotlib stamps its own version into a PNG's
    metadata unless told not to, which would make every comparison a comparison of versions."""
    kwargs = dict(
        symbol="XAUUSD",
        timeframe="M5",
        bars=bars(),
        spec=spec_for("XAUUSD"),
        overlays=overlays_for("XAUUSD"),
    )
    assert cr.render(**kwargs) == cr.render(**kwargs)


def test_the_png_carries_no_library_version() -> None:
    """Matplotlib stamps its own version into a PNG's `Software` text chunk unless told not to.

    Asserted directly, because "two renders are byte-identical" cannot see it: both renders come
    from the same process and therefore the same version. A plant removing the suppression
    survived that test, which is how this one came to exist. It matters for anyone who ever
    diffs two stored charts across an upgrade.
    """
    png = cr.render(symbol="XAUUSD", timeframe="M5", bars=bars(), spec=spec_for("XAUUSD"))
    assert b"Software" not in png
    assert b"matplotlib" not in png


def test_the_two_symbols_do_not_produce_the_same_picture() -> None:
    gold = cr.render(symbol="XAUUSD", timeframe="M5", bars=bars("XAUUSD"), spec=spec_for("XAUUSD"))
    silver = cr.render(
        symbol="XAGUSD", timeframe="M5", bars=bars("XAGUSD"), spec=spec_for("XAGUSD")
    )
    assert gold != silver


# ── an absence is an image, never an exception ────────────────────────────────


@pytest.mark.parametrize("count", [0, 1, cr.MIN_BARS - 1])
def test_too_few_bars_is_a_labelled_image(count: int) -> None:
    """The exact unavailable image, not merely a different one.

    The first version of this asserted only that the bytes differed from the full chart, which a
    renderer that skipped the guard entirely also satisfies: a chart of four candles is a
    different picture, and a perfectly misleading one. A plant removing the guard survived it.
    """
    png = cr.render(symbol="XAUUSD", timeframe="M5", bars=bars(count=count))
    plural = "" if count == 1 else "s"
    assert png == cr.unavailable(
        "XAUUSD", "M5", f"insufficient data: {count} bar{plural}, {cr.MIN_BARS} needed"
    )
    assert png != cr.render(symbol="XAUUSD", timeframe="M5", bars=bars())


def test_a_drawing_that_raises_is_an_image(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("the renderer fell over")

    monkeypatch.setattr(cr, "_draw", explode)
    png = cr.render(symbol="XAUUSD", timeframe="M5", bars=bars())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png == cr.unavailable("XAUUSD", "M5", "chart unavailable")


def test_a_slow_drawing_returns_an_image_inside_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget bounds the WAIT. The worker is still drawing when this returns, which is the
    documented behaviour and the reason the pool is bounded."""

    def crawl(*args, **kwargs):
        time.sleep(3.0)
        return b"too late"

    monkeypatch.setattr(cr, "_draw", crawl)
    started = time.monotonic()
    png = cr.render(symbol="XAUUSD", timeframe="M5", bars=bars(), budget_seconds=0.4)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"the caller waited {elapsed:.1f}s for a 0.4s budget"
    assert png == cr.unavailable("XAUUSD", "M5", "chart timed out after 0s")
    assert png != b"too late"


def test_the_unavailable_image_says_which_absence_it_is() -> None:
    few = cr.render(symbol="XAUUSD", timeframe="M5", bars=bars(count=4))
    none = cr.render(symbol="XAUUSD", timeframe="M5", bars=())
    assert few != none, "4 bars and 0 bars produced the same image"


# ── what is on it ─────────────────────────────────────────────────────────────


def test_the_invalidation_line_is_never_offered_as_a_stop() -> None:
    """A red dashed line at a round number under a candle chart is read as a stop by everyone who
    has ever traded. This one is where the structure stops being true, and nothing places an
    order from it."""
    drawn = " ".join(texts(figure_for()))
    assert "invalidation" in drawn
    assert "not a stop" in drawn


def test_the_volume_panel_is_named_tick_volume() -> None:
    drawn = " ".join(texts(figure_for()))
    assert cr.VOLUME_LABEL in drawn
    assert "price changes, not contracts traded" in cr.VOLUME_LABEL


def test_every_chart_carries_the_footer() -> None:
    assert any(cr.FOOTER_NOTE in text for text in texts(figure_for()))
    assert "no order is implied" in cr.FOOTER_NOTE


def test_the_notes_ride_beside_the_footer_rather_than_replacing_it() -> None:
    figure = cr.build(
        "XAUUSD", "M5", bars(), spec_for("XAUUSD"), cr.Overlays(notes=("immature history",))
    )
    drawn = " ".join(texts(figure))
    assert "immature history" in drawn
    assert cr.FOOTER_NOTE in drawn


def test_a_rising_candle_and_a_falling_one_are_drawn_differently() -> None:
    """The thing the golden hash CANNOT see.

    An average hash over a downsampled greyscale image is a coarse tripwire: it catches a layout
    change and it does not catch a colour change, because two colours of similar luminance
    occupy the same cells with the same brightness. A plant that swapped the up and down colours
    for near-black and near-white moved the gold hash by zero bits.

    So the colours are asserted directly, off the drawn artists, rather than by tuning the
    hash's tolerance until it happened to catch that one plant.
    """
    from matplotlib.colors import to_rgba
    from matplotlib.patches import Rectangle

    figure = cr.build("XAUUSD", "M5", bars("XAUUSD"), spec_for("XAUUSD"), cr.Overlays())
    bodies = [p for p in figure.axes[0].patches if isinstance(p, Rectangle)]
    colours = {p.get_facecolor() for p in bodies}

    assert to_rgba(cr.UP_COLOUR) in colours
    assert to_rgba(cr.DOWN_COLOUR) in colours
    assert cr.UP_COLOUR != cr.DOWN_COLOUR


def test_no_order_vocabulary_reaches_the_picture() -> None:
    """A chart is the easiest place for a reader to supply an intention the system never had."""
    drawn = " ".join(texts(figure_for())).lower()
    for forbidden in (
        "buy",
        "sell",
        "entry",
        "take profit",
        "stop loss",
        "stop-loss",
        "target",
        "lot size",
    ):
        assert forbidden not in drawn, f"the chart said {forbidden!r}"


def test_the_forbidden_market_vocabulary_is_absent_from_the_module() -> None:
    source = pathlib.Path(cr.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("absorption", "footprint", "order flow", "volume delta"):
        assert forbidden not in source


def test_the_renderer_computes_no_indicator() -> None:
    """Discord is forbidden from computing indicators (§71), and Discord is this module's main
    caller. A renderer with its own EMA would smuggle that capability in behind a picture — and
    would draw a market nobody actually looked at."""
    source = pathlib.Path(cr.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "from aureon.engine.indicators",
        "from aureon.engine.volatility",
        "ema(",
        "rsi(",
        "atr(",
    ):
        assert forbidden not in source, f"the renderer reached for {forbidden!r}"


def test_the_module_never_touches_pyplot() -> None:
    """``pyplot`` keeps global state, and this draws in a worker thread inside a bot that may
    render two charts at once. Two threads sharing one current axis draw each other's candles.

    AST rather than a grep for the word: the module docstring EXPLAINS why pyplot is not used,
    and a text search would fail on the explanation -- which is how a guard gets deleted for
    being annoying rather than fixed for being blunt.
    """
    import ast

    tree = ast.parse(pathlib.Path(cr.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if "pyplot" in name], imported
    assert any("backend_agg" in name for name in imported), imported


# ── the small decisions ───────────────────────────────────────────────────────


def test_the_price_axis_follows_the_brokers_digits() -> None:
    assert cr._decimals(spec_for("XAUUSD"), bars("XAUUSD")) == 2
    assert cr._decimals(spec_for("XAGUSD"), bars("XAGUSD")) == 3


@pytest.mark.parametrize(("symbol", "expected"), [("XAUUSD", "%.2f"), ("XAGUSD", "%.3f")])
def test_the_axis_formatter_carries_that_precision_onto_the_picture(
    symbol: str, expected: str
) -> None:
    """The decimals are only useful if they reach the axis.

    Asserted here rather than left to the golden hash, because a 16x16 perceptual hash cannot
    see the width of a tick label — which is exactly the per-instrument behaviour this renderer
    exists to get right.
    """
    figure = figure_for(symbol)
    price_axis = figure.axes[0]
    assert price_axis.yaxis.get_major_formatter().fmt == expected


def test_with_no_spec_the_precision_comes_from_the_prices_not_their_size() -> None:
    """The first version of this inferred from the MAGNITUDE of the price -- under ten, four
    decimals; over ten, two -- which is wrong on silver: about $31, quoted to three. A heuristic
    that is confidently wrong on half the portfolio is worse than none."""
    assert cr._decimals(None, bars("XAUUSD")) == 2
    assert cr._decimals(None, bars("XAGUSD")) == 3

    # And it is the data's precision, not its size: two instruments at the same price with
    # different ticks must not be formatted alike.
    coarse = (cr.ChartBar(at=START, open=31.0, high=31.1, low=30.9, close=31.05),)
    fine = (cr.ChartBar(at=START, open=31.0, high=31.1005, low=30.9, close=31.05),)
    assert cr._decimals(None, coarse) == 2
    assert cr._decimals(None, fine) == 4


def test_the_inferred_precision_is_capped() -> None:
    """A float artefact must not produce a wall of digits on the axis."""
    noisy = (cr.ChartBar(at=START, open=31.123456789012, high=31.2, low=31.0, close=31.1),)
    assert cr._decimals(None, noisy) == cr.MAX_DECIMALS


def test_a_spec_always_wins_over_the_fallback() -> None:
    """The broker's own digits are the answer; the inference is only for when there is none."""
    fine = (cr.ChartBar(at=START, open=31.1005, high=31.2, low=31.0, close=31.1),)
    assert cr._decimals(spec_for("XAUUSD"), fine) == 2


def test_the_window_is_clamped_at_both_ends() -> None:
    made = bars(count=500)
    assert len(cr._window(made, 10_000)) == cr.MAX_WINDOW
    assert len(cr._window(made, 1)) == cr.MIN_BARS
    assert len(cr._window(made, 50)) == 50
    # And it is the LAST n, because a chart is about now.
    assert cr._window(made, 50)[-1] == made[-1]


def test_bars_are_sorted_even_when_the_caller_hands_them_over_backwards() -> None:
    made = bars(count=30)
    assert cr._window(tuple(reversed(made)), 30) == made


def test_an_indicator_series_is_padded_on_the_left() -> None:
    """An EMA is missing at the START of a window, never at the end. Padding on the right would
    draw it one bar ahead of the price it was computed from, which looks like foresight."""
    assert cr._pad([1.0, 2.0], 4) == [None, None, 1.0, 2.0]
    assert cr._pad([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [3.0, 4.0, 5.0]


def test_a_mark_outside_the_window_is_dropped_rather_than_clamped() -> None:
    """A detection from before the window drawn on the first candle is a false statement about
    when it happened — and one a reader cannot check, because the bar is right there."""
    times = [START + timedelta(minutes=5 * i) for i in range(5)]
    assert cr._nearest(times, times[2]) == 2
    assert cr._nearest(times, times[0] - timedelta(minutes=1)) is None
    assert cr._nearest(times, times[-1] + timedelta(minutes=1)) is None
    # A mark stamped at a close falls inside the bar it closed.
    assert cr._nearest(times, times[1] + timedelta(minutes=4)) == 1


def test_the_x_labels_carry_the_date_only_when_the_window_spans_one() -> None:
    same_day = [START + timedelta(minutes=5 * i) for i in range(12)]
    _, labels = cr._time_ticks(same_day)
    assert all(len(label) == 5 for label in labels), labels

    across = [START + timedelta(hours=6 * i) for i in range(8)]
    _, spanning = cr._time_ticks(across)
    assert all("-" in label for label in spanning), spanning


# ── the day-frame source ──────────────────────────────────────────────────────


class FakeFrames:
    def __init__(self) -> None:
        self.recent_calls: list[dict] = []
        self.named: list[tuple[str, str]] = []

    def recent_frames(self, symbol, timeframe, market_dates, *, complete_only=True):
        self.recent_calls.append(
            {"dates": list(market_dates), "complete_only": complete_only}
        )
        return []

    def get_frame(self, symbol, market_date, timeframe):
        self.named.append((symbol, market_date))
        return None


def test_completed_days_come_from_the_cache_and_today_is_asked_for_by_name() -> None:
    """The day in progress is the documented exception (11D): every other reader must not have
    it, and a chart must. So it is fetched by name rather than by relaxing the default."""
    repository = FakeFrames()
    cr.bars_for(
        repository,
        symbol="XAUUSD",
        timeframe="M5",
        market_dates=["2026-09-14", "2026-09-15"],
        include_today="2026-09-16",
    )
    assert repository.recent_calls == [
        {"dates": ["2026-09-14", "2026-09-15"], "complete_only": True}
    ]
    assert repository.named == [("XAUUSD", "2026-09-16")]


def test_without_include_today_nothing_in_progress_is_read() -> None:
    repository = FakeFrames()
    cr.bars_for(
        repository, symbol="XAUUSD", timeframe="M5", market_dates=["2026-09-15"]
    )
    assert repository.named == []


# ── the golden image ──────────────────────────────────────────────────────────


def perceptual_hash(png: bytes, *, size: int = 16) -> str:
    """A 256-bit average hash: greyscale, downsample, one bit per cell against the mean.

    Sensitive to the SHAPE of the picture and insensitive to a font's hinting, which is the
    property wanted here — a golden comparison on raw bytes fails on every matplotlib patch
    release and teaches everyone to regenerate it without looking.

    Its blind spot, measured rather than assumed: **it does not see a colour change.** Swapping
    the up and down candle colours for near-black and near-white moved the gold hash by zero
    bits here. Neither more resolution nor more channels fixes it — at 48x48 greyscale the same
    swap moved 18 bits of 2304, and a three-channel hash at 16x16 moved 3 of 768, while a layout
    change at the same settings moved 144. The reason is structural: candle bodies are thin, so
    at any downsample their colour is averaged into the background.

    So the colours are asserted directly, off the drawn artists, by
    ``test_a_rising_candle_and_a_falling_one_are_drawn_differently`` — rather than by tuning
    this tolerance until it happened to catch one plant.
    """
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        grey = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        cells = list(grey.tobytes())
    mean = sum(cells) / len(cells)
    bits = "".join("1" if cell >= mean else "0" for cell in cells)
    return f"{int(bits, 2):0{size * size // 4}x}"


def distance(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD"])
def test_the_picture_has_not_changed_without_somebody_noticing(symbol: str) -> None:
    """One golden hash per symbol, with a tolerance in bits.

    A matplotlib upgrade can legitimately move a few bits — a font metric, an antialiasing
    change — so the comparison has slack. A layout change moves far more than the slack, and
    then the hash is regenerated **deliberately**:

        python -c "from tests.unit.test_chart_renderer import *; import json; \\
            json.dump({s: perceptual_hash(cr.render(symbol=s, timeframe='M5', \\
            bars=bars(s), spec=spec_for(s), overlays=overlays_for(s))) \\
            for s in ('XAUUSD','XAGUSD')}, open(str(GOLDEN),'w'), indent=2)"

    Both symbols, because gold and silver differ in the one thing this renderer is meant to get
    right per instrument — the number of decimals on the price axis — and a single golden image
    would leave that untested.
    """
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    png = cr.render(
        symbol=symbol,
        timeframe="M5",
        bars=bars(symbol),
        spec=spec_for(symbol),
        overlays=overlays_for(symbol),
    )
    found = perceptual_hash(png)
    assert distance(found, golden[symbol]) <= 12, (
        f"the {symbol} chart moved: {found} vs the golden {golden[symbol]}. "
        "If that was intended, regenerate the hashes -- see this test's docstring."
    )


def test_the_golden_hashes_actually_discriminate() -> None:
    """A tolerance wide enough to accept any picture is not a test. Gold's hash must be outside
    silver's tolerance and vice versa."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    apart = distance(golden["XAUUSD"], golden["XAGUSD"])
    assert apart > 12, f"the two golden hashes are only {apart} bits apart"
