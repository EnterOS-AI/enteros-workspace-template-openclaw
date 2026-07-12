#!/usr/bin/env python3
"""Extract the private runtime requirement and filter it from a pip input."""

import re
import sys
from pathlib import Path

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import InvalidVersion, Version


RUNTIME_DISTRIBUTION = "molecules-workspace-runtime"
RUNTIME_CANONICAL_NAME = canonicalize_name(RUNTIME_DISTRIBUTION)
LEADING_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


class RuntimeRequirementError(ValueError):
    """Raised when the private runtime requirement is unsafe or ambiguous."""


def _requirement_text(line: str) -> str:
    """Remove a conventional inline comment without stripping URL fragments."""
    return re.split(r"\s+#", line.strip(), maxsplit=1)[0].strip()


def _parse_runtime_candidate(line: str) -> Requirement | None:
    text = _requirement_text(line)
    if not text or text.startswith("#"):
        return None

    leading_name = LEADING_REQUIREMENT_NAME.match(text)
    try:
        requirement = Requirement(text)
    except InvalidRequirement as exc:
        if (
            leading_name
            and canonicalize_name(leading_name.group(1)) == RUNTIME_CANONICAL_NAME
        ):
            raise RuntimeRequirementError(
                "runtime requirement is not valid PEP 508 syntax"
            ) from exc
        return None

    if canonicalize_name(requirement.name) != RUNTIME_CANONICAL_NAME:
        return None
    if requirement.url or requirement.extras or requirement.marker:
        raise RuntimeRequirementError(
            "runtime requirement must not use a URL, extras, or environment marker"
        )
    return requirement


def prepare_requirements(
    source: Path, destination: Path, runtime_version: str = ""
) -> str:
    """Return the safe runtime requirement and write public-only requirements."""
    retained_lines: list[str] = []
    runtime_requirements: list[Requirement] = []

    for line in source.read_text(encoding="utf-8").splitlines(keepends=True):
        requirement = _parse_runtime_candidate(line)
        if requirement is None:
            retained_lines.append(line)
        else:
            runtime_requirements.append(requirement)

    if len(runtime_requirements) != 1:
        raise RuntimeRequirementError(
            "requirements must contain exactly one molecules-workspace-runtime entry"
        )

    if runtime_version:
        try:
            version = Version(runtime_version)
        except InvalidVersion as exc:
            raise RuntimeRequirementError("RUNTIME_VERSION is not a valid version") from exc
        runtime_requirement = f"{RUNTIME_DISTRIBUTION}=={version}"
    else:
        runtime_requirement = (
            f"{RUNTIME_DISTRIBUTION}{runtime_requirements[0].specifier}"
        )

    destination.write_text("".join(retained_lines), encoding="utf-8")
    return runtime_requirement


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: prepare-runtime-requirements.py SOURCE DESTINATION RUNTIME_VERSION",
            file=sys.stderr,
        )
        return 2

    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    destination.unlink(missing_ok=True)
    try:
        runtime_requirement = prepare_requirements(source, destination, sys.argv[3])
    except (OSError, RuntimeRequirementError) as exc:
        destination.unlink(missing_ok=True)
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(runtime_requirement)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
