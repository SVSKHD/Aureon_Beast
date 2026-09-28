from aureon.discord.orion_v2 import OrionV2Context, orion_v2_embed


def test_orion_v2_context_card_renders_deterministic_briefing():
    card = OrionV2Context(
        symbol="XAUUSD",
        timeframe="M5",
        direction="bullish setup",
        state="pullback → re-entry",
        price=3765.40,
        setup_zone="3762–3766",
        ema_m5="Bullish",
        trend_m15="Bullish",
        trend_h1="Bullish",
        trend_h4="Neutral",
        rsi="57 · Rising",
        session="London",
        regime="Trending",
        volume="Strong",
        liquidity="Sweep detected",
        wick_rejection="Confirmed",
        structure="Intact",
        progress=(("EMA Cross", True), ("Pullback", True), ("Re-entry", None)),
        move_levels=(("+$5", "3770.40"), ("+$10", "3775.40"), ("+$20", "3785.40")),
        invalidation="Below 3758.20 → bullish structure broken",
        status="WATCHING",
        next_step="M5 continuation close above 3766.50 → CONFIRMED",
        updated="12:01 IST",
    )

    embed = orion_v2_embed(card)
    payload = embed.to_dict()
    assert payload["title"] == "ORION V2 • XAUUSD • M5"
    assert "PULLBACK → RE-ENTRY" in payload["description"]
    fields = {field["name"]: field["value"] for field in payload["fields"]}
    assert "⏳ Re-entry" in fields["SETUP PROGRESS"]
    assert "+$20" in fields["MOVE MAP"]
    assert "does not execute trades" in fields["ORION STATUS · WATCHING"]


def test_orion_v2_module_does_not_replace_existing_setup_embed():
    from aureon.discord.embeds import setup_embed

    assert callable(setup_embed)
    assert callable(orion_v2_embed)
    assert setup_embed is not orion_v2_embed
