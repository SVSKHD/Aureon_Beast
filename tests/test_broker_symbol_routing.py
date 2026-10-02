from __future__ import annotations

from aureon.config import AureonConfig


def _base_env() -> dict[str, str]:
    return {
        "AUREON_SYMBOLS": "XAUUSD,XAGUSD",
        "AUREON_EVAL_RULES": "XAUUSD:XAU_OUTCOME_V2,XAGUSD:XAG_OUTCOME_V1",
    }


def test_mt5_symbol_flags_can_disable_silver_independently() -> None:
    env = _base_env() | {
        "AUREON_BROKER_SOURCE": "MT5",
        "AUREON_MT5_GOLD_ENABLED": "true",
        "AUREON_MT5_SILVER_ENABLED": "false",
    }
    config = AureonConfig.from_env(env=env)
    assert config.symbols == ("XAUUSD",)


def test_mt5_symbol_flags_can_enable_gold_and_silver() -> None:
    env = _base_env() | {
        "AUREON_BROKER_SOURCE": "MT5",
        "AUREON_MT5_GOLD_ENABLED": "true",
        "AUREON_MT5_SILVER_ENABLED": "true",
    }
    config = AureonConfig.from_env(env=env)
    assert config.symbols == ("XAUUSD", "XAGUSD")


def test_missing_flags_preserve_existing_symbol_configuration() -> None:
    config = AureonConfig.from_env(env=_base_env())
    assert config.symbols == ("XAUUSD", "XAGUSD")


def test_broker_specific_discord_channel_routes_mt5() -> None:
    env = _base_env() | {
        "AUREON_BROKER_SOURCE": "MT5",
        "AUREON_MT5_ALERT_CHANNEL_ID": "111",
        "AUREON_CTRADER_ALERT_CHANNEL_ID": "222",
    }
    config = AureonConfig.from_env(env=env)
    assert config.broker_alert_channel_id == 111


def test_broker_specific_discord_channel_routes_ctrader() -> None:
    env = _base_env() | {
        "AUREON_BROKER_SOURCE": "CTRADER",
        "AUREON_MT5_ALERT_CHANNEL_ID": "111",
        "AUREON_CTRADER_ALERT_CHANNEL_ID": "222",
    }
    config = AureonConfig.from_env(env=env)
    assert config.broker_alert_channel_id == 222


def test_legacy_alert_channel_remains_fallback() -> None:
    env = _base_env() | {
        "AUREON_BROKER_SOURCE": "MT5",
        "AUREON_ALERT_CHANNEL_ID": "333",
    }
    config = AureonConfig.from_env(env=env)
    assert config.broker_alert_channel_id == 333
