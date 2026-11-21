import os

from cortino.config import load_env_file


def test_env_file_loaded_without_overriding_shell(tmp_path, monkeypatch):
    for k in ("CT_A", "CT_B", "CT_C", "CT_D", "CT_E", "CT_SHELL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CT_SHELL", "from-shell")
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\nCT_A=plain\nexport CT_B = \"quoted # kept\"\nCT_C='single'\n"
        "CT_D=global  # trailing comment\nCT_E=\nCT_SHELL=from-file\nnot a line\n",
        encoding="utf-8",
    )
    loaded = load_env_file(f)
    assert sorted(loaded) == ["CT_A", "CT_B", "CT_C", "CT_D"]
    assert os.environ["CT_A"] == "plain"
    assert os.environ["CT_B"] == "quoted # kept"
    assert os.environ["CT_C"] == "single"
    assert os.environ["CT_D"] == "global"
    assert "CT_E" not in os.environ  # empty values are skipped
    assert os.environ["CT_SHELL"] == "from-shell"


def test_missing_env_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []
