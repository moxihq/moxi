# Moxi

Moxi is a harness for running many AI coding-agent sessions as one system. Popular agent
CLIs such as Claude Code and Codex add little structure around a session. Moxi adds some,
and it makes opinionated choices to do it: agents have names and identities, sessions
message each other, and a clock tracks when a session's prompt cache goes cold. These
choices won't suit everyone.

Moxi is built as modules that plug together. A module installs with the modules it
requires and nothing more; the rest are optional, and a feature that needs a missing
module is off and quiet.

## Modules

| module | what it does | requires | optional | repo |
|---|---|---|---|---|
| `moxi` | The core: names, paths, one config file per module, module discovery, the module contract check, and the `moxi` command | nothing (standard library only) | | [moxihq/moxi](https://github.com/moxihq/moxi) |
| sessions | Where a harness keeps a session's record, and how its lines read | `moxi` | | [moxihq/sessions](https://github.com/moxihq/sessions) |
| turns | The one interface to a harness's session: a turn, mail, `/clear` and `/exit` in, session events out. A library with no command of its own | `moxi` | `codex`: the Codex host | [moxihq/turns](https://github.com/moxihq/turns) |
| cache | The prompt cache clock: when a session's cache goes cold, with a watcher and a status line segment | `moxi`, sessions | | [moxihq/cache](https://github.com/moxihq/cache) |
| comms | Messages between agent sessions on one machine, delivered into the recipient's session at the end of its current turn | `moxi`, turns | `cache`: hold a send while the recipient's cache is cold; `codex`: Codex sessions | [moxihq/comms](https://github.com/moxihq/comms) |

## The `moxi` command

Every module runs as `moxi <module> <verb>`, for example `moxi comms send`. The core's own
commands:

- `moxi modules` lists the modules this environment holds, their state and their extras.
- `moxi install <module>` installs a module with the modules it requires, runs its setup,
  and offers each optional extra with a line saying what it enables. `--with`,
  `--without`, `--all` and `--none` answer ahead.
- `moxi shortcut <module>` puts a bare `<module>` command on PATH. It is off by default
  and refused when the name is already taken; `--remove` takes it off.
- `moxi config <module>` shows a module's settings and its config file.
- `moxi check` checks a module repo against the module contract.

## Install

Linux, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install moxi
moxi install comms
```

A plain `uv tool install moxi-comms` also works and puts `moxi` on PATH, but runs no
setup; `moxi install turns` then installs turns' Claude Code plugin.

### Before the modules are on PyPI

Install the core from git, then give `moxi install` one `--from` for each Moxi
distribution the module needs that the environment lacks, in dependency order: `moxi`,
`moxi-sessions`, `moxi-turns`, `moxi-cache`, `moxi-comms`. One left out is looked for on
PyPI and the install fails. For comms:

```sh
uv tool install git+https://github.com/moxihq/moxi
moxi install comms --none \
  --from 'moxi-turns @ git+https://github.com/moxihq/turns' \
  --from 'moxi-comms @ git+https://github.com/moxihq/comms'
```

comms with its `cache` extra needs cache and sessions too:

```sh
uv tool install git+https://github.com/moxihq/moxi
moxi install comms --with cache \
  --from 'moxi-sessions @ git+https://github.com/moxihq/sessions' \
  --from 'moxi-turns @ git+https://github.com/moxihq/turns' \
  --from 'moxi-cache @ git+https://github.com/moxihq/cache' \
  --from 'moxi-comms @ git+https://github.com/moxihq/comms'
```

## Troubleshooting

**`moxi` is gone after removing a module.** Every Moxi distribution provides the `moxi`
command, so removing one from an environment can take `moxi` with it while other modules
remain. Reinstall the tool (its name is in `uv tool list`):

```sh
uv tool install <tool> --reinstall-package <tool>
```

In a plain virtual environment, reinstalling any Moxi module brings `moxi` back.

**A shortcut fails with `moxi: not found`.** Its environment was removed. Delete the
shortcut file, or reinstall.

## License

MIT. See [LICENSE](LICENSE).
