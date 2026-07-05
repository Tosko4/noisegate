from __future__ import annotations

import errno
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SIZE_CAP = 1_000_000
ARTIFACT_ID_RE = re.compile(r"ng_[a-f0-9]{24,64}")


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
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"ng_{digest[:24]}"
        path = self._path_for(artifact_id, root=root)

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            existing = self.read(artifact_id)
            if hashlib.sha256(existing.encode("utf-8")).hexdigest() != digest:
                raise ArtifactSecurityError(
                    "artifact id collision with different content"
                ) from None
            return StoredArtifact(artifact_id, digest, len(data))
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ArtifactSecurityError("artifact path resolves through a symlink") from exc
            raise

        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)
        return StoredArtifact(artifact_id, digest, len(data))

    def read(self, artifact_id: str) -> str:
        root = self._ensure_root()
        path = self._path_for(artifact_id, root=root)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ArtifactSecurityError("artifact path resolves through a symlink") from exc
            raise
        with os.fdopen(fd, "rb") as handle:
            return handle.read().decode("utf-8")

    def _ensure_root(self) -> Path:
        if self.root.is_symlink():
            raise ArtifactSecurityError("artifact root must not be a symlink")
        if self.root.exists() and not self.root.is_dir():
            raise ArtifactSecurityError("artifact root must be a directory")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        return self.root.resolve(strict=True)

    def _path_for(self, artifact_id: str, *, root: Path | None = None) -> Path:
        if ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
            raise ArtifactSecurityError("invalid artifact id")
        resolved_root = root if root is not None else self._ensure_root()
        path = resolved_root / f"{artifact_id}.txt"
        if path.parent != resolved_root:
            raise ArtifactSecurityError("artifact path escapes artifact root")
        return path


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default
