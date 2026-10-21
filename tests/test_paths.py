from pathlib import Path

import pytest

from moxi import paths
from moxi.paths import Layout

HOME = "/users/someone"


def env(**extra):
    """An environment with a HOME, a device name and nothing else unless given."""
    return {"HOME": HOME, "MOXI_CACHE_DEVICE": "box", **extra}


def test_the_defaults_without_xdg():
    lay = Layout("cache")
    e = env()
    assert lay.data_dir(e) == Path(HOME, ".local/share/moxi/cache")
    assert lay.state_dir(e) == Path(HOME, ".local/state/moxi/box/cache")
    assert lay.cache_dir(e) == Path(HOME, ".cache/moxi/box/cache")
    assert lay.config_dir(e) == Path(HOME, ".config/moxi/cache")
    assert lay.config_file(e) == Path(HOME, ".config/moxi/cache/config.toml")
    # No runtime directory: under the state root.
    assert lay.runtime_dir(e) == Path(HOME, ".local/state/moxi/box/runtime/cache")


def test_the_roots_are_moxis_and_the_directories_the_modules():
    lay = Layout("cache")
    e = env()
    assert lay.data_root(e) == Path(HOME, ".local/share/moxi")
    assert lay.state_root(e) == Path(HOME, ".local/state/moxi/box")
    assert lay.cache_root(e) == Path(HOME, ".cache/moxi/box")
    assert lay.config_root(e) == Path(HOME, ".config/moxi")
    for root, d in [(lay.data_root, lay.data_dir), (lay.state_root, lay.state_dir),
                    (lay.cache_root, lay.cache_dir), (lay.config_root, lay.config_dir),
                    (lay.runtime_root, lay.runtime_dir)]:
        assert d(e) == root(e) / "cache"


def test_xdg_moves_the_defaults():
    e = env(XDG_DATA_HOME="/x/data", XDG_STATE_HOME="/x/state", XDG_CACHE_HOME="/x/cache",
            XDG_CONFIG_HOME="/x/config", XDG_RUNTIME_DIR="/run/user/1000")
    lay = Layout("cache")
    assert lay.data_dir(e) == Path("/x/data/moxi/cache")
    assert lay.state_dir(e) == Path("/x/state/moxi/box/cache")
    assert lay.cache_dir(e) == Path("/x/cache/moxi/box/cache")
    assert lay.config_dir(e) == Path("/x/config/moxi/cache")
    assert lay.runtime_dir(e) == Path("/run/user/1000/moxi/cache")


def test_an_empty_xdg_variable_is_unset():
    assert Layout("cache").data_dir(env(XDG_DATA_HOME="")) == \
        Path(HOME, ".local/share/moxi/cache")


def test_a_per_root_override_names_the_root():
    e = env(MOXI_CACHE_DATA_HOME="/d", MOXI_CACHE_STATE_HOME="/s", MOXI_CACHE_CACHE_HOME="/c",
            MOXI_CACHE_CONFIG_HOME="/f", MOXI_CACHE_RUNTIME_DIR="/r",
            XDG_RUNTIME_DIR="/run/user/1000")
    lay = Layout("cache")
    assert lay.data_dir(e) == Path("/d/cache")
    assert lay.state_dir(e) == Path("/s/cache")
    assert lay.cache_dir(e) == Path("/c/cache")
    assert lay.config_file(e) == Path("/f/cache/config.toml")
    assert lay.runtime_dir(e) == Path("/r/cache")


def test_home_collapses_all_five_roots():
    e = env(MOXI_CACHE_HOME="/h", XDG_DATA_HOME="/x", XDG_RUNTIME_DIR="/run/user/1000")
    lay = Layout("cache")
    assert lay.data_dir(e) == Path("/h/data/cache")
    assert lay.state_dir(e) == Path("/h/state/box/cache")
    assert lay.cache_dir(e) == Path("/h/cache/box/cache")
    assert lay.config_dir(e) == Path("/h/config/cache")
    assert lay.runtime_dir(e) == Path("/h/runtime/cache")


def test_a_per_root_override_wins_over_home():
    e = env(MOXI_CACHE_HOME="/h", MOXI_CACHE_DATA_HOME="/d", MOXI_CACHE_RUNTIME_DIR="/r")
    lay = Layout("cache")
    assert lay.data_dir(e) == Path("/d/cache")
    assert lay.runtime_dir(e) == Path("/r/cache")
    assert lay.config_dir(e) == Path("/h/config/cache")


def test_the_device_defaults_to_the_host_name(monkeypatch):
    monkeypatch.setattr(paths.socket, "gethostname", lambda: "thishost")
    e = {"HOME": HOME}
    assert Layout("cache").device(e) == "thishost"
    assert Layout("cache").state_dir(e) == Path(HOME, ".local/state/moxi/thishost/cache")
    assert Layout("cache").device({"HOME": HOME, "MOXI_CACHE_DEVICE": ""}) == "thishost"


def test_each_module_reads_only_its_own_prefix():
    e = env(MOXI_COMMS_HOME="/comms-home", MOXI_COMMS_DATA_HOME="/comms-data")
    assert Layout("cache").data_dir(e) == Path(HOME, ".local/share/moxi/cache")
    assert Layout("comms").data_dir(e) == Path("/comms-data/comms")
    assert Layout("comms").config_dir(e) == Path("/comms-home/config/comms")


def test_the_prefix_is_a_parameter():
    lay = Layout("cache", "OTHER_")
    assert lay.prefix == "OTHER_"
    assert lay.data_dir(env(OTHER_HOME="/o")) == Path("/o/data/cache")
    assert Layout("cache").prefix == "MOXI_CACHE_"
    assert Layout("cache").locators() == (
        "MOXI_CACHE_HOME", "MOXI_CACHE_DATA_HOME", "MOXI_CACHE_STATE_HOME",
        "MOXI_CACHE_CACHE_HOME", "MOXI_CACHE_CONFIG_HOME", "MOXI_CACHE_RUNTIME_DIR",
        "MOXI_CACHE_DEVICE")


def test_without_a_mapping_it_reads_the_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MOXI_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("MOXI_CACHE_DEVICE", "box")
    for var in ("MOXI_CACHE_DATA_HOME", "MOXI_CACHE_STATE_HOME", "MOXI_CACHE_RUNTIME_DIR"):
        monkeypatch.delenv(var, raising=False)
    lay = Layout("cache")
    assert lay.data_dir() == tmp_path / "data" / "cache"
    assert lay.state_dir() == tmp_path / "state" / "box" / "cache"
    assert lay.runtime_dir() == tmp_path / "runtime" / "cache"


def test_the_home_in_the_mapping_is_the_one_used(monkeypatch):
    monkeypatch.setenv("HOME", "/somewhere/else")
    assert Layout("cache").data_dir({"HOME": "/mine"}) == Path("/mine/.local/share/moxi/cache")


def test_a_layout_needs_a_module_name():
    with pytest.raises(ValueError):
        Layout("Not-A-Name")
    assert Layout("cache") == Layout("cache")
    assert Layout("cache") != Layout("comms")
    assert len({Layout("cache"), Layout("cache")}) == 1


def test_script_is_in_this_environments_scripts_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sysconfig, "get_path", lambda kind: str(tmp_path))
    (tmp_path / "moxi-cache-watch").write_text("#!/bin/sh\n")
    assert paths.script("moxi-cache-watch") == tmp_path / "moxi-cache-watch"
    with pytest.raises(FileNotFoundError, match="moxi-cache-gone"):
        paths.script("moxi-cache-gone")
