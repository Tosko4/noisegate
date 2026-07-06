from __future__ import annotations

import argparse
import sys
from pathlib import Path

from release_tools import ReleaseError, add_root_arg, repo_root_from_args, write_release_notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract release notes for a version from CHANGELOG.md."
    )
    add_root_arg(parser)
    parser.add_argument("version", help="semantic version, with or without leading v")
    parser.add_argument("--output", default="dist/release-notes.md")
    args = parser.parse_args(argv)

    try:
        write_release_notes(repo_root_from_args(args.root), args.version, Path(args.output))
    except ReleaseError as exc:
        print(f"release notes failed: {exc}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
