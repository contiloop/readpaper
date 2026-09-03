from pathlib import Path

import readpaper


def test_readpaper_namespace_is_importable() -> None:
    assert "StateService" in readpaper.__all__


def test_runtime_ignore_block_is_exact() -> None:
    root = Path(__file__).resolve().parents[1]
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    start = lines.index("# BEGIN READPAPER MANAGED")
    end = lines.index("# END READPAPER MANAGED")

    assert lines[start + 1 : end] == ["/papers/", "/.readpaper/"]
    assert lines.count("/papers/") == 1
    assert lines.count("/.readpaper/") == 1
