"""The toolchain works and both packages import."""

import app
import scripts


def test_packages_import() -> None:
    assert app.__doc__
    assert scripts.__doc__
