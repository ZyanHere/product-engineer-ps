"""Deterministic model for tests and the canonical demo path.

Records every ModelRequest it receives, which is how tests assert exact call
counts and observe the shrinking tool catalogue.

Must NOT know about: tools or the trace.
"""
