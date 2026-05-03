"""Integration tests — make real network calls to Wikidata/Wikipedia.

Run with:
    pytest -m integration -v
"""

import logging

import pytest


@pytest.mark.integration
def test_extract_einstein() -> None:
    """Fetch Albert Einstein (Q937) from Wikidata and verify key fields."""
    from wikidata_bulk_people import extract_person

    person = extract_person("Q937")

    assert person.qid == "Q937"
    assert person.name == "Albert Einstein"
    assert person.date_of_birth is not None
    assert person.date_of_birth.year == 1879
    assert person.date_of_birth.month == 3
    assert person.date_of_birth.day == 14
    assert person.date_of_death is not None
    assert person.date_of_death.year == 1955
    assert person.sex_or_gender == "male"
    assert len(person.occupations) > 0


def _collect_qids(filter_: object) -> set[str]:
    """Run QIDStream against the live WDQS endpoint and collect all QIDs."""
    from wikidata_bulk_people._extract import QIDStream

    stream = QIDStream(filter=filter_, user_agent="wikidata-bulk-people-tests/0.1")  # type: ignore[arg-type]
    return set(stream)


@pytest.mark.integration
def test_unordered_skip_rate_small_filter(caplog: pytest.LogCaptureFixture) -> None:
    """Compare ordered vs unordered results on a tightly-bounded filter.

    Picks a filter expected to fit in a single 500-item WDQS page so the
    comparison is meaningful (multipage WDQS responses are flaky on their own).
    Asserts unordered is a subset of ordered (no phantom QIDs) and logs the
    empirical skip rate for the user to inspect.
    """
    from wikidata_bulk_people._models import PeopleFilter

    base = {
        "occupation_qid": "Q169470",  # physicist
        "born_after": 1900,
        "born_before": 1905,
        "has_wikipedia_article": True,
    }

    ordered_qids = _collect_qids(PeopleFilter(ordered=True, **base))
    unordered_qids = _collect_qids(PeopleFilter(ordered=False, **base))

    # Both should return at least some results; if not, WDQS likely throttled.
    if not ordered_qids:
        pytest.skip("WDQS returned zero results for the ordered baseline — cannot compare")

    skipped = ordered_qids - unordered_qids
    spurious = unordered_qids - ordered_qids
    skip_rate = len(skipped) / len(ordered_qids)

    caplog.set_level(logging.INFO)
    logging.info(
        "[unordered small-filter] ordered=%d unordered=%d skipped=%d skip_rate=%.2f%% spurious=%d",
        len(ordered_qids),
        len(unordered_qids),
        len(skipped),
        skip_rate * 100,
        len(spurious),
    )

    # Unordered must never invent results that aren't in the ordered baseline.
    assert not spurious, (
        f"unordered mode returned {len(spurious)} QIDs not in ordered baseline: "
        f"{sorted(spurious)[:10]}"
    )
    # On a single-page filter the two modes should largely agree. Generous bound;
    # tighten once we've gathered empirical data.
    assert skip_rate < 0.10, f"unordered skipped {skip_rate * 100:.1f}% of results (>10%)"


@pytest.mark.integration
def test_unordered_handles_multipage(caplog: pytest.LogCaptureFixture) -> None:
    """Run unordered against a filter expected to span multiple pages.

    This is the load-bearing test: in ordered mode WDQS often throttles after
    the first page on complex filters. In unordered mode the throttling should
    ease up and the iterator should make it through several pages. We don't
    assert exact skip rate (WDQS is non-deterministic) — just record it.
    """
    from wikidata_bulk_people._models import PeopleFilter

    # All physicists born in a wider range, no Wikipedia constraint — likely 1k+ results.
    base = {
        "occupation_qid": "Q169470",
        "born_after": 1850,
        "born_before": 1900,
        "has_wikipedia_article": True,
    }

    unordered_qids = _collect_qids(PeopleFilter(ordered=False, **base))

    caplog.set_level(logging.INFO)
    logging.info(
        "[unordered multipage] unordered=%d (this is the count we got through before WDQS gave up)",
        len(unordered_qids),
    )

    # Sanity bound: if unordered returned < 100 results on a likely-thousands query,
    # something's wrong (WDQS still throttling, or our cursor logic is broken).
    assert len(unordered_qids) > 100, (
        f"unordered mode only returned {len(unordered_qids)} QIDs — expected > 100. "
        "WDQS may be throttling, or the cursor logic isn't advancing correctly."
    )
