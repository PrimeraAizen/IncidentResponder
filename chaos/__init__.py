"""Fault-injection harness (NOT part of the agent system).

Reproduces incident scenarios with known ground truth so the agents can be
evaluated objectively. The agents never import this package and have no tool
that can read its state (anti-leakage rule, SPEC 6.4).

    python -m chaos list | apply s01 | scenario s01 | reset
"""
