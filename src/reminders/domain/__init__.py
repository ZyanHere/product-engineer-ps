"""Domain: pure logic with no dependencies at all.

Nothing in this package may import a port, an adapter, a clock, or a store.
That is not a layering preference -- it is what makes several invariants
structural rather than remembered:

  * the local-time resolver's signature accepts **no clock**, so a resolution
    cannot be time-dependent even by accident (I-13, Phase 6);
  * the failure classifier is a total function over a closed union, so a new
    outcome cannot be silently ignored (Phase 7);
  * the backoff policy is computed from durable state only, never from a timer
    (Phase 7).

ARCHITECTURE section 2.4 draws this box with no outgoing edges. Keep it that
way: a single import of `Clock` in here would turn a compile-time guarantee
back into a code-review convention.
"""
