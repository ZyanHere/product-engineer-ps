"""THE control loop. The only orchestration authority in the system.

Owns sequencing, both budget gates, decision handling, and termination.
Readable top to bottom in one function, by design.

Must NOT own: provider formats, tool internals, or string rendering.
"""
