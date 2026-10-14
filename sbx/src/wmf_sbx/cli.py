#!/usr/bin/env python3
"""Defaults for command-line options from settings.yaml
(lima-port/PLAN-SETTINGS-DEFAULTS.md).

Every command builds its argparse parser and calls `parse_args`. For an
option `--some-flag` of `wmf-sbx VERB`, the settings key is
`VERB.someFlag` (the `dest` in camelCase); a subcommand nests, so `image
build --keep-builder` is `image: {build: {keepBuilder: true}}`. The
command line wins over settings.yaml, which wins over the code's own
default; `null` in settings is the same as no key. `--no-settings`, or
`session: {settings: false}`, turns the settings defaults off.

The helper does it with `parser.set_defaults`: the command reads `args.x`
as before and never knows where the value came from.
"""

import argparse
import json
import sys

from . import color as color_mod
from . import resolve as resolve_mod

# The options that say which file to read and whether to read it: they
# cannot come from that file.
OWN_DESTS = ("config", "settings")


class SettingsError(Exception):
    pass


class _Pass1Error(Exception):
    pass


def camel(dest):
    """`clone_missing` -> `cloneMissing`."""
    first, *rest = dest.split("_")
    return first + "".join(p[:1].upper() + p[1:] for p in rest)


def _warn(msg):
    print(color_mod.warning(msg), file=sys.stderr)


def subparsers(parser):
    """{name: parser} of `parser`'s subcommands (aliases left out)."""
    out = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            seen = set()
            for name, sub in action.choices.items():
                if id(sub) not in seen:
                    seen.add(id(sub))
                    out[name] = sub
    return out


def all_parsers(parser):
    """`parser` and its subcommands' parsers, depth first."""
    yield parser
    for sub in subparsers(parser).values():
        yield from all_parsers(sub)


def settable(parser):
    """{key: action} of the options of `parser` that settings.yaml can
    give a default to: all but -h, --config, --settings, and positionals.
    A store_true (or other constant) flag cannot be undone on the command
    line once settings sets it, so it is refused: use
    argparse.BooleanOptionalAction."""
    out = {}
    for action in parser._actions:
        if (not action.option_strings or action.dest in OWN_DESTS
                or isinstance(action, (argparse._HelpAction, argparse._SubParsersAction))):
            continue
        if isinstance(action, (argparse._StoreConstAction, argparse._CountAction)):
            raise AssertionError(
                f"{parser.prog} {'/'.join(action.option_strings)}: a constant flag cannot be "
                f"undone on the command line; use argparse.BooleanOptionalAction")
        out[camel(action.dest)] = action
    return out


def _add_own_options(parser, config_default):
    """--config and --settings/--no-settings on every parser that takes
    options of its own (a parser with subcommands gets them on each
    subcommand, where they can be given after it)."""
    subs = subparsers(parser)
    if subs:
        for sub in subs.values():
            _add_own_options(sub, config_default)
        return
    parser.add_argument("--config", metavar="PATH", default=config_default,
                        help=f"the settings file (default: {config_default})")
    parser.add_argument("--settings", action=argparse.BooleanOptionalAction, default=None,
                        help="take option defaults from the settings file (default: "
                             "yes, unless its session.settings is false)")


def _given(parser, argv):
    """The options and arguments in `argv`, as {dest: value}: a parse
    with every default replaced by SUPPRESS, and nothing required, so
    that `-h` and a missing argument do not stop it. Raises _Pass1Error
    when the command line is wrong anyway."""
    saved = []
    try:
        for p in all_parsers(parser):
            saved.append((p, "_defaults", p._defaults))
            p._defaults = {}
            for name, fn in (("print_help", lambda *a, **k: None),
                             ("exit", lambda *a, **k: None)):
                saved.append((p, name, None))
                setattr(p, name, fn)

            def error(message):
                raise _Pass1Error(message)
            saved.append((p, "error", None))
            p.error = error
            for action in p._actions:
                saved.append((action, "default", action.default))
                saved.append((action, "required", action.required))
                action.default = argparse.SUPPRESS
                action.required = False
            for group in p._mutually_exclusive_groups:
                saved.append((group, "required", group.required))
                group.required = False
        return vars(parser.parse_args(argv))
    finally:
        for obj, attr, value in reversed(saved):
            if attr in ("print_help", "exit", "error"):
                del obj.__dict__[attr]
            else:
                setattr(obj, attr, value)


