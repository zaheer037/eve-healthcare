"""The one error that the command-line tools raise on purpose."""


class SeedError(Exception):
    """A tool refuses to run. The message is safe to print: no secret and no email."""
