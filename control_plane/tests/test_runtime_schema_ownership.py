from pathlib import Path


def test_runtime_startup_does_not_create_schema() -> None:
    main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    text = main_path.read_text(encoding="utf-8")

    assert "Base.metadata.create_all" not in text
    assert ".metadata.create_all(" not in text
    assert "from .db import Base" not in text
