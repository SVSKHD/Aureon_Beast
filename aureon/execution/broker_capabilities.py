"""Pre-flight validation against the broker's own rules (§39, §42).

Everything here answers "will the broker accept this?" *before* the order is sent. The
point is not politeness: a rejection after a human has confirmed is opaque and
alarming, and an order the broker silently reshapes is worse still.

The volume rule is the sharpest one. A volume off the broker's step is **rejected, not
rounded** (§42, CLAUDE.md): rounding 0.15 down to 0.10 executes a trade the human did
not ask for, and rounding up risks more of their money than they authorised. Neither is
ours to choose.
"""

from __future__ import annotations

from dataclasses import dataclass

from aureon.models.enums import FailureCode, FillingMode, OrderType
from aureon.models.market import SymbolInfo


@dataclass(frozen=True)
class CapabilityCheck:
    """Whether the broker will accept something, and why not if it will not."""

    ok: bool
    failure_code: FailureCode | None = None
    message: str | None = None

    @classmethod
    def passed(cls) -> CapabilityCheck:
        return cls(ok=True)

    @classmethod
    def failed(cls, code: FailureCode, message: str) -> CapabilityCheck:
        return cls(ok=False, failure_code=code, message=message)


def check_volume(info: SymbolInfo, volume: float) -> CapabilityCheck:
    """Validate a volume against the broker's min/max/step (§42)."""
    if volume <= 0:
        return CapabilityCheck.failed(
            FailureCode.VOLUME_INVALID, f"volume must be positive, got {volume}"
        )
    try:
        info.normalize_volume(volume)
    except ValueError as exc:
        return CapabilityCheck.failed(FailureCode.VOLUME_INVALID, str(exc))
    return CapabilityCheck.passed()


def check_filling_mode(info: SymbolInfo, mode: FillingMode | None) -> CapabilityCheck:
    """Validate a filling mode against what the symbol supports (§39).

    ``None`` means "let the broker choose", which is always acceptable. Phase 6 builds
    its selector from ``filling_modes`` so a human is never offered FOK on a symbol
    that rejects it -- this is the backstop for a request built any other way.
    """
    if mode is None:
        return CapabilityCheck.passed()
    if not info.filling_modes:
        # The broker reported nothing. Failing closed here would block every trade on a
        # symbol whose metadata is merely incomplete, so an explicit mode is allowed
        # through and the broker itself becomes the arbiter.
        return CapabilityCheck.passed()
    if not info.supports(mode):
        supported = ", ".join(m.value for m in info.filling_modes) or "none reported"
        return CapabilityCheck.failed(
            FailureCode.FILLING_MODE_UNSUPPORTED,
            f"{info.symbol} does not support {mode.value} (supports: {supported})",
        )
    return CapabilityCheck.passed()


def check_stops(
    info: SymbolInfo,
    *,
    order_type: OrderType,
    price: float | None,
    sl: float | None,
    tp: float | None,
    reference_price: float,
) -> CapabilityCheck:
    """Validate stop and target distances, and their side (§42).

    Two distinct failures, kept distinct because they mean different things to whoever
    reads the embed:

    * ``INVALID_STOPS`` -- the stop is on the *wrong side* of the entry. A buy whose
      stop-loss sits above entry is not a tight stop, it is a mistake.
    * ``STOPS_TOO_CLOSE`` -- the side is right but the distance is inside the broker's
      ``stops_level``, so the broker will reject it.
    """
    entry = price if price is not None else reference_price
    is_buy = order_type.direction.value == "buy"
    minimum = info.min_stop_distance

    if sl is not None:
        if is_buy and sl >= entry:
            return CapabilityCheck.failed(
                FailureCode.INVALID_STOPS,
                f"BUY stop-loss {sl} must be below entry {entry}",
            )
        if not is_buy and sl <= entry:
            return CapabilityCheck.failed(
                FailureCode.INVALID_STOPS,
                f"SELL stop-loss {sl} must be above entry {entry}",
            )
        if minimum and abs(entry - sl) < minimum:
            return CapabilityCheck.failed(
                FailureCode.STOPS_TOO_CLOSE,
                f"stop-loss is {abs(entry - sl):.5f} from entry; broker requires "
                f"{minimum:.5f} ({info.stops_level} points)",
            )

    if tp is not None:
        if is_buy and tp <= entry:
            return CapabilityCheck.failed(
                FailureCode.INVALID_STOPS,
                f"BUY take-profit {tp} must be above entry {entry}",
            )
        if not is_buy and tp >= entry:
            return CapabilityCheck.failed(
                FailureCode.INVALID_STOPS,
                f"SELL take-profit {tp} must be below entry {entry}",
            )
        if minimum and abs(tp - entry) < minimum:
            return CapabilityCheck.failed(
                FailureCode.STOPS_TOO_CLOSE,
                f"take-profit is {abs(tp - entry):.5f} from entry; broker requires "
                f"{minimum:.5f} ({info.stops_level} points)",
            )

    return CapabilityCheck.passed()


def check_pending_entry(
    info: SymbolInfo,
    *,
    order_type: OrderType,
    price: float | None,
    reference_price: float,
) -> CapabilityCheck:
    """Validate a pending order's entry against the market (§42).

    A BUY_STOP must sit *above* the market and a BUY_LIMIT *below* it; placing one on
    the wrong side is either rejected or, worse, fills instantly at a price the human
    never intended. The broker's ``stops_level`` applies to the distance as well.
    """
    if order_type.is_market:
        return CapabilityCheck.passed()
    if price is None:
        return CapabilityCheck.failed(
            FailureCode.INVALID_STOPS, f"{order_type.value} requires an entry price"
        )

    above = {OrderType.BUY_STOP, OrderType.SELL_LIMIT}
    below = {OrderType.SELL_STOP, OrderType.BUY_LIMIT}
    if order_type in above and price <= reference_price:
        return CapabilityCheck.failed(
            FailureCode.INVALID_STOPS,
            f"{order_type.value} entry {price} must be above the market {reference_price}",
        )
    if order_type in below and price >= reference_price:
        return CapabilityCheck.failed(
            FailureCode.INVALID_STOPS,
            f"{order_type.value} entry {price} must be below the market {reference_price}",
        )

    minimum = info.min_stop_distance
    if minimum and abs(price - reference_price) < minimum:
        return CapabilityCheck.failed(
            FailureCode.STOPS_TOO_CLOSE,
            f"entry is {abs(price - reference_price):.5f} from the market; broker requires "
            f"{minimum:.5f} ({info.stops_level} points)",
        )
    return CapabilityCheck.passed()
