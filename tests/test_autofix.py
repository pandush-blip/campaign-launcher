from tri_launch.autofix import copy_to_markdown, fix_body, fix_copy, fix_subject


def test_adds_safe_variable_fallbacks():
    fixed, fixes = fix_body("Hi {{firstName}},\n\nQuick one about {{companyName}}.")
    assert "{{firstName | there}}" in fixed
    assert "{{companyName | your company}}" in fixed
    assert any("firstName" in str(f) for f in fixes)


def test_leaves_existing_fallbacks_and_unknowns_alone():
    fixed, fixes = fix_body("Hi {{firstName | friend}}, {{List Name}}")
    assert "{{firstName | friend}}" in fixed
    assert "{{List Name}}" in fixed
    assert not [f for f in fixes if "fallback" in str(f)]


def test_lowercase_random_is_not_touched_by_body_fixer():
    # QA flags lowercase random; the fixer's job is variables/dashes/spacing/greetings.
    fixed, _ = fix_body("{{random | a | b}}")
    assert fixed == "{{random | a | b}}"


def test_dashes_and_spacing_are_repaired():
    fixed, fixes = fix_body("Hello  there \u2014  this,that  works .")
    assert "\u2014" not in fixed
    assert "  " not in fixed
    assert "this, that" in fixed
    assert "works." in fixed
    kinds = " ".join(str(f) for f in fixes)
    assert "dash" in kinds and "spacing" in kinds


def test_greeting_gets_variation():
    fixed, fixes = fix_body("Hi {{firstName | there}},\n\nBody.")
    assert fixed.startswith("{{RANDOM | Hi | Hey | Hello}} {{firstName | there}},")
    assert any("greeting" in str(f) for f in fixes)


def test_no_greeting_variation_when_already_spun():
    fixed, fixes = fix_body("{{RANDOM | Hi | Hey}} {{firstName | there}},\n\nBody.")
    assert fixed.startswith("{{RANDOM | Hi | Hey}}")
    assert not [f for f in fixes if "greeting" in str(f)]


def test_emails_and_urls_are_not_broken():
    fixed, _ = fix_body("Write to john.doe@example.com or https://example.com/a.b")
    assert "john.doe@example.com" in fixed
    assert "https://example.com/a.b" in fixed


def test_subject_never_gets_greeting_spin():
    fixed, _ = fix_subject("Hi {{firstName | there}} - quick question")
    assert fixed == "Hi {{firstName | there}} - quick question"


def test_subject_gets_variable_fallbacks():
    # The Contoso block: the copy uses {{firstName}} in the subject; without a fallback
    # QA coverage blocks the campaign when any lead has no first name.
    fixed, fixes = fix_subject("{{RANDOM | RE: | Re:}} {{firstName}}, quick question")
    assert "{{firstName | there}}" in fixed
    assert any("firstName" in str(f) for f in fixes)


def test_fix_copy_only_subjects_step_one():
    copy, fixes = fix_copy(
        subject="quick question",
        steps=[
            {"delay_days": 0, "variants": ["Hi {{firstName}},\n\nFirst."]},
            {"delay_days": 3, "variants": ["{{firstName}}, bumping this."]},
        ],
    )
    assert copy["subject"] == "quick question"
    assert copy["steps"][0]["variants"][0].startswith("{{RANDOM")
    # follow-ups do not get greeting spin
    assert not copy["steps"][1]["variants"][0].startswith("{{RANDOM")
    markdown = copy_to_markdown(copy)
    assert markdown.count("Subject: quick question") == 1
    # a follow-up step carries an empty subject (same thread)
    assert markdown.count("Subject: \n") == 1


def test_copy_to_markdown_multiple_variants():
    markdown = copy_to_markdown(
        {
            "subject": "hello",
            "steps": [
                {"delay_days": 0, "variants": ["one", "two"]},
                {"delay_days": 4, "variants": ["three"]},
            ],
        }
    )
    assert "## Variant A" in markdown and "## Variant B" in markdown
    assert "# Step 2" in markdown and "delay: 4" in markdown
