from tri_launch.variables import DEFAULT_BUILTINS, coverage, extract_variables, unknown_variables


def test_extract_inside_blocks():
    text = "{{RANDOM | Hi {{firstName}} | Hey {{companyName | your company}}}}"
    assert sorted(extract_variables(text)) == ["companyName", "firstName"]


def test_unknown_variables():
    text = "{{firstName}} {{software type}} {{CompanyName}}"
    allowed = set(DEFAULT_BUILTINS) | {"software type"}
    assert unknown_variables(text, allowed) == ["CompanyName"]  # case-sensitive


def test_coverage_math_and_status():
    text = "{{firstName}} {{companyName | friend}}"
    leads = [{"firstName": "Ada"}] * 49 + [{"firstName": ""}]
    cov = coverage(text, leads)
    assert len(cov) == 1  # companyName has a fallback -> skipped
    c = cov[0]
    assert (c.variable, c.empty, c.total, c.status) == ("firstName", 1, 50, "warn")
    assert c.pct == 2.0


def test_coverage_blocks_above_threshold():
    leads = [{"firstName": ""}] * 3 + [{"firstName": "x"}] * 97
    c = coverage("{{firstName}}", leads)[0]
    assert c.status == "block"
    assert c.pct == 3.0


def test_coverage_ok_when_filled():
    c = coverage("{{firstName}}", [{"firstName": "x"}] * 5)[0]
    assert c.status == "ok"


def test_sender_variables_are_not_covered_from_leads():
    text = "{{sendingAccountFirstName}} {{accountSignature}}"
    assert coverage(text, [{}] * 10) == []
