from jobagent.core.text import clean_for_reading


def test_entities_decoded():
    assert (
        clean_for_reading("You&apos;re in&nbsp;&amp; welcome") == "You're in & welcome"
    )


def test_url_collapsed_to_domain():
    text = "Book here: https://calendly.com/jane/30min?utm_source=x&tok=abc123 thanks"
    assert clean_for_reading(text) == "Book here: [link: calendly.com] thanks"


def test_www_dropped_and_parens_kept():
    assert clean_for_reading("(https://www.hackerrank.com/test/xyz)") == (
        "([link: hackerrank.com])"
    )


def test_long_tracking_token_removed():
    token = "A1x-2Btac-2FHKQ6Bj6FNW8LyNMWW" * 4
    assert clean_for_reading(f"click {token} now") == "click […] now"


def test_normal_text_untouched():
    text = "Thank you for applying to the ML Engineer role."
    assert clean_for_reading(text) == text
