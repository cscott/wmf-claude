# PLAN: defaults for command-line options from settings.yaml

Written 2026-10-14, after `create --clone-missing` (commit 24728b8)
added the first `create:` section by hand; revised the same day with
the engineer's decisions (below). The goal: **every** option of every
`wmf-sbx` command gets its default from settings.yaml, with no code of
its own, so there is no list of exceptions to remember.

## Decisions (engineer, 2026-10-14)

- **Every option is settable.** `--dry-run`, `--cold`, `--name`,
  `--nested`, `--image` too. A `create: {dryRun: true}` engineer says
  `--no-dry-run` to make a real VM.
- **settings.yaml is an explicit request from the engineer.** That holds
  for options that lower safety too (`--sudo`, `--nested`,
  `exec --engineer`, `rm --force`): caveat emptor. wmf-sbx does not
  second-guess a value from settings any more than one from the command
  line. No floors: `create: {memory: 1GiB}` gets 1 GiB, `--nested` or
  not. The computed defaults apply only when nothing was asked for.
- **`null` means "choose for me".** For an option whose code default is
  computed (`--egress-proxy`, `--vm-type`, `--memory`, `--disk`,
  `--sudo`...), `key: null`, or no key, gives today's behaviour,
  warnings included. `egressProxy: true` or `false` is an explicit
  request: if wmf-sbx cannot comply (vz has no egress proxy), it is an
  error, as `--egress-proxy` is now.
- **A global `--settings/--no-settings`.** `--no-settings` turns off the
  defaults from settings.yaml, for scripts and tests that must not
  depend on the engineer's file. `session: {settings: false}` turns
  them off for every command. The command line wins over that, as
  always: `--settings` turns them back on, and `settings: true` does not
  undo an explicit `--no-settings`. This switch covers option defaults
  only. `repos:`, `environment:`, `imageCache:` and the others are still
  read: `create` cannot resolve a repo without `repos:`.
- **`session.sudo` moves to `create.sudo`.** `session:` holds Claude
  Code's preferences; this is a `create` option. There is no legacy
  `session.sudo`.
- **No compatibility keys.** The lima port is not released. When a
  key moves or is renamed, the old one goes; ask the engineer about
  their own settings.yaml instead of keeping an alias. (The `images:`
  cache policy section became `imageCache:` this way, commit 2d838ce.)
- **Follow-ups, not this plan:** `resume` on argparse (it parses by
  hand today); defaults for positional arguments. Both are below, under
  "Follow-ups". `logs` stays as it is: it has no options of its own,
  only `tail`'s. `run`, an alias of `resume`, is removed.

## The rule

For an option `--some-flag` of `wmf-sbx VERB`, the settings key is
`VERB.someFlag`: the argparse `dest` in camelCase, as `imageCache.maxAge`
and `create.cloneMissing` are. A subcommand nests: `image build
--keep-builder` is `image: {build: {keepBuilder: true}}`. Precedence,
highest first:

1. the command line;
2. settings.yaml `VERB.someFlag`, unless `--no-settings` or
   `session.settings: false`;
3. the code default: `default=`, or the value computed when the option
   is None, as for `--memory` and `--vm-type`.

`null` in settings is the same as no key.

**Unknown keys warn; they are not errors.** A key that the command does
not have gets a warning that lists the keys it does have, and is
ignored. The same goes for a top-level section that `load_config` does
not know (today a stale `images:` is silently ignored), and for an
unknown `session:` key (today an error). A warning still catches a
typo, and one settings.yaml can serve a stable wmf-sbx and a
development one that adds a section or an option: the stable one warns
and carries on. A known key with a value of the wrong type is still an
error: that is not a version skew. Step 1 does the top level, with the
list of command sections from step 6's walk.

Only `--config` and `--settings/--no-settings` cannot come from
settings. They say which file to read and whether to read it. (`-h`
is not an option with a value.) `session.settings` is how settings.yaml
turns the second one off.

## The helper: `cli.py`

One new module, `src/wmf_sbx/cli.py`. Every command builds its parser
in its `main` and calls:

