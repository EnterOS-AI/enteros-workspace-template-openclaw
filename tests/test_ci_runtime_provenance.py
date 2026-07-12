"""Static supply-chain contracts for OpenClaw CI workflows."""
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".gitea" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW = REPO_ROOT / ".gitea" / "workflows" / "publish-image.yml"
SDK_COMMIT = "c58c6697b2455540c51" + "5c9a3c6656a34ab286e66"
RUNTIME_INSTALLER = (
    ".molecule-ci-canonical/scripts/install_workspace_dependencies.py "
    "--allow-missing --break-system-packages"
)
RETIRED_RUNTIME_PROJECT = "molecule" + "-ai-workspace-runtime"


def load_ci_workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text())


def test_ci_acquires_runtime_only_through_canonical_installer() -> None:
    workflow = CI_WORKFLOW.read_text()

    assert "--extra-index-url" not in workflow
    assert workflow.count(RUNTIME_INSTALLER) == 2
    assert workflow.count("git clone --depth 1 ") == 3
    assert workflow.count("molecule-ci.git .molecule-ci-canonical") == 3

    jobs = load_ci_workflow()["jobs"]
    for job_name in ("validate-runtime", "tests"):
        commands = [step.get("run", "") for step in jobs[job_name]["steps"]]
        installer_index = next(
            index
            for index, command in enumerate(commands)
            if RUNTIME_INSTALLER in command
        )
        prerequisites = "\n".join(commands[:installer_index])
        assert "molecule-ci.git .molecule-ci-canonical" in prerequisites
        assert "packaging" in prerequisites


def test_ci_installs_sdk_from_immutable_gitea_commit() -> None:
    workflow = CI_WORKFLOW.read_text()
    sdk_source = (
        "git+https://git.moleculesai.app/molecule-ai/"
        f"molecule-ai-sdk.git@{SDK_COMMIT}"
    )

    assert sdk_source in workflow
    assert "molecules-workspace-runtime molecule-ai-sdk" not in workflow


def test_ci_keeps_untrusted_forks_outside_runtime_execution_boundary() -> None:
    jobs = load_ci_workflow()["jobs"]

    for job_name in ("validate-runtime", "t4-conformance", "tests"):
        for step in jobs[job_name]["steps"]:
            if "uses" in step:
                continue
            if step["name"].startswith("Skip "):
                assert step["if"] == "github.event.pull_request.head.repo.fork == true"
            else:
                assert step["if"] == "github.event.pull_request.head.repo.fork != true"


def test_publish_lint_uses_private_only_canonical_runtime_download() -> None:
    workflow = PUBLISH_WORKFLOW.read_text()

    assert "molecules-workspace-runtime" in workflow
    assert RETIRED_RUNTIME_PROJECT not in workflow
    assert "pip download --quiet --isolated" in workflow
    assert "--only-binary=:all:" in workflow
    assert "--no-deps" in workflow
    assert '--index-url "$MOLECULE_PRIVATE_INDEX"' in workflow
    assert "--extra-index-url" not in workflow


def test_retired_runtime_project_is_absent_from_active_ci_and_guidance() -> None:
    paths = (
        CI_WORKFLOW,
        PUBLISH_WORKFLOW,
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "runbooks" / "local-dev-setup.md",
        REPO_ROOT / "install.sh",
        REPO_ROOT / "adapter.py",
    )

    for path in paths:
        assert RETIRED_RUNTIME_PROJECT not in path.read_text(), path


def test_obsolete_vendored_validator_is_deleted() -> None:
    assert not (REPO_ROOT / ".molecule-ci").exists()
