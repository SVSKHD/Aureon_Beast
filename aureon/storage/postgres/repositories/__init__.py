"""One module per domain, arriving in the order plan S-3 sets.

The order was not arbitrary: detections first because everything else references one,
evaluations next because they are the first thing that has to be idempotent over a
composite key, then setups and their events -- the first atomic transaction -- and only
then ``trade_requests``, where the claim has to be exactly-once before anything else can
be trusted. S-3b adds the rest: the small current-answer tables, the alerts with their own
race, the measurements, the reviews, the sessions and the broker-day cache.

Nothing here is imported for its side effects, so a module is added to this package by
being written rather than by being registered. What holds the set together is
``PostgresRepository`` in ``base.py``: a repository holds a ``Database``, never a
connection, and every write is an upsert at a deterministic id unless the record is a fact
rather than a state, in which case it is an insert that refuses a duplicate.

The method names match the Firestore repositories they replace, deliberately. S-4 swaps
the object a service is constructed with and changes nothing else, so a behavioural
difference in that step can only come from the repository -- which is where the tests are.
"""

from __future__ import annotations
