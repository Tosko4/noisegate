from __future__ import annotations

import argparse
import datetime as dt
import sys

from release_tools import ReleaseError, add_root_arg, prepare_release, repo_root_from_args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump Noisegate version files and promote changelog notes."
    )
    add_root_arg(parser)
    parser.add_argument("version", help="semantic version, with or without leading v")
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="release date YYYY-MM-DD",
    )
    args = parser.parse_args(argv)

    try:
        notes = prepare_release(
            repo_root_from_args(args.root),
            args.version,
            release_date=args.date,
        )
    except ReleaseError as exc:
        print(f"prepare release failed: {exc}", file=sys.stderr)
        return 1

    print(f"prepared release v{args.version.removeprefix('v')}")
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
