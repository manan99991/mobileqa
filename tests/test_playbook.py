"""Guards the learn-and-grow mechanism: the defect playbook must stay well-formed and keep flowing
into both brains. If someone adds a malformed pattern or breaks the prompt wiring, this fails."""
from mobileqa.brain import playbook, prompts

VALID_CLASSES = {"crash", "functional", "value", "ui", "ux", "a11y", "content", "perf", "security"}
VALID_SEV = {"critical", "high", "medium", "low"}


def test_patterns_are_well_formed():
    assert playbook.PATTERNS, "the playbook must not be empty — it is how the tester compounds"
    ids = set()
    for p in playbook.PATTERNS:
        assert p.id and p.id not in ids, f"duplicate/blank pattern id: {p.id}"
        ids.add(p.id)
        assert p.defect_class in VALID_CLASSES, f"{p.id}: bad class {p.defect_class}"
        assert p.severity in VALID_SEV, f"{p.id}: bad severity {p.severity}"
        assert p.probe and p.tell, f"{p.id}: needs both a probe and a tell"
        assert p.learned_from and p.since, f"{p.id}: record where/when it was learned"


def test_the_three_learned_classes_are_present():
    ids = {p.id for p in playbook.PATTERNS}
    for required in ("dead-control", "blank-output", "result-no-preview"):
        assert required in ids, f"missing learned pattern: {required}"


def test_blocks_render_and_inject_into_both_prompts():
    ab, vb = playbook.action_block(), playbook.vision_block()
    assert "LEARNED DEFECT PATTERNS" in ab
    assert vb  # at least one visual pattern
    # the prompts must still format with the injected blocks (and no stray single-brace fields)
    s = prompts.SYSTEM.format(platform="ios", playbook=ab)
    v = prompts.VISION_SYSTEM.format(platform="ios", screen="X", vision_playbook=vb)
    assert ab in s and vb in v
