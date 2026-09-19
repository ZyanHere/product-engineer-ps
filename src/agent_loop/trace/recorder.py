"""The TraceEvent union and the recorder: the single emit chokepoint.

Every event passes through deepcopy -> redact -> truncate -> assign seq/ts/run_id
-> append. One doorway means ordering, immutability, redaction and size bounds
are enforced once and cannot be bypassed.

Append-only is enforced by the absence of any other method, not by convention.

Must NOT own: event interpretation or filtering.
"""
