"""The test suite, as a package.

`__init__.py` is here so `tests.shared` is one module with one name. Without it,
a type checker walking the tree sees the same file as both `shared` and
`tests.shared` and refuses to guess which was meant.
"""
