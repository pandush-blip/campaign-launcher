import random

from tri_launch.spintax import (
    analyze,
    count_combinations,
    dedupe_spin_options,
    duplicate_option_blocks,
    render,
    render_extreme,
    variables_in_text,
    variables_without_fallback,
)


def test_counts_and_renders():
    text = "{{RANDOM | a | b}}"
    assert count_combinations(text) == 2
    out = render(text, rng=random.Random(1))
    assert out in {"a", "b"}


def test_fallback_resolution():
    assert render("{{firstName | there}}", {}) == "there"
    assert render("{{firstName | there}}", {"firstName": "Ada"}) == "Ada"
    assert render("{{firstName | there}}", {"firstName": "  "}) == "there"


def test_missing_variable_without_fallback():
    missing = []
    assert render("Hi {{firstName}}", {}, missing=missing) == "Hi "
    assert missing == ["firstName"]


def test_products_across_blocks():
    assert count_combinations("{{RANDOM | a | b}} {{RANDOM | c | d | e}}") == 6


def test_nested_variables_and_options():
    text = "{{RANDOM | Hi {{firstName | there}} | Hey {{lastName | Friend}}}}"
    assert sorted(variables_in_text(text)) == ["firstName", "lastName"]
    out = render(text, {"firstName": "Ada", "lastName": "Lovelace"}, rng=random.Random(0))
    assert out in {"Hi Ada", "Hey Lovelace"}


def test_variables_without_fallback_only():
    text = "{{firstName}} {{lastName | Friend}} {{RANDOM | x {{companyName}} | y}}"
    assert variables_without_fallback(text) == ["firstName", "companyName"]


def test_extreme_renders():
    text = "{{RANDOM | looooooooong | x}}"
    assert render_extreme(text, longest=True) == "looooooooong"
    assert render_extreme(text, longest=False) == "x"


def test_malformed_detection():
    assert any(i.level == "error" for i in analyze("{{RANDOM | a"))
    assert any("single" in i.message for i in analyze("Hi {there}"))
    assert any("stray" in i.message for i in analyze("a | b"))
    assert any("empty spin option" in i.message for i in analyze("{{RANDOM | | b}}"))
    assert any("lowercase" in i.message for i in analyze("{{random | a | b}}"))


def test_balanced_ok():
    assert analyze("{{RANDOM | a {{x | y}} | b}}") == []


def test_duplicate_options_detected_and_removed():
    text = "{{RANDOM | We book meetings. | We book meetings. | We fill calendars.}}"
    assert duplicate_option_blocks(text) == 1
    fixed, removed = dedupe_spin_options(text)
    assert removed == 1
    assert duplicate_option_blocks(fixed) == 0
    assert count_combinations(fixed) == 2
    assert "We book meetings." in fixed and "We fill calendars." in fixed


def test_dedupe_handles_nested_variables():
    text = "{{RANDOM | Hi {{firstName | there}} | Hi {{firstName | there}} | Hey {{firstName | there}}}}"
    fixed, removed = dedupe_spin_options(text)
    assert removed == 1
    assert fixed.count("Hi {{firstName | there}}") == 1
    assert "Hey {{firstName | there}}" in fixed


def test_single_option_block_is_unwrapped():
    fixed, removed = dedupe_spin_options("{{RANDOM | Same line. | Same line. | Same line.}}")
    assert removed == 2
    assert fixed == "Same line."
    assert analyze(fixed) == []


def test_dedupe_leaves_real_blocks_and_text_alone():
    text = "Hi {{firstName | there}},\n\n{{RANDOM | One option. | Another option.}}"
    fixed, removed = dedupe_spin_options(text)
    assert removed == 0
    assert fixed == text
