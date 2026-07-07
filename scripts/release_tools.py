from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404
import tomllib
from dataclasses import dataclass
from pathlib import Path

VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$"
)
PY_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
PLUGIN_VERSION_RE = re.compile(r'(?m)^version:\s*["\']?([^"\'\n]+)["\']?\s*$')
UV_LOCK_VERSION_RE = re.compile(
    r'(?ms)(\[\[package\]\]\s+name\s*=\s*"noisegate-hermes"\s+version\s*=\s*")[^"]+(")'
)
CHANGELOG_HEADING_RE = re.compile(
    r"(?m)^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)
CONTRIBUTOR_BULLET_RE = re.compile(r"(?m)^-\s+(?P<name>[^<\n]+?)(?:\s+<[^>]+>)?\s*$")
GITHUB_OWNER_REPO_RE = re.compile(r"^(?P<owner>[^/]+)/(?P<name>[^/]+)$")
GITHUB_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?(?P<login>[^@]+)@users\.noreply\.github\.com$",
    re.IGNORECASE,
)

GH_PR_JSON_FIELDS = "number,title,author,mergedAt,mergeCommit,url,body"
GH_PR_GRAPHQL_PAGE_SIZE = 100

CATEGORY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Security / Safety", ("security", "safety", "harden", "protect")),
    (
        "Release / Packaging",
        (
            "release",
            "publish",
            "publishing",
            "pypi",
            "npm",
            "package",
            "packages",
            "packaging",
            "installer",
            "distribution",
            "trusted publishing",
            "provenance",
        ),
    ),
    ("Internal / CI", ("ci", "workflow", "actionlint", "contributors")),
    ("Documentation", ("readme", "docs", "documentation")),
    ("Fixed", ("fix", "fixed", "bug", "fail", "failure", "collision")),
    ("Added", ("add", "added", "new", "introduce", "introduced")),
)

VERSION_FILES = (
    "pyproject.toml",
    "noisegate/_version.py",
    "noisegate/plugin.yaml",
    "uv.lock",
    "npm/noisegate/package.json",
    "npm/noisegate/package-lock.json",
)


class ReleaseError(RuntimeError):
    """Raised when release metadata is invalid."""


@dataclass(frozen=True)
class ReleaseState:
    version: str
    files: dict[str, str]


@dataclass(frozen=True)
class ReleasePullRequest:
    number: int
    title: str
    author: str
    merged_at: str
    merge_commit: str
    url: str
    body: str

    @property
    def author_mention(self) -> str:
        return self.author if self.author.startswith("@") else f"@{self.author}"


@dataclass(frozen=True)
class ReleasePullRequestSummary:
    previous_tag: str | None
    tag: str
    included: list[ReleasePullRequest]
    new_contributors: list[ReleasePullRequest]


def normalize_version(value: str) -> str:
    value = value.strip()
    if value.startswith("v"):
        value = value[1:]
    if not VERSION_RE.match(value):
        raise ReleaseError(f"Invalid semantic version: {value!r}")
    return value


def read_versions(root: Path) -> dict[str, str]:
    root = Path(root)
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        project_version = str(pyproject["project"]["version"])
    except KeyError as exc:  # pragma: no cover - defensive against invalid metadata
        raise ReleaseError("pyproject.toml is missing [project].version") from exc

    version_py_text = (root / "noisegate" / "_version.py").read_text(encoding="utf-8")
    version_py = _match_required(PY_VERSION_RE, version_py_text, "noisegate/_version.py")

    plugin_text = (root / "noisegate" / "plugin.yaml").read_text(encoding="utf-8")
    plugin_version = _match_required(PLUGIN_VERSION_RE, plugin_text, "noisegate/plugin.yaml")
    uv_lock_version = _uv_lock_project_version(root)
    npm_package_version = _json_file_version(root / "npm" / "noisegate" / "package.json")
    npm_lock_version = _json_file_version(root / "npm" / "noisegate" / "package-lock.json")

    return {
        "pyproject.toml": normalize_version(project_version),
        "noisegate/_version.py": normalize_version(version_py),
        "noisegate/plugin.yaml": normalize_version(plugin_version),
        "uv.lock": normalize_version(uv_lock_version),
        "npm/noisegate/package.json": normalize_version(npm_package_version),
        "npm/noisegate/package-lock.json": normalize_version(npm_lock_version),
    }


