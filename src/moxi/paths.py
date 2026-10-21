"""Where a Moxi module keeps its files: five roots, and one directory per root per module.

`Layout(module)` resolves them for one module:

    kind     default                                   override (prefix MOXI_<MODULE>_)
    data     ~/.local/share/moxi/<module>/              <prefix>DATA_HOME
    state    ~/.local/state/moxi/<device>/<module>/     <prefix>STATE_HOME
    cache    ~/.cache/moxi/<device>/<module>/           <prefix>CACHE_HOME
    config   ~/.config/moxi/<module>/  (config.toml)    <prefix>CONFIG_HOME
    runtime  $XDG_RUNTIME_DIR/moxi/<module>/            <prefix>RUNTIME_DIR
             else <state root>/runtime/<module>/

An override names the root; the module's directory is the root joined with the module's
name. `<prefix>HOME` collapses all five roots under one directory (`<home>/data`,
`<home>/state/<device>`, `<home>/cache/<device>`, `<home>/config`, `<home>/runtime`).
`<prefix>DEVICE` names the device (default: the host name). The XDG variables move the
defaults. A per-root override wins over `<prefix>HOME`, which wins over XDG.

Every method takes an optional `env` mapping and reads `os.environ` without one.

A module writes only under its own five directories. `script(name)` is where one of this
environment's own console scripts lives, for a module that writes the path of its own
entry point into a hook, a unit file or a launcher's argv: it never looks that entry
point up on PATH, where another program of the same name may stand first.
"""

from __future__ import annotations

import os
import socket
import sysconfig
from collections.abc import Mapping
from pathlib import Path

from . import names

#: The file name of a module's config file, in its config directory.
CONFIG_FILE = "config.toml"

#: The suffixes, after a module's prefix, of every variable this module reads.
LOCATORS = ("HOME", "DATA_HOME", "STATE_HOME", "CACHE_HOME", "CONFIG_HOME", "RUNTIME_DIR",
            "DEVICE")


class Layout:
    """The roots and directories of one module, under one environment prefix."""

    def __init__(self, module: str, prefix: str | None = None):
        self.module = names.check_module_name(module)
        self.prefix = names.env_prefix(module) if prefix is None else prefix
        self.namespace = names.NAMESPACE

    def __repr__(self) -> str:
        return f"Layout({self.module!r}, {self.prefix!r})"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Layout)
                and (self.module, self.prefix) == (other.module, other.prefix))

    def __hash__(self) -> int:
        return hash((self.module, self.prefix))

    def locators(self) -> tuple[str, ...]:
        """The names of every variable that moves this module's roots."""
        return tuple(f"{self.prefix}{suffix}" for suffix in LOCATORS)

    # -- the roots ---------------------------------------------------------------------

    def device(self, env: Mapping[str, str] | None = None) -> str:
        return _environ(env).get(f"{self.prefix}DEVICE") or socket.gethostname()

    def _root(self, suffix: str, xdg_var: str, xdg_default: str, *, device_keyed: bool,
              env: Mapping[str, str] | None) -> Path:
        environ = _environ(env)
        if per_root := environ.get(f"{self.prefix}{suffix}"):
            return Path(per_root)
        if home := environ.get(f"{self.prefix}HOME"):
            base = Path(home) / xdg_var.removeprefix("XDG_").removesuffix("_HOME").lower()
        else:
            xdg = environ.get(xdg_var) or _expanduser(xdg_default, environ)
            base = Path(xdg) / self.namespace
        return base / self.device(environ) if device_keyed else base

    def data_root(self, env: Mapping[str, str] | None = None) -> Path:
        return self._root("DATA_HOME", "XDG_DATA_HOME", "~/.local/share",
                          device_keyed=False, env=env)

    def state_root(self, env: Mapping[str, str] | None = None) -> Path:
        return self._root("STATE_HOME", "XDG_STATE_HOME", "~/.local/state",
                          device_keyed=True, env=env)

    def cache_root(self, env: Mapping[str, str] | None = None) -> Path:
        return self._root("CACHE_HOME", "XDG_CACHE_HOME", "~/.cache",
                          device_keyed=True, env=env)

    def config_root(self, env: Mapping[str, str] | None = None) -> Path:
        return self._root("CONFIG_HOME", "XDG_CONFIG_HOME", "~/.config",
                          device_keyed=False, env=env)

    def runtime_root(self, env: Mapping[str, str] | None = None) -> Path:
        environ = _environ(env)
        if per_root := environ.get(f"{self.prefix}RUNTIME_DIR"):
            return Path(per_root)
        if home := environ.get(f"{self.prefix}HOME"):
            return Path(home) / "runtime"
        if xdg := environ.get("XDG_RUNTIME_DIR"):
            return Path(xdg) / self.namespace
        # No login runtime directory (some containers): under the state root instead.
        return self.state_root(environ) / "runtime"

    # -- the module's directories ------------------------------------------------------

    def data_dir(self, env: Mapping[str, str] | None = None) -> Path:
        return self.data_root(env) / self.module

    def state_dir(self, env: Mapping[str, str] | None = None) -> Path:
        return self.state_root(env) / self.module

    def cache_dir(self, env: Mapping[str, str] | None = None) -> Path:
        return self.cache_root(env) / self.module

    def config_dir(self, env: Mapping[str, str] | None = None) -> Path:
        return self.config_root(env) / self.module

    def runtime_dir(self, env: Mapping[str, str] | None = None) -> Path:
        return self.runtime_root(env) / self.module

    def config_file(self, env: Mapping[str, str] | None = None) -> Path:
        """The module's one config file."""
        return self.config_dir(env) / CONFIG_FILE


def script(name: str) -> Path:
    """The path of console script `name` in this Python environment's scripts directory.

    Raises `FileNotFoundError` when the environment holds no such script.
    """
    path = Path(sysconfig.get_path("scripts")) / name
    if not path.is_file():
        raise FileNotFoundError(f"{name} is not installed in this environment "
                                f"(looked for {path})")
    return path


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _expanduser(path: str, environ: Mapping[str, str]) -> str:
    """`os.path.expanduser`, reading HOME from `environ` when it is a mapping of its own."""
    if environ is os.environ or "HOME" not in environ:
        return os.path.expanduser(path)
    return environ["HOME"] + path[1:] if path.startswith("~") else path


__all__ = ["CONFIG_FILE", "LOCATORS", "Layout", "script"]
