#!/usr/bin/env python3
"""The MediaWiki setup of a Lima sandbox (lima-port/HANDOFF-LIMA.md §7,
phase 5).

`wmf-sbx create` runs it after the clones (repos.py): the links of the
extensions and skins into core, composer.local.json, `composer update`,
`npm ci`, `.env`, the install, phpunit.xml and the api-testing config.
The steps are setup.py's, unchanged; setup.py runs in the VM as the
agent, outside nono, before the first session (D7). It is sent on stdin
to `python3 -`, so it is not in the golden image, and a change to it
needs no new image.

The plan is kit.build_plan's: the repos, their canonical names, the links
(computed on the host, where the manifests are) and the ':ro' flags. It
is kept in the VM at setup.SANDBOX_PLAN_FILE.
"""

import json
import os

from . import image as image_mod
from . import kit as kit_mod
from . import setup as setup_mod
from . import vm as vm_mod

SETUP_SOURCE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "setup.py")


class SetupError(Exception):
    """The MediaWiki setup failed in the VM."""


def build_plan(resolved_for_kit, links, readonly, requested, primary, reset_all):
    """The plan for setup.py --lima. resolved_for_kit is [(canonical, path)]."""
    return kit_mod.build_plan(
        resolved_for_kit, readonly_dirs=readonly, links=links, primary=primary,
        requested=requested, reset_all=reset_all)


def run_setup(name, plan, lima=None, env=None):
    """Write the plan into the agent's home, and run setup.py --lima as the
    agent. Its output goes to the terminal; the log and the status file
    are in the VM, in setup.LIMA_LOG_DIR. Raises SetupError on failure."""
    write = vm_mod.agent_argv(
        ["sh", "-c", f"umask 077 && cat > {setup_mod.SANDBOX_PLAN_FILE}"])
    res = vm_mod.shell(name, write, lima=lima, input=json.dumps(plan, indent=2) + "\n",
                       check=False, capture=False)
    if res.returncode != 0:
        raise SetupError(f"could not write the plan in the VM (exit {res.returncode})")
    with open(SETUP_SOURCE, encoding="utf-8") as f:
        source = f.read()
    argv = vm_mod.agent_argv(["python3", "-", "--lima", setup_mod.SANDBOX_PLAN_FILE],
                             env=image_mod.guest_proxy_env(env))
    res = vm_mod.shell(name, argv, lima=lima, input=source, check=False, capture=False)
    if res.returncode != 0:
        raise SetupError(
            f"the MediaWiki setup failed (exit {res.returncode}); the log is "
            f"{setup_mod.LIMA_LOG_DIR}/{setup_mod.SETUP_LOG_NAME} in the VM")
