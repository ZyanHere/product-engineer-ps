"""The tool contract.

Holds the Tool protocol, ToolContext, ToolOutcome and MAX_SUMMARY_CHARS.

Tool implementations MUST be stateless. Immutable configuration fixed at
construction time satisfies this; mutable state across calls does not.
"""
