"""Build Sales.twbx (a zip holding Sales.twb) deterministically from twb/Sales.twb."""

import zipfile
from pathlib import Path

HERE = Path(__file__).parent
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def build(dest: Path) -> Path:
    info = zipfile.ZipInfo("Sales.twb", FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr(info, (HERE / "twb" / "Sales.twb").read_bytes())
    return dest


if __name__ == "__main__":
    print(build(HERE / "twb" / "Sales.twbx"))