def _check(action, value, where):
    """Raises SettingsError unless `value` fits `action`."""
    if isinstance(action, argparse.BooleanOptionalAction):
        ok, want = isinstance(value, bool), "true or false"
    elif isinstance(action, argparse._AppendAction) or action.nargs in ("*", "+") \
            or isinstance(action.nargs, int):
        ok = isinstance(value, list) and all(
            isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in value)
        want = "a list"
    elif action.type is int:
        ok, want = isinstance(value, int) and not isinstance(value, bool), "an integer"
    else:
        ok, want = isinstance(value, str), "a string"
    if ok and action.choices is not None and value not in action.choices:
        ok, want = False, "one of " + ", ".join(map(str, action.choices))
    if not ok:
        raise SettingsError(f"settings.yaml {where}: {value!r}: want {want}")


def _show(value):
    return value if isinstance(value, str) else json.dumps(value)


def _apply(parser, path, section, given, use, warn):
    """Gives `parser` the defaults of settings.yaml `section` (at `path`,
    e.g. "image.build"), and its options the help suffix."""
    section = section if use else None
    if section is not None and not isinstance(section, dict):
        raise SettingsError(f"settings.yaml {path}: want a mapping")
    section = section or {}
    options = settable(parser)
    subs = subparsers(parser)
    for key, value in section.items():
        if key not in options and key not in subs:
            known = ", ".join(sorted(list(options) + list(subs))) or "none"
            warn(f"warning: settings.yaml {path}.{key}: `{parser.prog}` has no such option; "
                 f"ignored (it has: {known})")
    values = {}
    for key, action in options.items():
        value = section.get(key)
        if value is not None:
            _check(action, value, f"{path}.{key}")
            values[key] = value
    # A mutually exclusive group: a member on the command line drops the
    # settings of the others; two members set in settings are an error.
    for group in parser._mutually_exclusive_groups:
        keys = [camel(a.dest) for a in group._group_actions]
        if any(a.dest in given for a in group._group_actions):
            for k in keys:
                values.pop(k, None)
        else:
            on = [k for k in keys if values.get(k) not in (None, False)]
            if len(on) > 1:
                raise SettingsError(f"settings.yaml: {' and '.join(f'{path}.{k}' for k in on)} "
                                    f"cannot both be set; they are mutually exclusive")
    for key, action in options.items():
        suffix = f"[settings: {path}.{key}"
        if key in values:
            suffix += f": {_show(values[key])}"
            if action.dest not in given:
                parser.set_defaults(**{action.dest: values[key]})
        suffix = (suffix + "]").replace("%", "%%")
        if action.help is not argparse.SUPPRESS:
            action.help = f"{action.help} {suffix}" if action.help else suffix
    for name, sub in subs.items():
        _apply(sub, f"{path}.{name}", section.get(name), given, use, warn)


def parse_args(parser, argv, section, config_default=None, warn=_warn):
    """(args, config). Adds --config and --settings/--no-settings to
    `parser`, and gives every other option its default from settings.yaml
    `section` (the verb: "create", or "image" for a parser whose
    subcommands are `image: {build: ...}`). A settings file that cannot be
    read, or a value of the wrong type, exits with status 1."""
    config_default = config_default or resolve_mod.DEFAULT_CONFIG
    _add_own_options(parser, config_default)
    try:
        given = _given(parser, argv)
    except _Pass1Error:
        parser.parse_args(argv)   # the same error, with the usage
        given = {}
    try:
        config = resolve_mod.load_config(given.get("config", config_default))
        use = given.get("settings")
        if use is None:
            session = config.get("session")
            use = session.get("settings") if isinstance(session, dict) else None
            if use is not None and not isinstance(use, bool):
                raise SettingsError(f"settings.yaml session.settings: {use!r}: want true "
                                    f"or false")
            use = use is not False
        _apply(parser, section, config.get(section), given, use, warn)
    except (SettingsError, resolve_mod.ResolutionError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        raise SystemExit(1)
    return parser.parse_args(argv), config
