from pathlib import Path
import tomllib

from packaging.specifiers import SpecifierSet


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_replit_supported_python_range_excludes_unresolvable_313() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    supported = SpecifierSet(project["project"]["requires-python"])

    assert supported.contains("3.11")
    assert supported.contains("3.12")
    assert not supported.contains("3.13")
