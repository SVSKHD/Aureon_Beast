"""M5-first execution gate rendering."""

from types import SimpleNamespace

from aureon.discord.service import _m5_execution_gate_lines
from aureon.models.agent_decision import DirectorState, HtfState
from aureon.models.enums import Direction, Timeframe


def _state(*, director_state=DirectorState.READY, direction=Direction.BUY, htf_state=HtfState.BULLISH):
    return SimpleNamespace(
        timeframe=Timeframe.M5,
        market_director=SimpleNamespace(
            state=director_state,
            direction=direction,
            trigger_required=None,
        ),
        higher_timeframe_agent=SimpleNamespace(state=htf_state),
    )


def test_m5_ready_and_htf_aligned_is_aligned() -> None:
    lines = _m5_execution_gate_lines(_state())
    assert lines[0].startswith("M5 execution ALIGNED")
    assert "M15/H1/H4 support" in lines[0]


def test_m5_ready_with_mixed_htf_is_caution() -> None:
    lines = _m5_execution_gate_lines(_state(htf_state=HtfState.MIXED))
    assert lines[0].startswith("M5 execution CAUTION")


def test_m5_ready_with_opposite_htf_is_blocked() -> None:
    lines = _m5_execution_gate_lines(
        _state(direction=Direction.BUY, htf_state=HtfState.BEARISH)
    )
    assert lines[0].startswith("M5 execution BLOCKED")


def test_non_ready_director_blocks_immediate_execution() -> None:
    lines = _m5_execution_gate_lines(_state(director_state=DirectorState.FORMING))
    assert lines[0].startswith("M5 execution BLOCKED")
    assert "forming" in lines[0]


def test_non_m5_state_is_not_an_execution_lane() -> None:
    state = _state()
    state.timeframe = Timeframe.M15
    lines = _m5_execution_gate_lines(state)
    assert lines[0].startswith("M5 execution BLOCKED")
