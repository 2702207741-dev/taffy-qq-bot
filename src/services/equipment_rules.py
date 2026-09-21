"""Single source of the equipment-index candidate rules shared by the
production path (``market_history.MarketHistoryStore``) and the rebase
rehearsal tool (``scripts/prepare_equipment_rebase.py``).

Only rules that are literally implemented in BOTH places live here, so a
fix cannot drift between them.  Pure constants only — importing this module
never initialises a store, database or business task.

Rule summary (matches the v0.5.0 rehearsal):
- ranking: hourly-bucket count DESC over the trailing window, then latest
  bucket DESC, then market name COLLATE BINARY ASC — a deterministic total
  order;
- membership is fixed by the full sort FIRST (LIMIT target), prices are
  validated AFTER; an invalid or aged quote never pulls in a later row;
- a fallback quote (used only when the cutoff bucket B has no valid price)
  must come from a bucket no older than 24h before B and must be a finite
  positive number.
"""

EQUIPMENT_ORDER_BY = "ORDER BY buckets DESC, last_bucket DESC, market_name COLLATE BINARY ASC"

#: Fallback-quote age gate: 0 <= B - source_bucket <= 86400 seconds.
EQUIPMENT_FALLBACK_MAX_AGE_SECONDS = 86400
