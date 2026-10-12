"""Harness sanity: every scenario file is valid, fair and leak-free."""

import re
from datetime import datetime, timezone

import pytest
import yaml

from chaos import deploy_sim
from chaos.scenario import STEP_TYPES, build_alert, load_scenario, scenario_files
from responder.schemas import Alert, Hypothesis
from responder.tools.topology import load_topology

FAULT_TYPES = set(Hypothesis.model_fields["fault_type"].annotation.__args__)
SCENARIOS = [p.stem for p in scenario_files()]


def test_there_are_scenarios():
    assert len(SCENARIOS) >= 8


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_is_valid(name):
    scn = load_scenario(name)
    assert scn.id == name
    assert scn.symptom, "every scenario needs a symptom check"
    for step in scn.setup:
        assert len(step) == 1 and next(iter(step)) in STEP_TYPES
        if "deploy" in step:
            assert (deploy_sim.PATCHES_DIR / step["deploy"]["patch"]).exists()
    expected = scn.expected
    assert expected["fault_type"] in FAULT_TYPES
    components = set(load_topology()) | {"none"}
    assert {expected["root_cause_service"], *expected["accepted_services"], *expected["must_not_blame"]} <= components


@pytest.mark.parametrize("name", SCENARIOS)
def test_noise_deploys_never_touch_the_ground_truth(name):
    scn = load_scenario(name)
    noise = scn.noise_services()
    assert 1 <= len(noise) <= 2
    assert not set(noise) & scn.ground_truth_services()
    assert all(s in deploy_sim.NOISE_PATCHES for s in noise)


@pytest.mark.parametrize("name", SCENARIOS)
def test_alert_reveals_only_the_symptom(name):
    scn = load_scenario(name)
    alert = build_alert(scn, datetime(2026, 10, 12, tzinfo=timezone.utc))
    Alert.model_validate(alert)
    text = yaml.safe_dump(alert).lower()
    assert scn.id.split("_")[0] not in text and scn.id not in text
    root = scn.expected["root_cause_service"]
    if root != alert["service"]:  # the alert names where it hurts, never what caused it
        assert not re.search(rf"\b{root}\b", text)


def test_patches_have_subject_and_author():
    for path in deploy_sim.PATCHES_DIR.glob("*.patch"):
        patch = deploy_sim.read_patch(path.name)
        assert patch.subject and "@" in patch.author_email and patch.author_name
