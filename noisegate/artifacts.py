from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_SIZE_CAP = 1_000_000
ARTIFACT_ID_RE = re.compile(r"ng_[a-f0-9]{24,64}")
TEMP_ARTIFACT_RE = re.compile(r"\.ng_[a-f0-9]{24,64}\.[^.]+\.tmp")
TEMP_ARTIFACT_STALE_SECONDS = 60 * 60



class ArtifactError(RuntimeError):
    pass


class ArtifactSecurityError(ArtifactError):
    pass


class ArtifactTooLarge(ArtifactError):
    def __init__(self, size_bytes: int, size_cap: int) -> None:
        self.size_bytes = size_bytes
        self.size_cap = size_cap
        super().__init__(f"artifact is {size_bytes} bytes; cap is {size_cap} bytes")


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    sha256: str
    size_bytes: int

    def to_metadata(self) -> dict[str, object]:
        return {
            "id": self.artifact_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ArtifactInfo:
    artifact_id: str
    sha256: str
    size_bytes: int
    modified_at: str


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    artifact_id: str
    ok: bool
    reason: str
    path: str


def default_artifact_dir() -> Path:
    env_dir = os.environ.get("NOISEGATE_ARTIFACT_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".local" / "share" / "noisegate" / "artifacts"


class ArtifactStore:
    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        size_cap: int = DEFAULT_SIZE_CAP,
    ) -> None:
        self.root = Path(root).expanduser() if root is not None else default_artifact_dir()
        self.size_cap = size_cap

    @classmethod
    def from_env(cls) -> ArtifactStore:
        cap = _parse_int(os.environ.get("NOISEGATE_ARTIFACT_SIZE_CAP"), DEFAULT_SIZE_CAP)
        return cls(default_artifact_dir(), size_cap=cap)

    def store(self, text: str) -> StoredArtifact:
        data = text.encode("utf-8")
        if len(data) > self.size_cap:
            raise ArtifactTooLarge(len(data), self.size_cap)

        root = self._ensure_root()
        self._cleanup_stale_temp_files(root)
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"ng_{digest[:24]}"
        path = self._path_for(artifact_id, root=root)

        try:
            stat_result = path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ArtifactSecurityError(type(exc).__name__) from exc
        else:
            if not stat.S_ISREG(stat_result.st_mode):
                raise ArtifactSecurityError("artifact path must be a regular file")
            existing = self._read_bytes(artifact_id, root=root)
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ArtifactSecurityError(
                    "artifact id collision with different content"
                ) from None
            return StoredArtifact(artifact_id, digest, len(data))

        temp_path: Path | None = None
        try:
            fd, raw_temp_path = tempfile.mkstemp(
                prefix=f".{artifact_id}.",
                suffix=".tmp",
                dir=root,
            )
            temp_path = Path(raw_temp_path)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(temp_path, 0o600)
            os.link(temp_path, path)
        except FileExistsError:
            existing = self.read(artifact_id)
            if hashlib.sha256(existing.encode("utf-8")).hexdigest() != digest:
                raise ArtifactSecurityError(
                    "artifact id collision with different content"
                ) from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ArtifactSecurityError("artifact path resolves through a symlink") from exc
            raise
        finally:
            if temp_path is not None:
                with suppress(FileNotFoundError):
                    temp_path.unlink()
        return StoredArtifact(artifact_id, digest, len(data))

    def read(self, artifact_id: str) -> str:
        return self._read_bytes(artifact_id).decode("utf-8")

    def list(self) -> list[ArtifactInfo]:
        root = self._ensure_root()
        artifacts: list[ArtifactInfo] = []
        for path in sorted(root.glob("ng_*.txt")):
            artifact_id = path.stem
            if ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
                continue
            try:
                data = self._read_bytes(artifact_id, root=root)
            except ArtifactError:
                continue
            stat_result = path.stat()
            artifacts.append(
                ArtifactInfo(
                    artifact_id=artifact_id,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    modified_at=datetime.fromtimestamp(
                        stat_result.st_mtime,
                        tz=UTC,
                    ).isoformat(),
                )
            )
        return artifacts

    def stats(self) -> dict[str, int]:
        artifacts = self.list()
        return {
            "artifacts": len(artifacts),
            "total_size_bytes": sum(artifact.size_bytes for artifact in artifacts),
        }

    def verify(self) -> list[ArtifactCheck]:
        root = self._ensure_root()
        checks: list[ArtifactCheck] = []
        for path in sorted(root.iterdir()):
            if path.name.startswith("."):
                if TEMP_ARTIFACT_RE.fullmatch(path.name):
                    checks.append(self._check_temp_artifact(path))
                continue
            if path.suffix != ".txt" or not path.name.startswith("ng_"):
                continue
            artifact_id = path.stem
            if ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
                checks.append(
                    ArtifactCheck(
                        artifact_id=artifact_id,
                        ok=False,
                        reason="invalid_id",
                        path=str(path),
                    )
                )
                continue
            if path.is_symlink():
                checks.append(
                    ArtifactCheck(
                        artifact_id=artifact_id,
                        ok=False,
                        reason="symlink",
                        path=str(path),
                    )
                )
                continue
            try:
                stat_result = path.lstat()
            except OSError as exc:
                checks.append(
                    ArtifactCheck(
                        artifact_id=artifact_id,
                        ok=False,
                        reason=type(exc).__name__,
                        path=str(path),
                    )
                )
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                checks.append(
                    ArtifactCheck(
                        artifact_id=artifact_id,
                        ok=False,
                        reason="non_regular",
                        path=str(path),
                    )
                )
                continue
            if stat_result.st_size > self.size_cap:
                checks.append(
                    ArtifactCheck(
                        artifact_id=artifact_id,
                        ok=False,
                        reason="too_large",
                        path=str(path),
                    )
                )
                continue
            try:
                data = self._read_bytes(artifact_id, root=root)
            except ArtifactError as exc:
                checks.append(
                    ArtifactCheck(
                        artifact_id=artifact_id,
                        ok=False,
                        reason=type(exc).__name__,
                        path=str(path),
                    )
                )
                continue
            digest = hashlib.sha256(data).hexdigest()
            expected_prefix = artifact_id.removeprefix("ng_")
            if not digest.startswith(expected_prefix):
                checks.append(
                    ArtifactCheck(
                        artifact_id=artifact_id,
                        ok=False,
                        reason="sha_mismatch",
                        path=str(path),
                    )
                )
                continue
            checks.append(
                ArtifactCheck(
                    artifact_id=artifact_id,
                    ok=True,
                    reason="ok",
                    path=str(path),
                )
            )
        return checks

    def _check_temp_artifact(self, path: Path) -> ArtifactCheck:
        try:
            stat_result = path.lstat()
        except OSError as exc:
            return ArtifactCheck(path.name, False, type(exc).__name__, str(path))
        if path.is_symlink():
            return ArtifactCheck(path.name, False, "temp_symlink", str(path))
        if not stat.S_ISREG(stat_result.st_mode):
            return ArtifactCheck(path.name, False, "temp_non_regular", str(path))
        if stat_result.st_size > self.size_cap:
            return ArtifactCheck(path.name, False, "temp_too_large", str(path))
        if self._is_stale_temp(stat_result):
            with suppress(FileNotFoundError):
                path.unlink()
            return ArtifactCheck(path.name, True, "stale_temp_removed", str(path))
        return ArtifactCheck(path.name, False, "temp_file", str(path))

    def _cleanup_stale_temp_files(self, root: Path) -> None:
        for path in root.iterdir():
            if not TEMP_ARTIFACT_RE.fullmatch(path.name):
                continue
            try:
                stat_result = path.lstat()
            except OSError:
                continue
            if path.is_symlink() or not stat.S_ISREG(stat_result.st_mode):
                continue
            if self._is_stale_temp(stat_result):
                with suppress(FileNotFoundError):
                    path.unlink()

    def _is_stale_temp(self, stat_result: os.stat_result) -> bool:
        now = datetime.now(UTC).timestamp()
        return now - stat_result.st_mtime >= TEMP_ARTIFACT_STALE_SECONDS


    def _ensure_root(self) -> Path:
        if self.root.is_symlink():
            raise ArtifactSecurityError("artifact root must not be a symlink")
        existed = self.root.exists()
        if existed and not self.root.is_dir():
            raise ArtifactSecurityError("artifact root must be a directory")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if existed:
            mode = stat.S_IMODE(self.root.stat().st_mode)
            if mode & 0o077:
                raise ArtifactSecurityError("artifact root permissions must be owner-only")
        else:
            # Private raw-output artifact directory: owner-only access is intentional.
            os.chmod(self.root, 0o700)  # nosemgrep
        return self.root.resolve(strict=True)

    def _path_for(self, artifact_id: str, *, root: Path | None = None) -> Path:
        if ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
            raise ArtifactSecurityError("invalid artifact id")
        resolved_root = root if root is not None else self._ensure_root()
        path = resolved_root / f"{artifact_id}.txt"
        if path.parent != resolved_root:
            raise ArtifactSecurityError("artifact path escapes artifact root")
        return path

    def _read_bytes(self, artifact_id: str, *, root: Path | None = None) -> bytes:
        resolved_root = root if root is not None else self._ensure_root()
        path = self._path_for(artifact_id, root=resolved_root)
        try:
            stat_result = path.lstat()
        except OSError as exc:
            raise ArtifactSecurityError(type(exc).__name__) from exc
        if not stat.S_ISREG(stat_result.st_mode):
            raise ArtifactSecurityError("artifact path must be a regular file")
        if stat_result.st_size > self.size_cap:
            raise ArtifactTooLarge(stat_result.st_size, self.size_cap)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ArtifactSecurityError("artifact path resolves through a symlink") from exc
            raise ArtifactSecurityError(type(exc).__name__) from exc
        with os.fdopen(fd, "rb") as handle:
            return handle.read()


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default
