"""Stateless tool catalogue: construction, duplicate check, lookup, specs().

Availability lives in RunState, not here, so the whole run stays one
serializable object.

Must NOT own: availability state or execution.
"""
