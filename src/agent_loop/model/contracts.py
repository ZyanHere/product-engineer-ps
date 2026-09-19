"""The model contract.

Holds the ModelClient protocol, ModelRequest, ModelResponse, ModelUsage, the
decision models, and ModelCallError.

ModelResponse.decision is the single untrusted field in the system: adapters
translate shape, the loop validates contract.

Must NOT contain: implementations.
"""
