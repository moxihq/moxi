"""Moxi's names: the constants every public spelling is built from, and the spellings.

A Moxi module has one name, lower-case ASCII letters and digits (`cache`, `comms`).
Everything else a module is called is spelled from that name here:

    distribution("cache")   moxi-cache      the package on PyPI
    import_name("cache")    moxi_cache      the Python package
    repo("cache")           moxihq/cache    the public repository
    env_prefix("cache")     MOXI_CACHE_     the prefix of every variable it reads

The core itself is `moxi` in all four places and is not a module.
"""

from __future__ import annotations

import re

#: The harness.
HARNESS = "moxi"
#: The GitHub organisation that holds the public repositories.
ORG = "moxihq"
#: The directory namespace under the data, state, cache, config and runtime roots,
#: shared by every module, each under its own subdirectory.
NAMESPACE = "moxi"

#: The umbrella's own verbs (`moxi install`, `moxi check`, ...). A module named by one
#: could never be reached as `moxi <module>`, so no module may take one.
VERBS = frozenset({"modules", "install", "shortcut", "config", "check", "help", "version"})

_NAME = re.compile(r"[a-z0-9]+")


def is_module_name(name: object) -> bool:
    """True when `name` is spelled as a module name: lower-case ASCII letters and digits."""
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


def check_module_name(name: object) -> str:
    """Return `name` when it is spelled as a module name, else raise `ValueError`."""
    if not is_module_name(name):
        raise ValueError(f"{name!r} is not a Moxi module name: "
                         "use lower-case ASCII letters and digits only")
    return name  # type: ignore[return-value]


def distribution(name: str) -> str:
    """The distribution name of module `name`: `moxi-<name>`."""
    return f"{HARNESS}-{check_module_name(name)}"


def import_name(name: str) -> str:
    """The import package of module `name`: `moxi_<name>`."""
    return f"{HARNESS}_{check_module_name(name)}"


def repo(name: str) -> str:
    """The public repository of module `name`: `moxihq/<name>`."""
    return f"{ORG}/{check_module_name(name)}"


def env_prefix(name: str) -> str:
    """The environment prefix of module `name`: `MOXI_<NAME>_`."""
    return f"{HARNESS.upper()}_{check_module_name(name).upper()}_"


__all__ = ["HARNESS", "ORG", "NAMESPACE", "VERBS", "is_module_name", "check_module_name",
           "distribution", "import_name", "repo", "env_prefix"]
