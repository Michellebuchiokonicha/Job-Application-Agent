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


def test_bracketed_link():
    assert clean_for_reading("[https://example.com/a]") == "[[link: example.com]]"


def test_malformed_url_does_not_crash():
    # A "[" in the host made urlsplit raise "Invalid IPv6 URL" and crashed labelling.
    # The property that matters: any text in, a string out, no exception.
    for text in [
        "https://exa[mple.com/x",
        "http://]bad/",
        "https://[::1",
        "see [https://x",
    ]:
        assert isinstance(clean_for_reading(text), str)
