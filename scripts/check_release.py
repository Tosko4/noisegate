from __future__ import annotations

import argparse
import sys

from release_tools import ReleaseError, add_root_arg, repo_root_from_args, validate_release_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Noisegate release metadata consistency.")
    add_root_arg(parser)
    parser.add_argument("--expected-version", default=None)
    parser.add_argument("--tag", default=None, help="release tag, e.g. v0.1.0")
    parser.add_argument(
        "--no-changelog",
        action="store_true",
        help="do not require changelog notes",
    )
    args = parser.parse_args(argv)

    try:
        state = validate_release_state(
            repo_root_from_args(args.root),
            expected_version=args.expected_version,
            tag=args.tag,
            require_changelog=not args.no_changelog,
        )
    except ReleaseError as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1

    print(f"release metadata ok: {state.version}")
    for file_name, version in sorted(state.files.items()):
        print(f"- {file_name}: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
