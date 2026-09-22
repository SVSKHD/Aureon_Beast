"""Local PostgreSQL persistence (plan §4, §5).

The application truth from Phase 13 onward. MT5 stays broker truth, parquet stays the
candle archive, the SQLite outbox stays local durability; this package is the one that
holds everything a service asks a question of.

Split three ways on purpose:

* ``database`` -- the engine, the transaction boundary and the startup wait. The only
  place a connection is opened.
* ``models`` -- the declarative base and the column types every table shares, including
  the one that refuses a naive timestamp.
* ``repositories`` -- one module per domain, arriving in the order the plan sets (S-3).

Nothing outside ``aureon/storage`` may import SQLAlchemy or psycopg, and a boundary test
enforces it: the point of a storage package is that the rest of the system cannot tell
what is underneath it.
"""

from __future__ import annotations
