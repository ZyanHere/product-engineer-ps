"""Shared test fixtures.

The determinism seams live here as pytest fixtures: a controllable clock, a
sequential id generator, and limit builders. Tests never touch the network, never
construct the real model adapter, and never require an API key.

Populated in Step 1.5, once the seams exist in agent_loop.agent.state.
"""
