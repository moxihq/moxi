import dataclasses
import textwrap
import tomllib
from pathlib import Path

import pytest

import moxi.modules as modules
from moxi import config
from moxi.config import Config, ConfigError, Schema, SchemaError

SCHEMA = """
[backend]
type = "enum"
values = ["local", "remote"]
default = "local"
doc = "Where messages are recorded."
read = "start"
env = "MOXI_DEMO_BACKEND"

[block]
type = "bool"
default = false
doc = "Refuse every send while true."
read = "use"
env = "MOXI_DEMO_BLOCK"

[local.retain_days]
type = "int"
default = 30
doc = "How many days the local record keeps a message."
read = "start"
env = "MOXI_DEMO_RETAIN_DAYS"

[local.record]
type = "path"
default = ""
doc = "The record's file; empty means the module's own place."
read = "start"
env = "MOXI_DEMO_RECORD"

[watch.tick]
type = "float"
default = 10.0
doc = "Seconds between two looks."
read = "start"

[provision.operator]
type = "string"
default = "operator"
doc = "The human's identity name."
read = "start"

[codex.agent]
type = "list"
default = []
doc = "The Codex roster, one table per agent."
read = "start"

[codex.defaults]
type = "table"
default = { sandbox = "workspace-write" }
doc = "Defaults for every Codex agent."
read = "start"

[gateway_url]
retired = "remove it; the record is always local now"
"""


@pytest.fixture
def schema():
    return Schema.parse(SCHEMA)


@pytest.fixture
def home(tmp_path):
    """An environment whose config root is a test directory; returns (env, config file)."""
    env = {"HOME": str(tmp_path), "MOXI_DEMO_CONFIG_HOME": str(tmp_path / "config")}
    return env, tmp_path / "config" / "demo" / "config.toml"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


# -- the schema ------------------------------------------------------------------------

def test_the_schema_declares_dotted_keys_in_order(schema):
    assert list(schema.keys) == ["backend", "block", "local.retain_days", "local.record",
                                 "watch.tick", "provision.operator", "codex.agent",
                                 "codex.defaults", "gateway_url"]
    assert [k.name for k in schema.live()][-1] == "codex.defaults"
    key = schema.keys["backend"]
    assert (key.type, key.default, key.read, key.values, key.env) == (
        "enum", "local", "start", ("local", "remote"), "MOXI_DEMO_BACKEND")
    assert schema.keys["gateway_url"].retired.startswith("remove it")


@pytest.mark.parametrize("text, words", [
    ('[a]\ntype = "int"\ndoc = "d"\nread = "start"\n', "lacks default"),
    ('[a]\ntype = "int"\ndefault = 1\nread = "start"\n', "lacks doc"),
    ('[a]\ntype = "int"\ndefault = 1\ndoc = "d"\n', "lacks read"),
    ('[a]\ntype = "number"\ndefault = 1\ndoc = "d"\nread = "start"\n', "has type"),
    ('[a]\ntype = "int"\ndefault = 1\ndoc = "d"\nread = "later"\n', "has read"),
    ('[a]\ntype = "int"\ndefault = 1\ndoc = "two\\nlines"\nread = "start"\n', "one-line doc"),
    ('[a]\ntype = "int"\ndefault = "1"\ndoc = "d"\nread = "start"\n',
     "default that is a string, not an int"),
    ('[a]\ntype = "enum"\ndefault = "x"\ndoc = "d"\nread = "start"\n', "without values"),
    ('[a]\ntype = "enum"\nvalues = ["y"]\ndefault = "x"\ndoc = "d"\nread = "start"\n',
     "default that is 'x'"),
    ('[a]\ntype = "int"\nvalues = [1]\ndefault = 1\ndoc = "d"\nread = "start"\n',
     "not an enum"),
    ('[a]\ntype = "list"\ndefault = []\ndoc = "d"\nread = "start"\nenv = "MOXI_X_A"\n',
     "env on a list"),
    ('[a]\ntype = "int"\ndefault = 1\ndoc = "d"\nread = "start"\nenv = "lower"\n',
     "has env"),
    ('[a]\ntype = "int"\ndefault = 1\ndoc = "d"\nread = "start"\nunits = "s"\n',
     "unknown field"),
    ('[a]\ntype = "path"\ndefault = "rel/dir"\ndoc = "d"\nread = "start"\n',
     "relative path"),
    ('a = 1\n', "is a value"),
    ('[a]\n', "empty table"),
    ('[a]\nretired = ""\n', "empty retired"),
    ('[a\n', "not valid TOML"),
])
def test_a_schema_that_breaks_the_format_is_refused(text, words):
    with pytest.raises(SchemaError) as err:
        Schema.parse(text, source="pkg/settings.toml")
    assert words in str(err.value)
    assert "pkg/settings.toml" in str(err.value)


