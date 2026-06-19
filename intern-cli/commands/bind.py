"""internctl bind — bind coordinator to workspace team."""

from __future__ import annotations

import argparse

from commands import team


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("bind", help="绑定 coordinator 到 workspace team")
    parser.add_argument("coordinator_id", help="coordinator id")
    parser.add_argument("team_id", help="team name")
    parser.add_argument("--project", default="axis_intern_agents", help="项目名称")
    parser.add_argument("--json", dest="as_json", action="store_true", help="输出更新后的 coordinator metadata")
    parser.set_defaults(func=team.run_bind)
