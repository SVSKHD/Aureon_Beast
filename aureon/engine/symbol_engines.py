"""One analysis engine per symbol, dispatched by the candle's own symbol (9A).

``AnalysisEngine`` already keys its windows and its cross counters by
``(symbol, timeframe)``, so a single engine would keep the right *history* for two
symbols. What it cannot do is keep the right *parameters*: its agents are constructed
with one symbol's thresholds, and those thresholds are not dimensionless.
``min_penetration_points = 5`` is $0.05 on gold and 0.625 of a tick on silver (decision
143), so one shared roster would measure one instrument with the other's numbers — and
nothing downstream would say so, because a sweep is a sweep once it is written.

So: one engine, one roster and one ``LevelTracker`` per symbol, and a dispatcher that
routes each candle by the symbol it carries rather than by which stream the caller thought
it was polling.

## It refuses a symbol it was not built for

Loudly, rather than silently picking a roster. The provider only ever serves configured
symbols, so an unconfigured one arriving here means the wiring is wrong — and the quiet
alternative is measuring it with whatever roster happened to be first, which is the exact
failure this class exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.detection import Detection
from aureon.models.market import Candle


class SymbolEngines:
    """A read-only mapping of symbol to its own engine, plus the dispatch."""

    def __init__(self, engines: Mapping[str, AnalysisEngine]) -> None:
        if not engines:
            raise ValueError("SymbolEngines needs at least one engine")
        self._engines: dict[str, AnalysisEngine] = {
            symbol.upper(): engine for symbol, engine in engines.items()
        }

    # ── Access ────────────────────────────────────────────────────────────────

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._engines)

    def for_symbol(self, symbol: str) -> AnalysisEngine:
        try:
            return self._engines[symbol.upper()]
        except KeyError:
            raise KeyError(
                f"no analysis engine for {symbol}; this process observes "
                f"{', '.join(self._engines) or 'nothing'}. Refusing to measure it with "
                "another symbol's thresholds -- they are not dimensionless."
            ) from None

    def __len__(self) -> int:
        return len(self._engines)

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and symbol.upper() in self._engines

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def on_closed_candle(self, candle: Candle) -> list[Detection]:
        """Route one candle to its symbol's engine. The MarketEngine's whole interface."""
        return self.for_symbol(candle.symbol).on_closed_candle(candle)

    def feed(self, candles: Iterable[Candle]) -> list[Detection]:
        """Replay a mixed stream in the order given, routing each candle.

        In the order given, deliberately: two symbols' candles interleave in time, and
        re-ordering them per symbol would evaluate each against a clock the other never
        saw.
        """
        produced: list[Detection] = []
        for candle in candles:
            produced.extend(self.on_closed_candle(candle))
        return produced

    def reset(self) -> None:
        for engine in self._engines.values():
            engine.reset()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        shape = ", ".join(
            f"{symbol}:window={engine.window_size}"
            for symbol, engine in self._engines.items()
        )
        return f"SymbolEngines({shape})"
