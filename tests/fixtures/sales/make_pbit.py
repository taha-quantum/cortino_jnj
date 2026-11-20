"""Build pbit/Sales.pbit deterministically from the text sources in pbit/src/.

A .pbit is a zip; Power BI writes DataModelSchema and Report/Layout as UTF-16 LE with a BOM.
Run: uv run python tests/fixtures/sales/make_pbit.py
"""

import zipfile
from pathlib import Path

HERE = Path(__file__).parent / "pbit"
PARTS = {"DataModelSchema": "DataModelSchema.json", "Report/Layout": "Layout.json"}


def build(dest: Path = HERE / "Sales.pbit") -> Path:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, src in PARTS.items():
            text = (HERE / "src" / src).read_text(encoding="utf-8")
            info = zipfile.ZipInfo(arcname, date_time=(2025, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0  # same on every OS
            info.external_attr = 0o600 << 16
            z.writestr(info, b"\xff\xfe" + text.encode("utf-16-le"))
    return dest


if __name__ == "__main__":
    print(build())
