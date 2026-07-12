"""Security contract for acquiring the private workspace runtime wheel."""

from pathlib import Path


DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"
PRIVATE_RUNTIME_INDEX = (
    "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
)


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_runtime_wheel_is_downloaded_only_from_the_private_index() -> None:
    dockerfile = _dockerfile()

    assert f"ARG MOLECULE_RUNTIME_INDEX={PRIVATE_RUNTIME_INDEX}" in dockerfile
    assert "pip download --isolated --only-binary=:all: --no-deps" in dockerfile

    download_command = dockerfile.split("pip download", 1)[1].split(";", 1)[0]
    assert '--index-url "$MOLECULE_RUNTIME_INDEX"' in download_command
    assert "--extra-index-url" not in download_command
    assert '"${runtime_requirement}"' in download_command
    assert "--extra-index-url" not in dockerfile


def test_runtime_version_pin_and_single_wheel_are_enforced() -> None:
    dockerfile = _dockerfile()

    assert "awk '/^[[:space:]]*molecules-workspace-runtime/" in dockerfile
    assert 'runtime_requirement="molecules-workspace-runtime==${RUNTIME_VERSION}"' in dockerfile
    assert 'test "${runtime_wheel_count}" -eq 1' in dockerfile


def test_local_runtime_wheel_and_requirements_share_one_isolated_solve() -> None:
    dockerfile = _dockerfile()

    install_command = "pip install --isolated" + dockerfile.split(
        "pip install --isolated", 1
    )[1].split(";", 1)[0]
    assert '"${runtime_wheel}"' in install_command
    assert "-r requirements.txt" in install_command
    assert dockerfile.count("pip install --isolated") == 1