def test_an_env_override_must_carry_the_modules_prefix(home):
    env, _ = home
    for name, words in [("MOXI_OTHER_X", "prefix MOXI_DEMO_"),
                        ("MOXI_DEMO_HOME", "root locator"),
                        ("MOXI_DEMO_CONFIG_HOME", "root locator")]:
        schema = Schema.parse(f'[x]\ntype = "int"\ndefault = 1\ndoc = "d"\nread = "start"\n'
                              f'env = "{name}"\n')
        with pytest.raises(SchemaError, match=words):
            config.load("demo", schema, env=env)
    twice = Schema.parse('[x]\ntype = "int"\ndefault = 1\ndoc = "d"\nread = "start"\n'
                         'env = "MOXI_DEMO_X"\n'
                         '[y]\ntype = "int"\ndefault = 1\ndoc = "d"\nread = "start"\n'
                         'env = "MOXI_DEMO_X"\n')
    with pytest.raises(SchemaError, match="already overrides"):
        config.load("demo", twice, env=env)


# -- reading ---------------------------------------------------------------------------

def test_a_missing_file_means_every_default(schema, home):
    env, path = home
    s = config.load("demo", schema, env=env)
    assert s.file == path
    assert s["backend"] == "local" and s["block"] is False
    assert s["local.retain_days"] == 30
    assert s["local.record"] is None
    assert s["watch.tick"] == 10.0
    assert s["codex.agent"] == [] and s["codex.defaults"] == {"sandbox": "workspace-write"}
    assert set(s.sources.values()) == {"default"}
    assert "gateway_url" not in s


def test_the_file_sets_keys_and_says_so(schema, home):
    env, path = home
    write(path, """
        backend = "remote"
        [local]
        retain_days = 7
        record = "~/records/demo.db"
        [watch]
        tick = 5
        [[codex.agent]]
        name = "one"
        [codex.defaults]
        sandbox = "read-only"
        """)
    s = config.load("demo", schema, env=env)
    assert s["backend"] == "remote" and s.source("backend") == "file"
    assert s["local.retain_days"] == 7
    assert s["local.record"] == Path(env["HOME"], "records/demo.db")
    assert s["watch.tick"] == 5.0 and isinstance(s["watch.tick"], float)
    assert s["codex.agent"] == [{"name": "one"}]
    assert s["codex.defaults"] == {"sandbox": "read-only"}
    assert s.source("block") == "default"


def test_settings_are_frozen(schema, home):
    env, _ = home
    s = config.load("demo", schema, env=env)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.module = "other"
    with pytest.raises(TypeError):
        s.values["backend"] = "remote"
    with pytest.raises(KeyError, match="declares no setting"):
        s["nope"]


def test_a_reading_never_shares_file_values_with_the_next(schema, home):
    env, path = home
    write(path, """
        [[codex.agent]]
        name = "one"
        [codex.defaults]
        sandbox = "read-only"
        """)
    first = config.load("demo", schema, env=env)
    first["codex.agent"].append({"name": "mutated"})
    first["codex.defaults"]["sandbox"] = "mutated"
    again = config.load("demo", schema, env=env)
    assert again["codex.agent"] == [{"name": "one"}]
    assert again["codex.defaults"] == {"sandbox": "read-only"}


@pytest.mark.parametrize("text, key, words", [
    ('colour = "red"\n', "colour", "not a setting"),
    ('[local]\ncolour = "red"\n', "local.colour", "not a setting"),
    ('gateway_url = "x"\n', "gateway_url", "is retired"),
    ('block = "yes"\n', "block", "is a string, not a bool"),
    ('[local]\nretain_days = 1.5\n', "local.retain_days", "is a float, not an int"),
    ('backend = "cloud"\n', "backend", "is 'cloud'"),
    ('local = 3\n', "local", "table of settings"),
    ('[local]\nrecord = "records/demo.db"\n', "local.record", "relative path"),
    ('[codex]\nagent = "x"\n', "codex.agent", "not a list"),
    ('[codex]\ndefaults = 1\n', "codex.defaults", "not a table"),
])
def test_a_bad_file_names_the_file_the_key_and_the_fix(schema, home, text, key, words):
    env, path = home
    write(path, text)
    with pytest.raises(ConfigError) as err:
        config.load("demo", schema, env=env)
    e = err.value
    assert e.file == str(path) and e.key == key
    assert words in e.problem and e.fix
    assert str(path) in str(e) and "fix:" in str(e)


def test_a_retired_key_says_what_to_do_now(schema, home):
    env, path = home
    write(path, 'gateway_url = "x"\n')
    with pytest.raises(ConfigError) as err:
        config.load("demo", schema, env=env)
    assert err.value.fix == "remove it; the record is always local now"


def test_a_malformed_file_is_refused(schema, home):
    env, path = home
    write(path, "backend = \n")
    with pytest.raises(ConfigError, match="not valid TOML") as err:
        config.load("demo", schema, env=env)
    assert err.value.key is None


