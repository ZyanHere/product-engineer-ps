"""execute_tool_call(): the trust boundary between runtime and tool.

Availability check, input validation, bounded execution, output validation,
mechanical summary clamp. Detects and classifies failures.

Must NOT own: retry decisions, trace writes, or state mutation.
"""
