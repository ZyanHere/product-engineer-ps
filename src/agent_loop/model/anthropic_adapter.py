"""Anthropic Messages API adapter: translation only.

Maps ModelRequest to a request body, tool_use blocks to a decision, provider
errors to ModelCallError, and token counts to ModelUsage. Declares the reserved
submit_final_answer tool so citations arrive as structured output.

Never requests extended thinking: the chain-of-thought boundary is enforced here
rather than by filtering downstream.

Must NOT own: validation, retries, or budget.
"""
