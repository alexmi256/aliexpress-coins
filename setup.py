"""Build shim for checkouts whose path contains braces.

setuptools' ``install`` command expands its base directories with
``str.format``. A checkout path such as ``.../{issue}`` is therefore read as a
format field and the wheel build fails with ``invalid variable 'issue'``.
A wheel build does not install in place, so there is nothing to expand.

Remove this shim once setuptools handles literal braces in paths.
"""

from setuptools import setup
from setuptools.command.install import install


class _InstallWithoutPathExpansion(install):
    """Skip setuptools' ``str.format`` expansion of paths containing braces."""

    def expand_basedirs(self) -> None:
        """Leave the install bases as the wheel builder supplied them."""


setup(cmdclass={'install': _InstallWithoutPathExpansion})
