"""Adapters: concrete implementations of the ports in `reminders.ports`.

Everything in this package is replaceable. Nothing in `reminders.domain` or
`reminders.app` may import from here directly -- they depend on the port
protocols, and the composition root wires a concrete adapter in.
"""
