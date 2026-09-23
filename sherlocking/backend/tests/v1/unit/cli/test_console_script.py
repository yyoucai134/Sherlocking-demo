import tomllib


def test_pyproject_exposes_sherlocking_console_script() -> None:
    with open("pyproject.toml", "rb") as file:
        pyproject = tomllib.load(file)

    assert pyproject["project"]["scripts"]["sherlocking"] == "sherlocking.backend.app.cli.main:main"
