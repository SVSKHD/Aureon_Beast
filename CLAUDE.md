# Aureon — standing rules

Read docs/ARCHITECTURE.md (frozen, §94), docs/CONTRACTS.md and docs/PHASE1_DECISIONS.md before any change.

## Non-negotiables
- A detection never creates a trade. No code path may call the broker from the observer, agents, engine, evaluation or reviews packages. A test greps for this.
- Execution only from TradeRequestStatus.CONFIRMED, via a Firestore transaction that sets the executor lease. Unknown broker result → reconcile, never retry.
- All state changes go through aureon.models.enums.assert_transition. Never write a status field without it.
- Money-moving logic depends on aureon.execution.broker_interface.BrokerInterface, never on MetaTrader5 directly. Only aureon/data/mt5_provider.py and aureon/execution/mt5_broker.py may import MetaTrader5.
- Every timestamp is tz-aware. Use MarketTime for stored records. Never datetime.now() without tz; never local OS time.
- Detections are immutable. Outcomes go in detection_evaluations. Never add a field to Detection that encodes future information.
- Firestore writes go through aureon/storage repositories only. No raw client calls elsewhere. Tick data never goes to Firestore.
- Discord never calls the broker and never computes indicators. It reads Firestore and writes only trade_requests, settings.trading_enabled and audit_logs.

## Working rules
- Add models only in aureon/models; regenerate docs/CONTRACTS.md (python scripts/gen_contracts.py) and commit it.
- Every new decision resolving spec ambiguity → append a row to docs/PHASE1_DECISIONS.md (rename to DECISIONS.md if you like) with the spec §.
- Tests before service loops. Each phase ends with its acceptance test green; do not proceed on partial passes.
- Python 3.11+, pydantic v2, pytest. Windows-only libraries (MetaTrader5) are imported lazily inside the two permitted modules so the suite runs on any OS with a fake.
- Never commit secrets. Discord token, Firebase service account path, MT5 login/password/server come from environment variables listed in .env.example.
- Keep responses lean: what changed, what was tested, what remains.
