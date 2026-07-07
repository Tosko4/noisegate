from __future__ import annotations

import argparse
import sys
from pathlib import Path

from release_tools import ReleaseError, add_root_arg, repo_root_from_args, write_release_notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build PR-aware release notes for a version from CHANGELOG.md "
            "and GitHub PR metadata."
        )
    )
    add_root_arg(parser)
    parser.add_argument("version", help="semantic version, with or without leading v")
    parser.add_argument("--output", default="dist/release-notes.md")
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repository for gh PR lookups, for example Tosko4/noisegate",
    )
    args = parser.parse_args(argv)

    try:
        write_release_notes(
            repo_root_from_args(args.root),
            args.version,
            Path(args.output),
            repo=args.repo,
        )
    except ReleaseError as exc:
        print(f"release notes failed: {exc}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
