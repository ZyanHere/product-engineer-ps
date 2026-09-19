"""Entrypoint: parse argv, build Deps, invoke run(), render, set the exit code.

Also the outermost error boundary - an unexpected exception is reported with its
traceback and exits 2. The loop itself stays unwrapped, deliberately.

Must NOT own: loop, decision, or failure logic.
"""
