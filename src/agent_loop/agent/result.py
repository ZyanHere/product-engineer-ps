"""Evidence derivation, grounding validation, and final assembly.

Holds build_evidence(), validate_final(), derive_observed_gaps(), finalize()
and RunResult. Evidence is a projection of the trace, never a second store.

Must NEVER call the model or dispatch a tool. That impossibility is what makes
the execution-limit guarantee airtight rather than merely intended.
"""