def validate_release_state(
    root: Path,
    *,
    expected_version: str | None = None,
    tag: str | None = None,
    require_changelog: bool = True,
) -> ReleaseState:
    versions = read_versions(root)
    unique = set(versions.values())
    if len(unique) != 1:
        detail = ", ".join(f"{path}={version}" for path, version in sorted(versions.items()))
        raise ReleaseError(f"Version mismatch: {detail}")

    version = unique.pop()
    if expected_version is not None and version != normalize_version(expected_version):
        expected = normalize_version(expected_version)
        raise ReleaseError(f"Expected version {expected}, found {version}")

    if tag is not None and version != normalize_version(tag):
        raise ReleaseError(f"Tag {tag!r} does not match package version {version}")

    if require_changelog:
        notes = changelog_notes_for_version(root, version)
        if not notes.strip():
            raise ReleaseError(f"CHANGELOG.md has no notes for version {version}")

    return ReleaseState(version=version, files=versions)


def set_versions(root: Path, version: str) -> None:
    version = normalize_version(version)
    root = Path(root)
    _replace_file(
        root / "pyproject.toml",
        re.compile(r'(?m)^(version\s*=\s*)["\'][^"\']+["\']\s*$'),
        rf'\1"{version}"',
        "pyproject.toml [project].version",
    )
    _replace_file(
        root / "noisegate" / "_version.py",
        PY_VERSION_RE,
        f'__version__ = "{version}"',
        "noisegate/_version.py __version__",
    )
    _replace_file(
        root / "noisegate" / "plugin.yaml",
        PLUGIN_VERSION_RE,
        f'version: "{version}"',
        "noisegate/plugin.yaml version",
    )
    _replace_file(
        root / "uv.lock",
        UV_LOCK_VERSION_RE,
        rf'\g<1>{version}\2',
        "uv.lock noisegate-hermes version",
    )
    _set_json_file_version(root / "npm" / "noisegate" / "package.json", version)
    _set_json_file_version(root / "npm" / "noisegate" / "package-lock.json", version)


def prepare_release(root: Path, version: str, *, release_date: str) -> str:
    version = normalize_version(version)
    set_versions(root, version)
    notes = _promote_unreleased_changelog(root, version, release_date)
    validate_release_state(root, expected_version=version)
    return notes


def changelog_notes_for_version(root: Path, version: str) -> str:
    version = normalize_version(version)
    path = Path(root) / "CHANGELOG.md"
    if not path.exists():
        raise ReleaseError("CHANGELOG.md is missing")
    text = path.read_text(encoding="utf-8")
    headings = list(CHANGELOG_HEADING_RE.finditer(text))
    for index, match in enumerate(headings):
        if match.group("version") == version:
            start = match.end()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[start:end].strip()
    raise ReleaseError(f"CHANGELOG.md has no section for version {version}")


