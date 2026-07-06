from __future__ import annotations

import argparse
import sys

from release_tools import (
    ReleaseError,
    add_root_arg,
    check_contributors_file,
    git_contributor_names,
    repo_root_from_args,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify git author names are documented in CONTRIBUTORS.md."
    )
    add_root_arg(parser)
    args = parser.parse_args(argv)
    root = repo_root_from_args(args.root)

    try:
        names = git_contributor_names(root)
        missing = check_contributors_file(root, contributor_names=set(names))
    except (ReleaseError, RuntimeError) as exc:
        print(f"contributor check failed: {exc}", file=sys.stderr)
        return 1

    print("contributors seen:")
    for name in names:
        print(f"- {name}")
    if missing:
        print("missing from CONTRIBUTORS.md:", file=sys.stderr)
        for name in missing:
            print(f"- {name}", file=sys.stderr)
        return 1
    print("contributors ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
