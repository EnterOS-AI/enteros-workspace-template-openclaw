"""Security contract for acquiring the private workspace runtime wheel."""

import subprocess
import sys
from pathlib import Path

import pytest


DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"
PREPARE_REQUIREMENTS = (
    Path(__file__).parents[1] / "scripts" / "prepare-runtime-requirements.py"
)
PRIVATE_RUNTIME_INDEX = (
    "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
)


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _prepare_requirements(
    tmp_path: Path, requirements: str, runtime_version: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    source = tmp_path / "requirements.txt"
    filtered = tmp_path / "requirements-public.txt"
    source.write_text(requirements, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REQUIREMENTS),
            str(source),
            str(filtered),
            runtime_version,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result, filtered


@pytest.mark.parametrize(
    "runtime_requirement",
    [
        "molecules-workspace-runtime @ https://attacker.example/runtime.whl",
        "molecules-workspace-runtime[unsafe]>=0.3.11",
        'molecules-workspace-runtime>=0.3.11; python_version >= "3.11"',
    ],
)
def test_unsafe_runtime_requirement_forms_are_rejected(
    tmp_path: Path, runtime_requirement: str
) -> None:
    result, filtered = _prepare_requirements(
        tmp_path,
        f"{runtime_requirement}\npython-multipart>=0.0.27\n",
    )

    assert result.returncode != 0
    assert "runtime requirement must not use" in result.stderr
    assert not filtered.exists()


def test_runtime_override_replaces_exact_requirement_and_filters_it(
    tmp_path: Path,
) -> None:
    result, filtered = _prepare_requirements(
        tmp_path,
        "molecules-workspace-runtime==0.3.124\npython-multipart>=0.0.27\n",
        runtime_version="0.3.125",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "molecules-workspace-runtime==0.3.125"
    assert filtered.read_text(encoding="utf-8") == "python-multipart>=0.0.27\n"


def test_runtime_requirement_is_canonicalized_and_filtered(tmp_path: Path) -> None:
    result, filtered = _prepare_requirements(
        tmp_path,
        "molecules_workspace_runtime>=0.3.11\npython-multipart>=0.0.27\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "molecules-workspace-runtime>=0.3.11"
    assert filtered.read_text(encoding="utf-8") == "python-multipart>=0.0.27\n"


@pytest.mark.parametrize(
    "requirements",
    [
        "python-multipart>=0.0.27\n",
        (
            "molecules-workspace-runtime>=0.3.11\n"
            "molecules-workspace-runtime<1\n"
        ),
    ],
)
def test_exactly_one_runtime_requirement_is_required(
    tmp_path: Path, requirements: str
) -> None:
    result, filtered = _prepare_requirements(tmp_path, requirements)

    assert result.returncode != 0
    assert "exactly one molecules-workspace-runtime entry" in result.stderr
    assert not filtered.exists()


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

    assert "prepare-runtime-requirements.py" in dockerfile
    assert 'test "${runtime_wheel_count}" -eq 1' in dockerfile


def test_local_runtime_wheel_and_requirements_share_one_isolated_solve() -> None:
    dockerfile = _dockerfile()

    install_command = "pip install --isolated" + dockerfile.split(
        "pip install --isolated", 1
    )[1].split(";", 1)[0]
    assert '"${runtime_wheel}"' in install_command
    assert "-r /tmp/molecule-runtime/requirements-public.txt" in install_command
    assert dockerfile.count("pip install --isolated") == 1
