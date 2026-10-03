# Aureon — cTrader native Python cBot

This folder is the cTrader-native entry point for Aureon. It is intentionally separate
from `main_aureon.py` (MT5) and from the external Open API experiment.

## Current milestone

The cBot lifecycle and broker boundary are live:

- `on_start` announces Aureon startup to the cTrader Discord channel.
- `on_bar_closed` normalizes cTrader closed bars for the Aureon observation boundary.
- `on_stop` and `on_exception` report lifecycle changes.
- There are **no order, position, execution, modify or close calls**.
- Run one instance on **XAUUSD M5** and another on **XAGUSD M5**. They are independent,
  so Silver never waits for Gold.

The existing Aureon Discord process remains the owner of slash commands. Do not create
a second command implementation inside this cBot.

## Local configuration

Set these in the local environment used by the cTrader instance:

```text
AUREON_CTRADER_DISCORD_TOKEN=<cTrader Discord bot token>
AUREON_CTRADER_ALERT_CHANNEL_ID=<cTrader channel id>
```

Never commit the token.

## cTrader setup

1. Open **Algo** in cTrader.
2. Create a new **Python cBot** named `Aureon`.
3. Put `main.py` and `requirements.txt` in that cBot project.
4. Build it.
5. Add a local instance to **XAUUSD / M5**.
6. Add another local instance to **XAGUSD / M5**.
7. Start both instances.
8. Confirm the startup greeting appears in the cTrader Discord channel.

## Next mapping layer

`_observe()` is the deliberate adapter boundary. The next change maps normalized
cTrader candles into Aureon's existing deterministic agents, V4 journey tracking,
pullback/re-entry intelligence, learning memory and status publication. The agent logic
should be reused, not independently reimplemented for cTrader.
