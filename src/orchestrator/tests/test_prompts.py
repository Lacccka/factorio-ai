from factorio_ai.prompts import SYSTEM_PROMPT


def test_system_prompt_prioritizes_existing_upstream_before_new_extraction() -> None:
    assert "Use this source hierarchy for every required material" in SYSTEM_PROMPT
    assert "reuse an existing automated upstream producer/smelter/mining area" in SYSTEM_PROMPT
    assert "build a new raw-resource extraction or smelting subsystem only" in SYSTEM_PROMPT
    assert "Lack of an item on the current bus does not imply lack of existing production" in SYSTEM_PROMPT
    assert "source_mode=tap or source_mode=extend" in SYSTEM_PROMPT
