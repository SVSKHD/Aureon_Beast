"""Discord-ready V4 journey intelligence cards (TODO 093-097).

Formatting only. Inputs are frozen V4 observations/model outputs; this module does
not calculate market truth and never emits execution instructions.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from aureon.models.ema_journey_v4 import V4PullbackState, V4ReentryObservation
from aureon.services.v4_specialist_similarity import V4SimilarJourney, summarize_similar_outcomes

@dataclass(frozen=True)
class V4DiscordCard:
    title: str
    description: str
    fields: tuple[tuple[str, str], ...]
    footer: str = "V4 journey intelligence · observation only"

def journey_thread_key(journey_id: str) -> str:
    """TODO 093: stable key so all V4 updates reuse one journey thread."""
    return f"v4_journey:{journey_id}"

def remaining_move_card(symbol: str, direction: str, probabilities: dict[str, float], *, expected_mfe: float | None = None, expected_mae: float | None = None, uncertainty: float | None = None, similarity_confidence: float | None = None) -> V4DiscordCard:
    """TODO 094: concise remaining-movement view for one-glance reading."""
    targets=[]
    for target in (3,5,10,20,30,40):
        key=f"reached_{target}"
        if key in probabilities:
            targets.append(f"+{target} {float(probabilities[key]):.0%}")
    fields=[("Remaining move", " · ".join(targets) or "—")]
    if expected_mfe is not None or expected_mae is not None:
        fields.append(("Excursion", f"MFE {expected_mfe:.2f}" if expected_mfe is not None else "MFE —" + (f" · MAE {expected_mae:.2f}" if expected_mae is not None else "")))
    reliability=[]
    if uncertainty is not None:
        reliability.append(f"uncertainty {uncertainty:.0%}")
    if similarity_confidence is not None:
        reliability.append(f"history confidence {similarity_confidence:.0%}")
    if reliability:
        fields.append(("Reliability", " · ".join(reliability)))
    return V4DiscordCard(f"{symbol} · {direction.upper()} · V4 REMAINING MOVE","Probability view from the current frozen journey anchor.",tuple(fields))

def pullback_observation_card(pullback: V4PullbackState) -> V4DiscordCard:
    """TODO 095: compact pullback state, depth, retests and structure."""
    retests=", ".join(item.value.replace("_retest","").upper() for item in pullback.retests) or "none"
    fields=(
        ("Pullback", f"{pullback.classification.value.replace('_pullback','')} · depth {pullback.depth:.2f} · {pullback.retracement_fraction:.0%} retrace"),
        ("Structure", "intact" if pullback.structure_intact else "broken"),
        ("EMA alignment", "aligned" if pullback.ema_aligned else "not aligned"),
        ("Retests", retests),
    )
    return V4DiscordCard(f"{pullback.symbol} · PULLBACK OBSERVATION","Expansion has retraced; Aureon is observing the journey, not issuing an entry.",fields)

def reentry_observation_card(row: V4ReentryObservation) -> V4DiscordCard:
    """TODO 096: explicitly label re-entry as observation, never BUY/SELL."""
    fields=(
        ("Label", "RE-ENTRY OBSERVATION · research only"),
        ("Price observed", f"{row.price:.2f}"),
        ("Pullback", f"depth {row.pullback_depth:.2f} · {row.pullback_fraction:.0%}"),
        ("Structure / EMA", f"{'intact' if row.structure_intact else 'broken'} · {'aligned' if row.ema_aligned else 'not aligned'}"),
    )
    return V4DiscordCard(f"{row.symbol} · RE-ENTRY OBSERVATION","Continuation conditions were observed after a pullback. No order is requested.",fields)

def similar_journey_evidence_card(symbol: str, matches: list[V4SimilarJourney]) -> V4DiscordCard:
    """TODO 097: concise historical-neighbour evidence with sample count."""
    summary=summarize_similar_outcomes(matches)
    n=int(summary.get("samples",0))
    if not n:
        body="No sufficiently similar resolved journeys are available."
        fields=(("Historical evidence","n=0"),)
    else:
        body="Nearest resolved V4 journeys using frozen observable features."
        fields=(
            ("Historical evidence",f"n={n} · mean similarity {float(summary['mean_similarity']):.0%}"),
            ("Outcomes",f"+3 {float(summary['reach_3_rate']):.0%} · +5 {float(summary['reach_5_rate']):.0%} · +10 {float(summary['reach_10_rate']):.0%}"),
            ("Excursion",f"mean MFE {float(summary['mean_remaining_mfe']):.2f} · mean MAE {float(summary['mean_remaining_mae']):.2f}"),
        )
    return V4DiscordCard(f"{symbol} · SIMILAR JOURNEYS",body,fields)

def render_card_text(card: V4DiscordCard) -> str:
    """Transport-neutral rendering used by Discord adapters and tests."""
    lines=[card.title,card.description]
    lines.extend(f"{name}: {value}" for name,value in card.fields)
    lines.append(card.footer)
    return "\n".join(lines)