```python
def parse_args(parser, argv, section, config_default=resolve.DEFAULT_CONFIG):
    """(args, config). Adds --config and --settings/--no-settings to
    `parser`, and gives every other option its default from settings.yaml
    `section` (a verb, or "image.build" for a subcommand)."""
```

How it works:

1. **What did the command line say?** Parse `argv` once with every
   option's default replaced by `argparse.SUPPRESS`. The namespace then
   holds only the options that were given. This is how the helper knows
   that `--config` and `--no-settings` were given, which members of a
   mutually exclusive group were given, and which values are the code's
   own defaults.
2. **Which file, and whether to read it.** `--config` (or the
   default), then `resolve.load_config`. Unless `--no-settings` was
   given: `session.settings`, then `--settings`, decide whether the
   section is used.
3. **The section.** For each key: find the action whose camelCased
   `dest` matches; warn about unknown keys and skip them; check the value's type against
   the action (a boolean pair: bool; `type=int`: int; `choices`: one of
   them; `append` or `nargs`: a list; `null` always passes); then
   `parser.set_defaults(dest=value)`. For a mutually exclusive group
   (`allow --rm | --list`), a member given on the command line drops the
   settings values of the others. If two members are true in settings,
   that is an error that names both keys.
4. **Help.** Each option's help gets ` [settings: VERB.someFlag]`, and
   the value when the settings set one. So `--help` shows where a
   default comes from, and which defaults `--no-settings` would remove.
5. Parse again, for real, and return `(args, config)`. The command does
   not read settings.yaml a second time.

argparse's `set_defaults` is the whole mechanism: the command code reads
`args.x` as it does now and never knows where the value came from. The
helper walks `parser._actions` and `parser._mutually_exclusive_groups`.
Those are private API, but argparse has not changed them in a long time.
A unit test pins the parts used.

### Every flag is a pair

A settings default for a `store_true` flag cannot be undone on the
command line: with `create: {cold: true}` there is no `--no-cold`. So
every `store_true` flag becomes an `argparse.BooleanOptionalAction`
with a positive `dest`, as `--sudo`, `--egress-proxy` and
`--clone-missing` are now. The helper refuses a `store_true` action (an
assertion, and the test in step 6 runs it over every command).

| command | now | becomes | key |
| --- | --- | --- | --- |
| create | `--no-deps` | `--deps/--no-deps` (default true) | `deps` |
| | `--no-ci-deps` | `--ci-deps/--no-ci-deps` | `ciDeps` |
| | `--no-phan-deps` | `--phan-deps/--no-phan-deps` | `phanDeps` |
| | `--no-dev` | `--dev/--no-dev` | `dev` |
| | `--no-suggests` | `--suggests/--no-suggests` | `suggests` |
| | `--no-remotes` | `--remotes/--no-remotes` | `remotes` |
| | `--no-snapshot` | `--snapshot/--no-snapshot` | `snapshot` |
| | `--no-expire` | `--expire/--no-expire` (default null: `imageCache.expire`) | `expire` |
| | `--reset-all`, `--cold`, `--full-disk`, `--nested`, `--dry-run` | `--X/--no-X` | `resetAll`, `cold`, `fullDisk`, `nested`, `dryRun` |
| rm | `--dry-run`, `--keep-remotes`, `-f/--force`, `--prune` | `--X/--no-X` (`-f` stays) | `dryRun`, `keepRemotes`, `force`, `prune` |
| exec | `--engineer` | `--engineer/--no-engineer` | `engineer` |
| allow | `--rm`, `--list` | `--X/--no-X`, still a mutually exclusive group | `rm`, `list` |
| cp | `-r/--recursive` | `--recursive/--no-recursive` (`-r`, `-R` stay) | `recursive` |
| image build | `--dry-run`, `--keep-builder` | `--X/--no-X` | `dryRun`, `keepBuilder` |
| ls-remotes, resolve | `--json` | `--json/--no-json` | `json` |

Every spelling in use today keeps working, with the same meaning. The
code reads `args.deps` where it read `not args.no_deps`, and so on.
`--expire` null falls back to `imageCache.expire`, which `image build`
also reads. The two keys do not compete: `create.expire` is "this
create", and `imageCache.expire` is the cache policy.

