"""One module per domain, arriving in the order plan S-3 sets.

The order is not arbitrary: detections first because everything else references one,
evaluations next because they are the first thing that has to be idempotent over a
composite key, then setups and their events -- the first atomic transaction -- and only
then ``trade_requests``, where the claim has to be exactly-once before anything else can
be trusted.

Empty until S-3. The package exists now so the boundary test that forbids SQLAlchemy
outside ``aureon/storage`` has the shape it will be enforcing against, and so the first
repository is a file added rather than a directory invented.
"""

from __future__ import annotations
