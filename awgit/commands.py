"""The CLI, described as data — so an agent never has to scrape ``--help``.

``awgit commands --json`` emits every command, subcommand, flag, type, default
and choice list as machine-readable JSON. An agent reads it once and knows the
whole surface; it never parses help text, and it never guesses a flag.

**Introspected from the live argparse tree, never hand-maintained.** A written
list of commands is a second source of truth, and this repo has measured what
those cost: awgit's own README advertised ``awgit init`` for months while the
dispatcher had no such command, so the documented first step of setup exited 2
with "invalid choice". Deriving the description from the parser makes that class
unrepresentable — if a command is in the CLI it is in this output, and if it is
not in the CLI it cannot be.

The output carries a ``contract`` version. Bump it only when the SHAPE changes,
never when a command is added — a consumer keying on the shape should not have
to re-check because someone added a flag.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

#: Shape version of this document, NOT the awgit version. See module docstring.
CONTRACT = "1.0"

#: Commands that only READ. They must all accept ``--json``, because a read
#: whose only output is prose is a read an agent has to parse by eye. Asserted
#: by check_awgit_cli_contract ACC003 — kept here, next to the parser, so the
#: gate and the CLI cannot hold different opinions about what a read is.
READ_COMMANDS = frozenset({
    "status", "diff", "graph", "evidence", "bodies", "ledger",
    "merge-conflicts", "merge-preview", "commands", "version",
})


def _action_to_dict(action: argparse.Action) -> Optional[Dict[str, Any]]:
    """One argparse action as JSON, or None for the ones a caller cannot pass."""
    if isinstance(action, argparse._SubParsersAction):  # described separately
        return None
    if isinstance(action, argparse._HelpAction):
        return None
    out: Dict[str, Any] = {
        "name": action.dest,
        "flags": list(action.option_strings),  # empty list => positional
        "required": bool(action.required),
        "help": (action.help or "").strip(),
    }
    if action.choices:
        out["choices"] = [str(c) for c in action.choices]
    if action.default is not None and action.default != argparse.SUPPRESS:
        out["default"] = action.default if isinstance(
            action.default, (str, int, float, bool, list)) else str(action.default)
    if action.nargs is not None:
        out["nargs"] = action.nargs
    # A store_true/store_false takes no value; everything else does. Agents need
    # this to build a command line, and it is not inferable from the flags.
    out["takes_value"] = not isinstance(
        action, (argparse._StoreTrueAction, argparse._StoreFalseAction,
                 argparse._CountAction))
    if action.type is not None:
        out["type"] = getattr(action.type, "__name__", str(action.type))
    return out


def _subparsers_of(parser: argparse.ArgumentParser) -> Dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _help_texts(parser: argparse.ArgumentParser) -> Dict[str, str]:
    """Each subcommand's one-line help.

    ``add_parser(name, help=...)`` does NOT store the text on the child parser —
    it hangs a pseudo-action off the PARENT's ``_choices_actions``, and the
    child's ``description`` stays None. Reading ``sub.description`` therefore
    yields an empty string for every command, which is a description that
    describes nothing while looking like it worked.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {a.dest: (a.help or "").strip() for a in action._choices_actions}
    return {}


def describe_parser(parser: argparse.ArgumentParser) -> List[Dict[str, Any]]:
    """Every command under ``parser``, recursively, sorted by name."""
    out: List[Dict[str, Any]] = []
    helps = _help_texts(parser)
    for name, sub in sorted(_subparsers_of(parser).items()):
        args = [d for d in (_action_to_dict(a) for a in sub._actions) if d]
        entry: Dict[str, Any] = {
            "name": name,
            "help": helps.get(name) or (sub.description or "").strip(),
            "arguments": args,
            "reads_only": name in READ_COMMANDS,
        }
        nested = describe_parser(sub)
        if nested:
            entry["subcommands"] = nested
        out.append(entry)
    return out


def describe(parser: argparse.ArgumentParser, version: str) -> Dict[str, Any]:
    """The whole CLI as one JSON-able document."""
    return {
        "contract": CONTRACT,
        "tool": "awgit",
        "version": version,
        "commands": describe_parser(parser),
    }


def command_names(parser: argparse.ArgumentParser) -> List[str]:
    """Flat list of top-level command names — what the gate compares against."""
    return sorted(_subparsers_of(parser))


def render(parser: argparse.ArgumentParser, version: str, as_json: bool) -> str:
    doc = describe(parser, version)
    if as_json:
        return json.dumps(doc, indent=2, sort_keys=True)
    lines = [f"awgit {version} — {len(doc['commands'])} commands"]
    for cmd in doc["commands"]:
        mark = "r" if cmd["reads_only"] else " "
        lines.append(f"  {mark} {cmd['name']:<18} {cmd['help'][:74]}")
        for nested in cmd.get("subcommands", []):
            lines.append(f"      {nested['name']:<16} {nested['help'][:70]}")
    lines.append("")
    lines.append("  r = read-only (accepts --json).  Full detail: awgit commands --json")
    return "\n".join(lines)
