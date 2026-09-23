"""A picture of what the machine is looking at (12, T-10).

A setup card is a list of assertions — "swept the session high, reclaimed it, RSI turned" — and a
reader has no way to check any of them against the market. The chart is the check. It is the only
artefact in this system whose purpose is to let a human disagree with it.

## It draws what it is given and computes nothing

Every series on the chart arrives in ``Overlays``: the EMAs, the levels, the value area, the
anchor. The renderer never computes an indicator, for two reasons. The caller is usually Discord,
which is forbidden from computing indicators at all (§71) — so a renderer that computed its own
EMA would smuggle that capability in behind a picture. And a chart showing an EMA the observer did
not detect against would be a picture of a market nobody looked at: the numbers must come from the
same window the agents saw, or the image is evidence for a different question.

## Never ``pyplot``

The figure is built through the object API (``Figure`` + ``FigureCanvasAgg``). ``pyplot`` keeps
global state — a registry of open figures, a current axis — and this code runs in a worker thread
inside a Discord bot that may render two charts at once. Two threads sharing one current axis draw
each other's candles. A test asserts this module does not import ``pyplot``.

## Prices are formatted from the broker's own ``digits``

Gold quotes to two decimals and silver to three, and rounding silver to gold's precision puts the
value-area boundary on the wrong tick. ``SymbolInfo.digits`` and ``point`` come from
``symbol_specs``, which the observer publishes from the terminal (§39). With no spec the renderer
falls back to a width inferred from the prices themselves and says nothing about ticks.

## The budget bounds the WAIT, not the work

``render`` runs the drawing in a worker thread and waits ``budget_seconds`` for it. If the budget
expires the caller gets an "unavailable" image immediately, which is the point: a Discord
interaction has three seconds before it looks broken.

**The worker keeps running.** A Python thread cannot be cancelled, so the budget does not stop the
drawing — it stops the waiting. That is stated here rather than implied, because a reader who
believed otherwise would size the budget as if it were a resource limit. The executor is bounded
instead, so a pathological caller queues rather than forking threads without limit.

## An absence is an image, never an exception

Too few bars, no bars at all, a drawing that raised: each produces a small labelled image saying
which. A card whose chart is missing looks broken and a card with a chart saying "insufficient
data: 4 bars" says exactly what happened, which is a different and more useful failure.
"""

from __future__ import annotations

import concurrent.futures
import io
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

#: How long a caller waits. Five seconds because a Discord interaction has three before it looks
#: broken and this runs behind a deferred reply, not in front of one.
CHART_BUDGET_SECONDS = 5.0

#: Bars drawn by default, and the most that may be asked for. A hundred and twenty M5 bars is ten
#: hours -- a session and its run-up. The cap exists because the x axis has a fixed pixel width:
#: past a few hundred bars each candle is thinner than a line and the picture stops being readable
#: while taking longer to draw.
DEFAULT_WINDOW = 120
MAX_WINDOW = 400

#: Below this, there is no chart to draw. Ten bars is the smallest number from which a reader can
#: see a shape at all.
MIN_BARS = 10

#: 960x540 at 100 DPI: the size Discord renders inline without asking the reader to click.
WIDTH_INCHES = 9.6
HEIGHT_INCHES = 5.4
WIDE_WIDTH_INCHES = 12.8
WIDE_HEIGHT_INCHES = 7.2
DPI = 100

#: How much of the figure the volume panel takes.
PRICE_HEIGHT_RATIO = 4

#: Colours. Named so a change is one edit and so the reasons can be written down.
UP_COLOUR = "#2e7d52"
DOWN_COLOUR = "#b23a3a"
FAST_COLOUR = "#1f6fb2"
SLOW_COLOUR = "#8a6d3b"
LEVEL_COLOUR = "#666666"
#: The invalidation line. Red, like a stop, and labelled so nobody reads it as one -- it is where
#: the structure stops being true, and nothing in this system places an order from it.
INVALIDATION_COLOUR = "#b23a3a"
ANCHOR_COLOUR = "#2b2b2b"
VALUE_AREA_COLOUR = "#4a90d9"
GRID_COLOUR = "#dddddd"

#: The words under every volume panel. MT5's "volume" is the number of price CHANGES in the bar,
#: not contracts traded, and a panel labelled "volume" beside a candle chart reads as the second.
VOLUME_LABEL = "tick volume (price changes, not contracts traded)"

