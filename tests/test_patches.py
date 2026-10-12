"""Every deploy patch must apply cleanly to the current services/ code.

If services/ changes, a patch can silently stop applying and its scenario
breaks. This test catches that: it copies services/ into a fresh git repo
(like `make target-init`) and runs `git apply --check` for each patch.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from chaos import deploy_sim

ROOT = Path(__file__).resolve().parent.parent
PATCHES = sorted(p.name for p in deploy_sim.PATCHES_DIR.glob("*.patch"))


@pytest.fixture(scope="module")
def baseline_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("target") / "repo"
    shutil.copytree(ROOT / "services", repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


@pytest.mark.parametrize("patch", PATCHES)
def test_patch_applies_to_services(baseline_repo, patch):
    result = subprocess.run(["git", "apply", "--check", str(deploy_sim.PATCHES_DIR / patch)],
                            cwd=baseline_repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_noise_service_has_a_patch():
    for service, patch in deploy_sim.NOISE_PATCHES.items():
        assert patch in PATCHES
        assert f"a/{service}/" in (deploy_sim.PATCHES_DIR / patch).read_text()
