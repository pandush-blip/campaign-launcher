from tri_launch.config import LintConfig
from tri_launch.lint import lint_text


def kinds(text, cfg):
    return {i.kind for i in lint_text(text, cfg)}


def test_em_dash_and_exclamation_flags():
    cfg = LintConfig(ban_em_dash=True, ban_exclamation=True)
    assert "em-dash" in kinds("a \u2014 b", cfg)
    assert "exclamation" in kinds("Wow!", cfg)


def test_word_count_and_links():
    cfg = LintConfig(max_body_words=5, max_links=1)
    assert "word-count" in kinds("one two three four five six", cfg)
    assert "links" in kinds("see https://a.com and https://b.com", cfg)
    assert "links" not in kinds("see https://a.com", cfg)