def test_a_declared_env_override_wins_for_one_process(schema, home):
    env, path = home
    write(path, 'backend = "local"\nblock = false\n')
    s = config.load("demo", schema, env={**env, "MOXI_DEMO_BACKEND": "remote",
                                          "MOXI_DEMO_BLOCK": "on",
                                          "MOXI_DEMO_RETAIN_DAYS": "3",
                                          "MOXI_DEMO_RECORD": "/var/demo.db"})
    assert (s["backend"], s.source("backend")) == ("remote", "env")
    assert (s["block"], s.source("block")) == (True, "env")
    assert s["local.retain_days"] == 3
    assert s["local.record"] == Path("/var/demo.db")


def test_an_empty_env_override_is_unset(schema, home):
    env, _ = home
    s = config.load("demo", schema, env={**env, "MOXI_DEMO_BACKEND": ""})
    assert s.source("backend") == "default"


@pytest.mark.parametrize("var, value, words", [
    ("MOXI_DEMO_BLOCK", "maybe", "not a bool"),
    ("MOXI_DEMO_RETAIN_DAYS", "ten", "not an int"),
    ("MOXI_DEMO_BACKEND", "cloud", "is 'cloud'"),
    ("MOXI_DEMO_RECORD", "rel/path", "relative path"),
])
def test_a_bad_env_override_names_the_variable(schema, home, var, value, words):
    env, _ = home
    with pytest.raises(ConfigError) as err:
        config.load("demo", schema, env={**env, var: value})
    assert var in str(err.value) and words in str(err.value)


def test_an_undeclared_variable_moves_nothing(schema, home):
    env, _ = home
    s = config.load("demo", schema, env={**env, "MOXI_DEMO_TICK": "1"})
    assert s.source("watch.tick") == "default"


def test_the_file_is_parsed_again_only_when_it_changes(schema, home, monkeypatch):
    env, path = home
    write(path, 'backend = "remote"\n')
    calls = []
    real = tomllib.load
    monkeypatch.setattr(tomllib, "load", lambda fh: calls.append(1) or real(fh))
    config.load("demo", schema, env=env)
    config.load("demo", schema, env=env)
    assert len(calls) == 1
    write(path, 'backend = "local"\nblock = true\n')
    s = config.load("demo", schema, env=env)
    assert len(calls) == 2
    assert s["block"] is True


def test_the_config_root_comes_from_the_layout(schema, tmp_path, monkeypatch):
    monkeypatch.setenv("MOXI_DEMO_HOME", str(tmp_path / "h"))
    monkeypatch.delenv("MOXI_DEMO_CONFIG_HOME", raising=False)
    assert config.load("demo", schema).file == tmp_path / "h" / "config" / "demo" / \
        "config.toml"


# -- the running module's reader -------------------------------------------------------

def test_config_starts_and_then_reads_at_use(schema, home):
    env, path = home
    reports = []
    cfg = Config("demo", schema, env=env, report=reports.append)
    assert cfg.start()["block"] is False
    write(path, "block = true\n")
    assert cfg.use("block") is True
    assert reports == []


def test_a_bad_file_refuses_the_start(schema, home):
    env, path = home
    write(path, "block = 1\n")
    with pytest.raises(ConfigError):
        Config("demo", schema, env=env).start()


def test_at_use_a_bad_file_keeps_the_last_good_and_reports_once_per_change(schema, home):
    env, path = home
    reports = []
    cfg = Config("demo", schema, env=env, report=reports.append)
    write(path, "block = true\n")
    cfg.start()
    write(path, "block = 'yes please'\n")
    assert cfg.use("block") is True
    assert cfg.use("block") is True
    assert len(reports) == 1 and reports[0].key == "block"
    write(path, "block = 'no, really, yes please'\n")
    assert cfg.use("block") is True
    assert len(reports) == 2
    write(path, "block = false\n")
    assert cfg.use("block") is False
    assert len(reports) == 2


def test_use_before_start_is_a_bug(schema, home):
    env, _ = home
    with pytest.raises(RuntimeError):
        Config("demo", schema, env=env).use("block")


# -- a module's schema by its announcement ---------------------------------------------

def test_the_schema_comes_from_the_announcements_package(site, home):
    env, path = home
    site.write("moxi_demo/settings.toml", SCHEMA)
    site.module("demo", SETTINGS="'settings.toml'")
    found = modules.load("demo")
    schema = config.schema_for(found)
    assert "local.retain_days" in schema.keys
    assert schema.source == "moxi_demo/settings.toml"
    write(path, "[local]\nretain_days = 2\n")
    assert config.load("demo", schema, env=env)["local.retain_days"] == 2


def test_no_settings_means_no_schema(site):
    site.module("demo")
    assert config.schema_for(modules.load("demo")) is None


def test_a_missing_schema_resource_is_a_schema_error(site):
    site.module("demo", SETTINGS="'gone.toml'")
    with pytest.raises(SchemaError, match="cannot be read"):
        config.schema_for(modules.load("demo"))
