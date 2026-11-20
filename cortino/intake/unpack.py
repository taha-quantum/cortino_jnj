import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath


class UnsafeArchive(Exception):
    pass


def _declared_total(infos: list[zipfile.ZipInfo]) -> int:
    return sum(i.file_size for i in infos)


def _check_name(name: str) -> None:
    norm = name.replace("\\", "/")
    win = PureWindowsPath(name)
    if (
        norm.startswith("/")
        or win.drive
        or win.root
        or PurePosixPath(norm).is_absolute()
    ):
        raise UnsafeArchive(f"absolute path in archive: {name!r}")
    if ".." in norm.split("/"):
        raise UnsafeArchive(f"path traversal in archive: {name!r}")


def safe_unzip(zip_path: Path, dest: Path, max_bytes: int) -> list[Path]:
    """Extract zip_path into dest, refusing anything that could escape it or blow past max_bytes."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    written: list[Path] = []
    try:
        _extract(zip_path, dest, root, max_bytes, written)
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        RuntimeError,
        NotImplementedError,
    ) as e:
        raise UnsafeArchive(f"corrupt, encrypted or unsupported archive: {e}") from e
    return written


def _extract(
    zip_path: Path, dest: Path, root: Path, max_bytes: int, written: list[Path]
) -> None:
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        for info in infos:
            _check_name(info.filename)
            if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                raise UnsafeArchive(f"symlink in archive: {info.filename!r}")
            target = (root / info.filename.replace("\\", "/")).resolve()
            if target == root or not target.is_relative_to(root):
                raise UnsafeArchive(f"path escapes destination: {info.filename!r}")
        if _declared_total(infos) > max_bytes:
            raise UnsafeArchive("archive unpacks to more than the allowed size")
        total = 0
        for info in infos:
            target = (root / info.filename.replace("\\", "/")).resolve()
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(1 << 20):
                    total += len(chunk)
                    if total > max_bytes:
                        raise UnsafeArchive(
                            "archive unpacks to more than the allowed size"
                        )
                    out.write(chunk)
            written.append(dest / target.relative_to(root))