#: The words under every chart. It is a picture of observations, and a picture is the easiest
#: place for a reader to supply an intention the system never had.
FOOTER_NOTE = "observations only · no order is implied by anything on this chart"


@dataclass(frozen=True)
class ChartBar:
    """One bar, in the renderer's own terms.

    Its own type rather than ``Candle`` or ``FrameBar``, because the two callers hold different
    things -- the observer has ``Candle``s in memory and Discord has a stored ``MarketDayFrame`` --
    and a renderer that accepted either would grow a branch per source. ``from_candles`` and
    ``from_frames`` do the conversion at the edge.
    """

    at: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0

    @property
    def rising(self) -> bool:
        return self.close >= self.open


@dataclass(frozen=True)
class ChartLevel:
    """A horizontal line with a name. The name is the point: an unlabelled line is decoration."""

    price: float
    label: str


@dataclass(frozen=True)
class ChartMark:
    """Something that happened at a bar: a detection, or a setup event."""

    at: datetime
    price: float
    label: str = ""
    #: ``bullish`` / ``bearish`` / ``None``. Never BUY or SELL: a mark on a chart of observations
    #: is not a side to take.
    direction_context: str | None = None


@dataclass(frozen=True)
class Overlays:
    """Everything drawn on top of the candles, all of it computed elsewhere.

    The EMA series are aligned to the bars they are drawn with: ``ema_fast[i]`` belongs to
    ``bars[i]``, and ``None`` means the series had no value there (an EMA has no value before its
    period is filled). A shorter series is padded on the LEFT, because indicators are missing at
    the start of a window and not at the end -- getting that backwards would draw an EMA one bar
    ahead of the price it was computed from, which looks like foresight.
    """

    title: str = ""
    subtitle: str = ""
    ema_fast: tuple[float | None, ...] = ()
    ema_slow: tuple[float | None, ...] = ()
    ema_fast_label: str = "EMA fast"
    ema_slow_label: str = "EMA slow"
    levels: tuple[ChartLevel, ...] = ()
    #: ``(low, high)`` of the value area, shaded. Not a range to trade.
    value_area: tuple[float, float] | None = None
    poc_price: float | None = None
    #: The setup's anchor, and where its claim is falsified. Labelled, never styled as orders.
    anchor_price: float | None = None
    invalidation_price: float | None = None
    detections: tuple[ChartMark, ...] = ()
    events: tuple[ChartMark, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Human-readable market context rendered in a dedicated right-side panel. Values are
    #: supplied by the observer/Discord service; the renderer does not derive them.
    analysis_lines: tuple[str, ...] = field(default_factory=tuple)


def from_candles(candles: Sequence[Any]) -> tuple[ChartBar, ...]:
    """``Candle``s as the observer holds them."""
    return tuple(
        ChartBar(
            at=candle.open_time.utc,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            tick_volume=int(candle.tick_volume or 0),
        )
        for candle in candles
    )


def from_frames(frames: Sequence[Any]) -> tuple[ChartBar, ...]:
    """Stored ``MarketDayFrame`` documents, oldest day first, flattened and re-sorted.

    Re-sorted rather than trusted: each frame's bars are ordered by its own validator, and the
    frames arrive ordered by the repository, but a caller that passed two days out of order would
    otherwise draw Tuesday before Monday with no sign that anything was wrong.
    """
    bars = [
        ChartBar(
            at=bar.at,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            tick_volume=int(bar.tick_volume or 0),
        )
        for frame in frames
        for bar in frame.bars
    ]
    return tuple(sorted(bars, key=lambda bar: bar.at))


def bars_for(
    repository: Any,
    *,
    symbol: str,
    timeframe: Any,
    market_dates: Sequence[str],
    include_today: str | None = None,
) -> tuple[ChartBar, ...]:
    """Bars from the day-frame cache: COMPLETED days, plus today by name if asked (11D).

    ``recent_frames`` returns complete days only, which is the right default everywhere else in
    this system: an unfinished day's last bar is not its last bar, and an aggregation that read
    one would produce a daily candle whose close is not the close.

    A chart is the documented exception -- it wants the day in progress, because that is the day a
    reader is looking at -- so the day in progress is fetched by name with ``get_frame`` and the
    caller has to ask for it. The distinction stays visible rather than becoming a default.
    """
    frames = list(
        repository.recent_frames(symbol, timeframe, market_dates, complete_only=True)
    )
    if include_today is not None:
        today = repository.get_frame(symbol, include_today, timeframe)
        if today is not None:
            frames.append(today)
    return from_frames(frames)


# ── the public entry point ────────────────────────────────────────────────────


def render(
    *,
    symbol: str,
    timeframe: Any,
    bars: Sequence[ChartBar],
    spec: Any | None = None,
    overlays: Overlays | None = None,
    window: int = DEFAULT_WINDOW,
    budget_seconds: float = CHART_BUDGET_SECONDS,
) -> bytes:
    """A PNG of the last ``window`` bars. Never raises; see the module docstring.

    The return is bytes rather than a path or a figure: the caller attaches it to a Discord
    message or writes it to a file, and a renderer that chose one of those for them would make the
    other awkward and the tests slower.
    """
    label = _timeframe_label(timeframe)
    kept = _window(bars, window)
    if len(kept) < MIN_BARS:
        return unavailable(
            symbol,
            label,
            f"insufficient data: {len(kept)} bar{'' if len(kept) == 1 else 's'}, "
            f"{MIN_BARS} needed",
        )

    try:
        future = _pool().submit(_draw, symbol, label, kept, spec, overlays or Overlays())
    except RuntimeError:  # pragma: no cover - the interpreter is shutting down
        log.warning("the chart pool is gone; drawing in the calling thread")
        return _safely(symbol, label, kept, spec, overlays or Overlays())

    try:
        return future.result(timeout=max(0.1, budget_seconds))
    except concurrent.futures.TimeoutError:
        # The worker is still drawing and cannot be stopped -- see the module docstring. The
        # caller gets an image now, which is what the budget is for.
        log.warning(
            "the %s %s chart exceeded its %.1fs budget", symbol, label, budget_seconds
        )
        return unavailable(symbol, label, f"chart timed out after {budget_seconds:.0f}s")
    except Exception:  # noqa: BLE001 - an absence is an image, never an exception
        log.exception("the %s %s chart could not be drawn", symbol, label)
        return unavailable(symbol, label, "chart unavailable")


def _safely(symbol, label, kept, spec, overlays) -> bytes:
    try:
        return _draw(symbol, label, kept, spec, overlays)
    except Exception:  # noqa: BLE001
        log.exception("the %s %s chart could not be drawn", symbol, label)
        return unavailable(symbol, label, "chart unavailable")


_POOL: concurrent.futures.ThreadPoolExecutor | None = None


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    """One bounded pool for the process.

    Bounded rather than a thread per call: the budget above does not stop a slow drawing, so a
    caller rendering faster than the machine draws would otherwise accumulate threads until the
    process fell over. Two workers, so one slow chart does not block a second reader, and no more,
    because drawing is CPU work and this process is also running a bot.
    """
    global _POOL
    if _POOL is None:
        _POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="chart"
        )
    return _POOL


