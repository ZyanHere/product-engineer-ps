"""Limits, the run deadline, and the single limit-checking authority.

Holds Limits, Deadline, check(), remaining_ms() and with_deadline().

Must NOT own: the decision about what to do when a limit is exhausted.
"""
