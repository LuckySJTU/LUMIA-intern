#!/usr/bin/env python3
"""internctl — intern 管理 CLI 入口

用法:
  internctl create <name> [--project <project>]
  internctl init <name> [--project <project>]
  internctl list [--json]
  internctl status <name> [--json]
  internctl delete <name> [--confirm]
  internctl setup {status|doctor|apply|export}
  internctl workspace {list|create|enable|disable|doctor|delete|mode|migrate}
  internctl metadata resolve
  internctl daemon {start|stop|restart|status}
  internctl session {start|status|stop}
  internctl helper {start|stop|status|migrate|invite-owner}
"""

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "internal":
        from commands import internal
        sys.exit(internal.main(argv[1:]))

    parser = argparse.ArgumentParser(
        prog="internctl",
        description="Manage intern lifecycle",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    from commands import create, list_cmd, status, delete, setup, init, reset_hook_env, skill, team, bind, workspace, metadata, daemon, session, helper
    create.setup_parser(subparsers)
    init.setup_parser(subparsers)
    list_cmd.setup_parser(subparsers)
    status.setup_parser(subparsers)
    team.setup_parser(subparsers)
    bind.setup_parser(subparsers)
    delete.setup_parser(subparsers)
    setup.setup_parser(subparsers)
    reset_hook_env.setup_parser(subparsers)
    skill.setup_parser(subparsers)
    workspace.setup_parser(subparsers)
    metadata.setup_parser(subparsers)
    daemon.setup_parser(subparsers)
    session.setup_parser(subparsers)
    helper.setup_parser(subparsers)

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    from lib.enterprise_boundary import (
        TOP_LEVEL_ADMIN_COMMANDS,
        emit_admin_rejection,
        enterprise_mode_active,
    )

    if enterprise_mode_active() and args.command in TOP_LEVEL_ADMIN_COMMANDS:
        sys.exit(emit_admin_rejection(args.command, json_output=bool(getattr(args, "json", False))))

    if hasattr(args, 'func'):
        sys.exit(args.func(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
