"""Run state, model-visible turns, and the projection between them.

Holds RunState, Turn, Clock, IdGen, project() and to_model_turn().
RunState is JSON-safe by construction so checkpointing needs no new machinery.

Must NOT own: transition policy - the loop owns state transitions.
"""
