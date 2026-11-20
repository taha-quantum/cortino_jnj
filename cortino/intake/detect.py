import zipfile
from pathlib import Path
from typing import Literal

SourceType = Literal["pbip_zip", "pbit", "pbix", "twb", "twbx", "unknown"]


def detect(path: Path) -> SourceType:
    """Classify an upload from its content; the extension is ignored."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                names = [n.replace("\\", "/") for n in z.namelist()]
        except zipfile.BadZipFile:
            return "unknown"
        if any(p.endswith(".SemanticModel") for n in names for p in n.split("/")[:-1]):
            return "pbip_zip"
        base = {n.rsplit("/", 1)[-1] for n in names}
        if "DataModelSchema" in base:
            return "pbit"
        if "DataModel" in base:
            return "pbix"
        if any(n.lower().endswith(".twb") for n in names):
            return "twbx"
        return "unknown"
    text = head.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n")
    if text.startswith(("<?xml", "<workbook")) and "<workbook" in text:
        return "twb"
    return "unknown"