The value options are settable as they are: `create --name`, `--vm-type`,
`--cpus`, `--memory`, `--disk`, `--image`, `--egress-proxy`, `--sudo`,
`--clone-missing`; `exec -w/--workdir`; `rm --scan` (a list: settings
`scan: [~/Wikimedia]` makes `rm` scan there each time, as `--scan`
would); `login --image`; `ls-remotes --path`; `resolve --path`.

### What changes in the commands

- `create`: today's computed defaults stay as they are, behind "None
  means compute". So `vmType: null` and no key are the same, and
  `memory: 1GiB` is used as given, even with `nested: true`.
  `args.egress_proxy` True or False from settings is "requested", so
  `egress_choice` raises on vz. The hand-written lookups go away: the
  `clone_missing` one from 24728b8, and `session.sudo` (now
  `create.sudo`).
- `--name` from settings: if that sandbox exists, the error is the same
  as for `--name` on the command line ("exists already"). There is no
  `unique_sandbox_name` prompt, because the engineer named it.
- `--image` from settings: an image that `image gc` has removed gives
  the same error as on the command line.
- `session.preference_settings` accepts `settings` (a bool), and no
  longer `sudo`: a `session: {sudo: ...}` is an unknown key, which
  now warns (above) instead of failing.

## Steps

Each step is one commit with the sbx suite green.

1. **`cli.py` and its tests.** The SUPPRESS parse, `--config` and
   `--settings` handling, `session.settings`, camelCase and subcommand
   nesting, the type checks, unknown keys, mutually exclusive groups,
   the help suffix, `null`. Unit tests with small parsers; no command
   changes yet.
2. **`create` on the helper.** The flag pairs; `create.sudo` (and
   `session.sudo` removed); `--expire` null to `imageCache.expire`; `egressProxy`/`vmType`/`memory`/`disk` null as
   today; the `cloneMissing` lookup removed. README `create` section,
   `reference/example-settings.yaml` (`create:` lists every key,
   commented out, and `session: settings:`).
3. **`rm`, `exec`, `allow`, `cp` on the helper.** Their pairs.
4. **`image` (subcommands), `login`, `ls-remotes`, `resolve`, `ls`,
   `start`, `stop`, `status` on the helper.** Most have no options but
   NAME. They still get `--config`, `--settings`, and the settings
   behaviour for the next option that someone adds.
5. **README "Defaults from settings.yaml"**: the rule, `null`,
   `--no-settings`, `session.settings`, and the caveat-emptor
   sentence. `bin/wmf-sbx` usage: one line for `--no-settings`.
6. **The test that keeps it uniform.** It walks every command in
   `__main__.COMMANDS` through the parser its `main` hands to
   `cli.parse_args` (the test patches `parse_args` to catch it) and asserts: no
   `store_true` action is left; every option except `--config` and
   `--settings` maps to a key; and two options never map to one key. A
   second test gives each command's parser a settings file whose section
   sets every key to a wrong type. With `--no-settings`, `cli.parse_args`
   must return the code defaults, with no error. That shows that
   `--no-settings` really reads nothing.

## Follow-ups (not in this plan)

- **`resume` on argparse.** It splits its arguments by hand
  (`split_args`): LAUNCHER-FLAGs go to `bin/claude` and CLAUDE-ARGs
  after `--` to Claude Code. On argparse it gets the helper like the
  others. The pass-
  through parts become known options where they are ours (the launcher
  flags `bin/claude` takes: `--local-web[=PORTS]`, `--allow-post=HOST`,
  `--local-db`, ...), so `resume: {localWeb: true}` works, and
  `parse_known_args`/REMAINDER keeps `--` CLAUDE-ARGs as they are.
- **Positional arguments.** A default NAME of `.` for the commands that
  take one: `rm` included, because it asks first; `create PRIMARY` too.
  And settings defaults for positionals (`create: {primary: ...}`), by
  the same rule. Parsing problems to solve there: `allow NAME HOST...`
  and `exec NAME -- CMD` with NAME optional, and the guessing in
  `logs`' `split_args` (NAME first or last; resume's goes away with
  the first follow-up).