def _window(bars: Sequence[ChartBar], window: int) -> tuple[ChartBar, ...]:
    """The last ``window`` bars, sorted, with the window itself clamped."""
    wanted = max(MIN_BARS, min(int(window), MAX_WINDOW))
    ordered = sorted(bars, key=lambda bar: bar.at)
    return tuple(ordered[-wanted:])


def _timeframe_label(timeframe: Any) -> str:
    return str(getattr(timeframe, "value", timeframe))


# ── the drawing ───────────────────────────────────────────────────────────────


def _figure(*, wide: bool = False):
    """A figure and its canvas, through the object API. Never ``pyplot`` -- see the docstring."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    size = (WIDE_WIDTH_INCHES, WIDE_HEIGHT_INCHES) if wide else (WIDTH_INCHES, HEIGHT_INCHES)
    figure = Figure(figsize=size, dpi=DPI)
    FigureCanvasAgg(figure)
    return figure


def _png(figure) -> bytes:
    buffer = io.BytesIO()
    # No metadata, so two renders of the same bars are byte-identical: matplotlib otherwise
    # stamps its own version into the PNG, which makes a golden comparison a comparison of
    # library versions.
    figure.savefig(buffer, format="png", metadata={"Software": None})
    return buffer.getvalue()


def unavailable(symbol: str, timeframe: str, reason: str) -> bytes:
    """The image an absence gets. Small, plain and legible at thumbnail size."""
    try:
        figure = _figure()
        axes = figure.add_subplot(111)
        axes.set_axis_off()
        axes.text(
            0.5,
            0.58,
            f"{symbol} {timeframe}",
            ha="center",
            va="center",
            fontsize=18,
            color="#444444",
        )
        axes.text(
            0.5, 0.42, reason, ha="center", va="center", fontsize=12, color="#888888"
        )
        return _png(figure)
    except Exception:  # noqa: BLE001 - there is nothing left to fall back to
        log.exception("even the unavailable image could not be drawn")
        return b""


#: The most decimals a price axis will ever be given, whatever the data says. Eight is past any
#: instrument this system trades and stops a float artefact from producing a wall of digits.
MAX_DECIMALS = 8

#: How many recent bars the fallback inspects. Enough to see the instrument's precision, few
#: enough that it costs nothing.
PRECISION_SAMPLE = 50


def _decimals(spec: Any | None, bars: Sequence[ChartBar]) -> int:
    """How many decimals a price is written with.

    From the broker's own ``digits`` where there is a spec. Gold quotes to two and silver to
    three, and rounding silver to gold's precision puts a level on the wrong tick -- which is
    exactly the thing a reader opens a chart to check.

    Without a spec, inferred from the PRICES THEMSELVES: the most decimals any recent quote
    actually carries. The first version of this inferred from the MAGNITUDE of the price -- under
    ten, four decimals; over ten, two -- which is wrong on silver, one of the two instruments this
    system trades: silver is about $31 and quotes to three. A heuristic that is wrong on half the
    portfolio is worse than no heuristic, because it is confidently wrong. The precision of the
    data is a real signal; its size is not.
    """
    digits = getattr(spec, "digits", None)
    if isinstance(digits, int) and 0 <= digits <= MAX_DECIMALS:
        return digits
    return min(MAX_DECIMALS, max((_places(bar) for bar in bars[-PRECISION_SAMPLE:]), default=2))


def _places(bar: ChartBar) -> int:
    """The most decimals any of one bar's four prices carries.

    ``repr`` gives the shortest string that round-trips, so ``31.005`` is three and ``31.0`` is
    one -- which is what "how precise is this quote" means for a float.
    """
    from decimal import Decimal

    found = 0
    for price in (bar.open, bar.high, bar.low, bar.close):
        exponent = Decimal(repr(float(price))).as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0:
            found = max(found, -exponent)
    return found


def _pad(series: Sequence[float | None], length: int) -> list[float | None]:
    """Align a series to the bars, padding on the LEFT. See ``Overlays``."""
    values = list(series)
    if len(values) >= length:
        return values[-length:]
    return [None] * (length - len(values)) + values


def _finite(values: Sequence[float | None]) -> list[float]:
    return [v for v in values if v is not None and math.isfinite(v)]


def _draw(
    symbol: str,
    label: str,
    bars: tuple[ChartBar, ...],
    spec: Any | None,
    overlays: Overlays,
) -> bytes:
    return _png(build(symbol, label, bars, spec, overlays))


def build(
    symbol: str,
    label: str,
    bars: tuple[ChartBar, ...],
    spec: Any | None,
    overlays: Overlays,
):
    """The figure, before it becomes bytes.

    Split out from ``_draw`` so the tests can ask what is ON the chart -- which annotations, which
    titles, which footer -- rather than only what its pixels hash to. A perceptual hash tells you
    the picture changed; it cannot tell you the invalidation line lost the words "not a stop".
    """
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FormatStrFormatter

    decimals = _decimals(spec, bars)
    figure = _figure()
    right_edge = 0.70 if overlays.analysis_lines else 0.88
    grid = figure.add_gridspec(
        PRICE_HEIGHT_RATIO + 1,
        1,
        hspace=0.06,
        left=0.06,
        right=right_edge,
        top=0.90,
        bottom=0.10,
    )
    price = figure.add_subplot(grid[:PRICE_HEIGHT_RATIO, 0])
    volume = figure.add_subplot(grid[PRICE_HEIGHT_RATIO, 0], sharex=price)

    xs = list(range(len(bars)))

    # ── the value area, first, so everything else sits on top of it ────────────
    if overlays.value_area is not None:
        low, high = sorted(overlays.value_area)
        price.axhspan(low, high, color=VALUE_AREA_COLOUR, alpha=0.10, zorder=0)
    if overlays.poc_price is not None:
        price.axhline(
            overlays.poc_price,
            color=VALUE_AREA_COLOUR,
            linestyle=":",
            linewidth=1.2,
            zorder=1,
        )

    # ── the candles ───────────────────────────────────────────────────────────
    body_width = 0.7
    for index, bar in enumerate(bars):
        colour = UP_COLOUR if bar.rising else DOWN_COLOUR
        price.vlines(index, bar.low, bar.high, color=colour, linewidth=0.9, zorder=2)
        bottom = min(bar.open, bar.close)
        height = abs(bar.close - bar.open)
        if height == 0:
            # A doji has no body. A zero-height rectangle draws nothing at all, so the open and
            # close get a line of their own rather than vanishing.
            price.hlines(
                bar.close,
                index - body_width / 2,
                index + body_width / 2,
                color=colour,
                linewidth=1.2,
                zorder=3,
            )
            continue
        price.add_patch(
            Rectangle(
                (index - body_width / 2, bottom),
                body_width,
                height,
                facecolor=colour,
                edgecolor=colour,
                linewidth=0.6,
                zorder=3,
            )
        )

    # ── the indicator lines, as they were computed elsewhere ──────────────────
    for series, colour, name in (
        (overlays.ema_fast, FAST_COLOUR, overlays.ema_fast_label),
        (overlays.ema_slow, SLOW_COLOUR, overlays.ema_slow_label),
    ):
        if not series:
            continue
        aligned = _pad(series, len(bars))
        if not _finite(aligned):
            continue
        price.plot(
            xs,
            [v if v is not None and math.isfinite(v) else float("nan") for v in aligned],
            color=colour,
            linewidth=1.3,
            label=name,
            zorder=4,
        )

    # ── the named levels, labelled in the right margin ────────────────────────
    for level in overlays.levels:
        price.axhline(
            level.price, color=LEVEL_COLOUR, linestyle="--", linewidth=0.9, zorder=2
        )
        price.annotate(
            level.label,
            xy=(1.005, level.price),
            xycoords=("axes fraction", "data"),
            fontsize=7,
            color=LEVEL_COLOUR,
            va="center",
        )

    if overlays.anchor_price is not None:
        price.axhline(
            overlays.anchor_price, color=ANCHOR_COLOUR, linewidth=1.4, zorder=5
        )
        price.annotate(
            f"anchor {overlays.anchor_price:.{decimals}f}",
            xy=(1.005, overlays.anchor_price),
            xycoords=("axes fraction", "data"),
            fontsize=7,
            color=ANCHOR_COLOUR,
            va="center",
        )
    if overlays.invalidation_price is not None:
        price.axhline(
            overlays.invalidation_price,
            color=INVALIDATION_COLOUR,
            linestyle="-.",
            linewidth=1.2,
            zorder=5,
        )
        price.annotate(
            # Named in full every time. A red dashed line at a round number under a candle chart
            # is read as a stop by everyone who has ever traded, and this one is not a stop.
            f"invalidation {overlays.invalidation_price:.{decimals}f} · not a stop",
            xy=(1.005, overlays.invalidation_price),
            xycoords=("axes fraction", "data"),
            fontsize=7,
            color=INVALIDATION_COLOUR,
            va="center",
        )

    # ── what happened, and when ───────────────────────────────────────────────
    times = [bar.at for bar in bars]
    for mark in overlays.detections:
        index = _nearest(times, mark.at)
        if index is None:
            continue
        marker = (
            "^"
            if mark.direction_context == "bullish"
            else "v"
            if mark.direction_context == "bearish"
            else "o"
        )
        price.scatter(
            [index],
            [mark.price],
            marker=marker,
            s=46,
            color=ANCHOR_COLOUR,
            zorder=6,
        )
        if mark.label:
            y_offset = 12 if mark.direction_context != "bearish" else -18
            price.annotate(
                mark.label,
                xy=(index, mark.price),
                xytext=(4, y_offset),
                textcoords="offset points",
                fontsize=6.5,
                color=ANCHOR_COLOUR,
                zorder=7,
                bbox=dict(
                    boxstyle="round,pad=0.14",
                    fc="white",
                    ec=ANCHOR_COLOUR,
                    alpha=0.82,
                ),
            )
    for mark in overlays.events:
        index = _nearest(times, mark.at)
        if index is None:
            continue
        price.scatter(
            [index], [mark.price], marker="o", s=18, color=FAST_COLOUR, zorder=6
        )
        if mark.label and overlays.analysis_lines:
            price.annotate(
                mark.label.replace("_", " "),
                xy=(index, mark.price),
                xytext=(4, 8),
                textcoords="offset points",
                fontsize=6.5,
                color=ANCHOR_COLOUR,
                rotation=18,
                zorder=7,
            )

    # ── the right-side analysis panel ──────────────────────────────────────────
    if overlays.analysis_lines:
        figure.text(
            0.735,
            0.885,
            "MARKET CONTEXT",
            fontsize=11,
            fontweight="bold",
            color="#222222",
            va="top",
        )
        figure.text(
            0.735,
            0.845,
            "\n".join(overlays.analysis_lines),
            fontsize=9,
            color="#333333",
            va="top",
            linespacing=1.55,
            family="monospace",
        )

    # ── the tick-volume panel, named honestly ─────────────────────────────────
    volume.bar(
        xs,
        [bar.tick_volume for bar in bars],
        width=body_width,
        color=[UP_COLOUR if bar.rising else DOWN_COLOUR for bar in bars],
        alpha=0.55,
    )
    volume.set_ylabel(VOLUME_LABEL, fontsize=6, color="#666666")
    volume.tick_params(axis="y", labelsize=6)
    volume.grid(axis="y", color=GRID_COLOUR, linewidth=0.5)

    # ── the frame ─────────────────────────────────────────────────────────────
    price.set_title(overlays.title or f"{symbol} {label}", fontsize=12, loc="left")
    if overlays.subtitle:
        price.set_title(overlays.subtitle, fontsize=8, loc="right", color="#666666")
    price.yaxis.set_major_formatter(FormatStrFormatter(f"%.{decimals}f"))
    price.grid(color=GRID_COLOUR, linewidth=0.5)
    price.tick_params(axis="x", labelbottom=False)
    price.tick_params(axis="y", labelsize=8)
    if overlays.ema_fast or overlays.ema_slow:
        price.legend(loc="upper left", fontsize=7, framealpha=0.8)

    ticks, labels = _time_ticks(times)
    volume.set_xticks(ticks)
    volume.set_xticklabels(labels, fontsize=7)
    volume.set_xlim(-1, len(bars))

    footer = " · ".join((*overlays.notes, FOOTER_NOTE))
    figure.text(
        0.06 if overlays.analysis_lines else 0.07,
        0.015,
        footer,
        fontsize=7,
        color="#888888",
    )
    return figure


def _nearest(times: Sequence[datetime], at: datetime) -> int | None:
    """The index of the bar a mark belongs to, or ``None`` if it is outside the window.

    ``None`` rather than clamping to the first or last bar: a detection from before the window
    drawn on its first candle is a false statement about when it happened, and one a reader
    cannot check because the bar is right there on the screen.
    """
    for index, moment in enumerate(times):
        if moment == at:
            return index
    if not times or at < times[0] or at > times[-1]:
        return None
    # Between two bars (a mark stamped at a close rather than an open): the bar it fell inside.
    for index in range(len(times) - 1):
        if times[index] <= at < times[index + 1]:
            return index
    return len(times) - 1


def _time_ticks(times: Sequence[datetime], *, wanted: int = 6) -> tuple[list[int], list[str]]:
    """At most ``wanted`` x labels, in UTC, as HH:MM -- or with the date when the window spans one.

    UTC, not the market timezone: every other timestamp a reader compares this against
    (``detected_at.utc``, a log line, an ops event) is UTC, and a chart in Athens time would make
    them line up wrongly by an hour twice a year.
    """
    if not times:
        return [], []
    step = max(1, len(times) // wanted)
    indices = list(range(0, len(times), step))
    spans_days = times[0].date() != times[-1].date()
    fmt = "%m-%d %H:%M" if spans_days else "%H:%M"
    return indices, [times[i].strftime(fmt) for i in indices]


__all__ = [
    "CHART_BUDGET_SECONDS",
    "DEFAULT_WINDOW",
    "FOOTER_NOTE",
    "MAX_DECIMALS",
    "MAX_WINDOW",
    "MIN_BARS",
    "VOLUME_LABEL",
    "ChartBar",
    "ChartLevel",
    "ChartMark",
    "Overlays",
    "bars_for",
    "build",
    "from_candles",
    "from_frames",
    "render",
    "unavailable",
]