def git_contributor_names(root: Path) -> list[str]:
    root = Path(root)
    if not root.exists():
        raise ReleaseError(
            f"repository root does not exist while reading contributor names: {root}"
        )
    git = _git_executable()
    # git is resolved to an executable path; shell remains disabled.
    try:
        proc = subprocess.run(  # nosec B603
            [git, "log", "--no-merges", "--format=%aN%x00%aE"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or f"exit code {exc.returncode}"
        raise ReleaseError(f"git log failed while reading contributor names: {detail}") from exc
    except OSError as exc:
        raise ReleaseError(f"git log failed while reading contributor names: {exc}") from exc
    return sorted(
        {
            _normalized_contributor_name(line)
            for line in proc.stdout.splitlines()
            if _normalized_contributor_name(line)
        }
    )


def _normalized_contributor_name(git_log_line: str) -> str:
    name, separator, email = git_log_line.strip().partition("\x00")
    if separator:
        email = email.strip()
        if email.lower() == "codex@openai.com":
            return ""
        match = GITHUB_NOREPLY_RE.match(email)
        if match:
            return match.group("login")
    return name.strip()


def contributors_file_names(root: Path) -> set[str]:
    path = Path(root) / "CONTRIBUTORS.md"
    if not path.exists():
        raise ReleaseError("CONTRIBUTORS.md is missing")
    names: set[str] = set()
    for match in CONTRIBUTOR_BULLET_RE.finditer(path.read_text(encoding="utf-8")):
        name = match.group("name").strip()
        if name and not name.lower().startswith("github actions"):
            names.add(name)
    return names


def check_contributors_file(root: Path, contributor_names: set[str] | None = None) -> list[str]:
    root = Path(root)
    actual = (
        contributor_names if contributor_names is not None else set(git_contributor_names(root))
    )
    documented = contributors_file_names(root)
    bot_suffixes = ("[bot]",)
    missing = sorted(
        name
        for name in actual
        if name and not name.endswith(bot_suffixes) and name not in documented
    )
    return missing


def write_release_notes(
    root: Path,
    version: str,
    output: Path,
    *,
    repo: str | None = None,
) -> None:
    notes = release_notes_for_version(root, version, repo=repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(notes + "\n", encoding="utf-8")


def release_notes_for_version(root: Path, version: str, *, repo: str | None = None) -> str:
    notes = changelog_notes_for_version(root, version).strip()
    try:
        summary = release_pull_request_summary(root, version, repo=repo)
    except ReleaseError as exc:
        raise ReleaseError(
            "Could not build PR-aware release notes; ensure gh is authenticated and "
            f"release tags are available: {exc}"
        ) from exc
    if not summary.included:
        raise ReleaseError(
            f"No merged pull requests found for {summary.tag}; release notes must list "
            "the PRs included since the previous release"
        )
    return "\n\n".join(
        part
        for part in (notes, _format_update_section(), _format_release_pr_section(summary))
        if part.strip()
    )


def release_pull_request_summary(
    root: Path,
    version: str,
    *,
    repo: str | None = None,
) -> ReleasePullRequestSummary:
    root = Path(root)
    tag = f"v{normalize_version(version)}"
    previous_tag = _previous_release_tag(root, tag)
    rev_range = f"{previous_tag}..{tag}" if previous_tag else tag
    commits = set(_git_output(root, ["rev-list", rev_range]).splitlines())
    if not commits:
        raise ReleaseError(f"No commits found for release range {rev_range}")

    merged_prs = _merged_pull_requests(root, repo=repo)
    included = [pr for pr in merged_prs if pr.merge_commit in commits]
    included.sort(key=lambda pr: pr.merged_at)

    first_pr_by_author: dict[str, ReleasePullRequest] = {}
    for pr in sorted(merged_prs, key=lambda item: item.merged_at):
        first_pr_by_author.setdefault(pr.author.lower(), pr)
    new_contributors = [
        pr
        for pr in included
        if first_pr_by_author.get(pr.author.lower()) == pr
        and not pr.author.endswith("[bot]")
    ]

    return ReleasePullRequestSummary(
        previous_tag=previous_tag,
        tag=tag,
        included=included,
        new_contributors=new_contributors,
    )


def _format_update_section() -> str:
    return "\n".join(
        (
            "## How to update",
            "",
            "For an existing Hermes Agent installation, run:",
            "",
            "```bash",
            "uvx --from noisegate-hermes noisegate install-hermes",
            "```",
            "",
            "Preview the exact commands first:",
            "",
            "```bash",
            "uvx --from noisegate-hermes noisegate install-hermes --dry-run",
            "```",
            "",
            "npm users can use the wrapper instead:",
            "",
            "```bash",
            "npx -p noisegate-hermes noisegate install-hermes",
            "```",
            "",
            "The installer finds `hermes` on `PATH`, resolves the Python environment "
            "that runs Hermes, installs `noisegate-hermes` there, enables the "
            "`noisegate` plugin, and runs `noisegate doctor`.",
            "",
            "If Hermes is running as a long-lived gateway/service, restart or reload "
            "that Hermes process through your normal maintenance flow after "
            "installing so the plugin/config change is picked up. Avoid interrupting "
            "in-flight agent work.",
        )
    )


def _format_release_pr_section(summary: ReleasePullRequestSummary) -> str:
    lines = ["## Included pull requests"]
    range_label = (
        f"{summary.previous_tag}...{summary.tag}" if summary.previous_tag else summary.tag
    )
    lines.append(f"Release range: `{range_label}`.")
    grouped: dict[str, list[ReleasePullRequest]] = {}
    for pr in summary.included:
        grouped.setdefault(_release_pr_category(pr), []).append(pr)

    category_order = (
        "Security / Safety",
        "Release / Packaging",
        "Added",
        "Changed",
        "Fixed",
        "Documentation",
        "Internal / CI",
    )
    for category in category_order:
        prs = grouped.pop(category, [])
        if not prs:
            continue
        lines.extend(("", f"### {category}"))
        for pr in prs:
            lines.append(
                f"- [#{pr.number}]({pr.url}) {pr.title} — {pr.author_mention}"
            )
    for category, prs in sorted(grouped.items()):
        lines.extend(("", f"### {category}"))
        for pr in prs:
            lines.append(
                f"- [#{pr.number}]({pr.url}) {pr.title} — {pr.author_mention}"
            )

    lines.extend(("", "## New contributors"))
    if summary.new_contributors:
        for pr in summary.new_contributors:
            lines.append(
                f"- {pr.author_mention} made their first merged Noisegate PR in "
                f"#{pr.number} — {pr.title}."
            )
    else:
        lines.append("- No new contributors in this release range.")
    return "\n".join(lines)


def _release_pr_category(pr: ReleasePullRequest) -> str:
    title = pr.title.lower()
    body = pr.body.lower()
    for category, terms in CATEGORY_TERMS:
        if _contains_category_term(title, terms):
            return category
    for category, terms in CATEGORY_TERMS:
        if _contains_category_term(body, terms):
            return category
    return "Changed"


def _contains_category_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(_contains_whole_term(text, term) for term in terms)


def _contains_whole_term(text: str, term: str) -> bool:
    pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _previous_release_tag(root: Path, tag: str) -> str | None:
    try:
        return _git_output(root, ["describe", "--tags", "--abbrev=0", f"{tag}^"]).strip()
    except ReleaseError:
        return None


def _merged_pull_requests(root: Path, *, repo: str | None = None) -> list[ReleasePullRequest]:
    gh = _gh_executable()
    owner, name = _github_repo_parts(root, gh, repo=repo)
    prs: list[ReleasePullRequest] = []
    cursor: str | None = None
    while True:
        page = _merged_pull_request_page(root, gh, owner=owner, name=name, cursor=cursor)
        prs.extend(_release_pull_requests_from_items(page["nodes"]))
        if not page["has_next_page"]:
            return prs
        end_cursor = page["end_cursor"]
        cursor = str(end_cursor) if end_cursor is not None else None
        if not cursor:  # pragma: no cover - defensive against malformed GitHub data
            raise ReleaseError("gh pr pagination did not return an end cursor")


def _github_repo_parts(root: Path, gh: str, *, repo: str | None) -> tuple[str, str]:
    if repo:
        match = GITHUB_OWNER_REPO_RE.match(repo)
        if not match:
            raise ReleaseError(
                f"GitHub repository must use owner/name form, got {repo!r}"
            )
        return match.group("owner"), match.group("name")

    try:
        proc = subprocess.run(  # nosec B603
            [gh, "repo", "view", "--json", "owner,name"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or f"exit code {exc.returncode}"
        raise ReleaseError(f"gh repo view failed while resolving repository: {detail}") from exc
    except OSError as exc:
        raise ReleaseError(f"gh repo view failed while resolving repository: {exc}") from exc
    try:
        data = json.loads(proc.stdout)
        owner = data["owner"]["login"]
        name = data["name"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"gh repo view returned invalid JSON: {exc}") from exc
    return str(owner), str(name)


def _merged_pull_request_page(
    root: Path,
    gh: str,
    *,
    owner: str,
    name: str,
    cursor: str | None,
) -> dict[str, object]:
    query = _merged_pr_query(include_cursor=cursor is not None)
    argv = [
        gh,
        "api",
        "graphql",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"limit={GH_PR_GRAPHQL_PAGE_SIZE}",
        "-f",
        f"query={query}",
    ]
    if cursor is not None:
        argv.extend(("-f", f"cursor={cursor}"))
    try:
        proc = subprocess.run(  # nosec B603
            argv,
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or f"exit code {exc.returncode}"
        raise ReleaseError(f"gh api graphql failed while reading merged PRs: {detail}") from exc
    except OSError as exc:
        raise ReleaseError(f"gh api graphql failed while reading merged PRs: {exc}") from exc
    try:
        data = json.loads(proc.stdout)
        pull_requests = data["data"]["repository"]["pullRequests"]
        page_info = pull_requests["pageInfo"]
        nodes = pull_requests["nodes"]
        return {
            "nodes": nodes,
            "has_next_page": bool(page_info["hasNextPage"]),
            "end_cursor": page_info.get("endCursor"),
        }
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"gh api graphql returned invalid PR JSON: {exc}") from exc


def _merged_pr_query(*, include_cursor: bool) -> str:
    cursor_argument = ", after: $cursor" if include_cursor else ""
    cursor_variable = ", $cursor: String" if include_cursor else ""
    return f"""
query($owner: String!, $name: String!, $limit: Int!{cursor_variable}) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(
      states: MERGED,
      first: $limit{cursor_argument},
      orderBy: {{field: UPDATED_AT, direction: DESC}}
    ) {{
      nodes {{
        number
        title
        body
        mergedAt
        url
        author {{ login }}
        mergeCommit {{ oid }}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""


def _release_pull_requests_from_items(items: object) -> list[ReleasePullRequest]:
    if not isinstance(items, list):
        raise ReleaseError("merged PR JSON did not contain a node list")
    prs: list[ReleasePullRequest] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        author = item.get("author") or {}
        merge_commit = item.get("mergeCommit") or {}
        login = author.get("login") if isinstance(author, dict) else None
        oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        if not login or not oid:
            continue
        prs.append(
            ReleasePullRequest(
                number=int(item["number"]),
                title=str(item["title"]),
                author=str(login),
                merged_at=str(item["mergedAt"]),
                merge_commit=str(oid),
                url=str(item["url"]),
                body=str(item.get("body") or ""),
            )
        )
    return prs


def _git_output(root: Path, argv: list[str]) -> str:
    git = _git_executable()
    try:
        proc = subprocess.run(  # nosec B603
            [git, *argv],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or f"exit code {exc.returncode}"
        raise ReleaseError(f"git {' '.join(argv)} failed: {detail}") from exc
    except OSError as exc:
        raise ReleaseError(f"git {' '.join(argv)} failed: {exc}") from exc
    return proc.stdout.strip()


def _gh_executable() -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise ReleaseError("gh executable was not found on PATH")
    return str(Path(gh).resolve())


def _promote_unreleased_changelog(root: Path, version: str, release_date: str) -> str:
    path = Path(root) / "CHANGELOG.md"
    if not path.exists():
        path.write_text("# Changelog\n\n## [Unreleased]\n\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if re.search(rf"(?m)^## \[{re.escape(version)}\]", text):
        return changelog_notes_for_version(root, version)

    headings = list(CHANGELOG_HEADING_RE.finditer(text))
    unreleased = next((h for h in headings if h.group("version") == "Unreleased"), None)
    if unreleased is None:
        text = text.rstrip() + "\n\n## [Unreleased]\n\n"
        path.write_text(text, encoding="utf-8")
        headings = list(CHANGELOG_HEADING_RE.finditer(text))
        unreleased = next(h for h in headings if h.group("version") == "Unreleased")

    next_heading = next((h for h in headings if h.start() > unreleased.start()), None)
    start = unreleased.end()
    end = next_heading.start() if next_heading else len(text)
    unreleased_notes = text[start:end].strip()
    if not unreleased_notes:
        raise ReleaseError("CHANGELOG.md Unreleased section has no release notes")

    before = text[: unreleased.end()].rstrip()
    after = text[end:].lstrip("\n")
    replacement = (
        f"{before}\n\n"
        f"## [{version}] - {release_date}\n\n"
        f"{unreleased_notes}\n\n"
    )
    path.write_text(replacement + after, encoding="utf-8")
    return unreleased_notes


def _match_required(pattern: re.Pattern[str], text: str, label: str) -> str:
    match = pattern.search(text)
    if not match:
        raise ReleaseError(f"Could not find version in {label}")
    return match.group(1)


def _uv_lock_project_version(root: Path) -> str:
    path = root / "uv.lock"
    if not path.exists():
        raise ReleaseError("uv.lock is missing")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for package in data.get("package", []):
        if package.get("name") == "noisegate-hermes":
            return str(package.get("version", ""))
    raise ReleaseError("uv.lock has no noisegate-hermes package entry")


def _json_file_version(path: Path) -> str:
    if not path.exists():
        raise ReleaseError(f"{path.as_posix()} is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    if not isinstance(version, str):
        raise ReleaseError(f"{path.as_posix()} has no string version")
    return version


def _set_json_file_version(path: Path, version: str) -> None:
    if not path.exists():
        raise ReleaseError(f"{path.as_posix()} is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    packages = data.get("packages")
    if isinstance(packages, dict) and isinstance(packages.get(""), dict):
        packages[""]["version"] = version
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _replace_file(path: Path, pattern: re.Pattern[str], replacement: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise ReleaseError(f"Could not update {label}")
    path.write_text(new, encoding="utf-8")


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise ReleaseError("git executable was not found on PATH")
    return str(Path(git).resolve())


def repo_root_from_args(path: str | None) -> Path:
    return Path(path).resolve() if path else Path(__file__).resolve().parents[1]


def add_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        default=None,
        help="repository root; defaults to parent of scripts/",
    )
