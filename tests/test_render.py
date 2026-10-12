from tri_launch.render import generate_samples, mechanical_issues


def _kinds(text):
    return {i.kind for i in mechanical_issues(text)}


def test_each_mechanical_check_has_a_case():
    assert "double-space" in _kinds("Hello  world")
    assert "space-before-punctuation" in _kinds("Hello , world")
    assert "missing-space-after-punctuation" in _kinds("Hello,world")
    assert "doubled-word" in _kinds("the the deal")
    assert "a-an" in _kinds("a apple")
    assert "a-an" in _kinds("an banana")
    assert "lowercase-sentence-start" in _kinds("Hello. world")
    assert "empty-greeting" in _kinds("Hi ,\nthere")
    assert "unresolved-braces" in _kinds("Hi {{firstName}}")
    assert "stray-pipe" in _kinds("a | b")


def test_clean_text_has_no_issues():
    assert mechanical_issues("Hi Ada,\n\nQuick question for Acme. Worth a chat?") == []


def test_samples_are_deterministic_and_seeded():
    text = "{{RANDOM | Hi | Hey}} {{firstName | there}}"
    leads = [{"firstName": f"L{i}", "email": f"l{i}@x.com"} for i in range(8)]
    a = generate_samples(text, leads, seed=42)
    b = generate_samples(text, leads, seed=42)
    assert [s.text for s in a] == [s.text for s in b]
    assert len(a) == 12
    assert a[-2].label == "shortest options"
    assert a[-1].label == "longest options"
