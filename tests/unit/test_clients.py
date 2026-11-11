from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from unittest.mock import patch
import pytest
import requests

from tests.support import FixtureAPI, Response
from wikidata_bulk_people._cache import ObjectCache
from wikidata_bulk_people._clients import (
    BaseClient, WikidataClient, WikipediaClient, CommonsClient, RestClient,
    _HostThrottle, _retry_after, _throttle,
)
from wikidata_bulk_people._extract import PersonExtractor
from wikidata_bulk_people import TransportError, NotFoundError

@pytest.mark.parametrize("bad", [
    Response({"error": {"code": "maxlag", "info": "busy"}}),
    Response({}, status=429, headers={"Retry-After": "1"}),
    Response({}, status=503, headers={"Retry-After": "invalid"}),
    Response(ValueError("not JSON")),
    requests.ConnectionError("offline"),
])
def test_transient_response_retries_without_becoming_empty_data(bad):
    client = BaseClient("test")
    with patch.object(client._session, "get", side_effect=[bad, Response({"ok": True})]), \
         patch.object(_throttle, "wait"), patch.object(_throttle, "defer") as defer:
        assert client._get("https://example.org") == {"ok": True}
        assert client.retries == 1 and defer.call_count == 1

def test_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert 58 <= _retry_after(format_datetime(future), 2) <= 60
    assert _retry_after("-1", 5) == 0
    assert _retry_after("junk", 5) == 5

def test_exhaustion_preserves_status_and_does_not_sleep_after_final_attempt():
    client = BaseClient("test")
    with patch.object(client._session, "get", return_value=Response({}, status=503)), \
         patch.object(_throttle, "wait"), patch.object(_throttle, "defer") as defer:
        with pytest.raises(TransportError) as error:
            client._get("https://example.org")
        assert error.value.status_code == 503
        assert defer.call_count == 4

def test_nonretryable_api_error():
    client = BaseClient("test")
    with patch.object(client._session, "get", return_value=Response({"error": {"code": "badvalue"}})) as get, \
         patch.object(_throttle, "wait"):
        with pytest.raises(TransportError, match="badvalue"):
            client._get("https://example.org")
        assert get.call_count == 1

def test_host_throttle_rechecks_shared_cooldown_without_blocking_other_hosts(monkeypatch):
    import wikidata_bulk_people._clients as module
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(module.time, "sleep", sleep)
    throttle = _HostThrottle()
    throttle.wait("a", 0.2)
    throttle.defer("a", 5)
    throttle.wait("b", 0.2)
    assert not sleeps
    throttle.wait("a", 0.2)
    assert sleeps == [5]

def test_batches_have_same_full_output_and_cache_reuses_sources(tmp_path):
    cache = ObjectCache(tmp_path / "cache.db")
    extractor = PersonExtractor("test", cache=cache)
    qids = [f"Q{n}" for n in range(1000000, 1000050)]
    with FixtureAPI(50) as api:
        people = extractor.extract_many(qids)
        assert len(people) == 50
        person = people[qids[0]]
        assert len(person.images) == 20
        assert len(person.spouses) == 2
        assert person.images[1].alt == "Portrait 1"
        assert person.images[0].license == "CC BY-SA 4.0"
        assert person.date_of_birth.year == 1900
        assert "Second paragraph" not in person.lead_paragraph
        assert api.calls == {"entities": 1, "labels": 1, "articles": 3, "commons": 20, "html": 50}
        count = sum(api.calls.values())
        again = extractor.extract_many(qids)
        assert sum(api.calls.values()) == count
        assert again[qids[0]].lastrevid == person.lastrevid
    extractor.close()
    cache.close()

def test_cache_is_bounded_persistent_and_expiring(tmp_path, monkeypatch):
    cache = ObjectCache(tmp_path / "cache", capacity=2, ttl=60)
    cache.put_many({"a": None, "b": {"label": "B"}, "c": [1]})
    assert len(cache._memory) == 2
    assert cache.get("a") == (True, None)
    cache.close()
    cache = ObjectCache(tmp_path / "cache", ttl=0)
    assert cache.get("b") == (False, None)
    cache.close()

def test_entity_batch_boundaries_and_missing():
    client = WikidataClient("test")
    with FixtureAPI():
        assert len(client.get_entities([f"Q{n}" for n in range(1, 123)])) == 122
    with patch.object(client, "_get", return_value={"entities": {"Q999": {"missing": ""}}}):
        with pytest.raises(NotFoundError):
            client.get_entity("Q999")
    with patch.object(client, "_get", return_value={"entities": {}}):
        with pytest.raises(TransportError):
            client.get_entity("Q9999")

def test_pages_merge_continuations_and_title_aliases():
    client = WikipediaClient("test")
    responses = [
        {"query": {"normalized": [{"from": "A_B", "to": "A B"}],
                   "redirects": [{"from": "A B", "to": "C"}],
                   "pages": {"1": {"title": "C", "extract": "Intro", "images": [{"title": "File:1.jpg"}]}}},
         "continue": {"imcontinue": "1|x", "continue": "||"}},
        {"query": {"pages": {"1": {"title": "C", "images": [{"title": "File:2.jpg"}]}}}},
    ]
    with patch.object(client, "_get", side_effect=responses):
        assert client.get_pages(["A_B"])["A_B"] == {"extract": "Intro", "images": ["File:1.jpg", "File:2.jpg"]}

def test_repeated_continuation_and_omitted_titles_fail():
    client = WikipediaClient("test")
    response = {"query": {"pages": {"1": {"title": "A", "extract": "x"}}},
                "continue": {"imcontinue": "same"}}
    with patch.object(client, "_get", return_value=response):
        with pytest.raises(TransportError, match="Repeated"):
            client.get_pages(["A"])
    with patch.object(client, "_get", return_value={"query": {"pages": {"1": {"title": "B", "extract": ""}}}}):
        with pytest.raises(TransportError, match="Incomplete"):
            client.get_pages(["A"])

def test_commons_missing_and_normalized():
    client = CommonsClient("test")
    body = {"query": {"normalized": [{"from": "File:A_B.jpg", "to": "File:A B.jpg"}],
        "pages": {"1": {"title": "File:A B.jpg", "imageinfo": [{"url": "u"}]},
                  "-1": {"title": "File:Absent.jpg", "missing": ""}}}}
    with patch.object(client, "_get", return_value=body):
        assert client.get_image_infos(["File:A_B.jpg", "File:Absent.jpg"]) == {
            "File:A_B.jpg": {"url": "u"}, "File:Absent.jpg": {}}

def test_rest_uses_transport_and_encoded_title():
    client = RestClient("test")
    with patch.object(client, "_request", return_value='<img src="//upload.wikimedia.org/wikipedia/commons/a/ab/A%20B.jpg" alt="alt">') as get:
        assert client.extract_image_alts("A/B") == {"a b.jpg": "alt"}
        assert get.call_args.args[0] == "https://en.wikipedia.org/w/rest.php/v1/page/A%2FB/html"
        client.extract_image_alts("A/B")
        assert get.call_count == 1


def test_redirect_hops_each_consume_the_shared_request_budget():
    client = BaseClient("test")
    redirect = Response(status=302)
    redirect.headers = {"Location": "/w/rest.php/v1/revision/123/html"}
    redirect.close = lambda: None
    with patch.object(client._session, "get", side_effect=[redirect, Response({"ok": True})]) as get, \
            patch.object(_throttle, "wait") as throttle:
        assert client._get("https://en.wikipedia.org/w/rest.php/v1/page/Title/html") == {"ok": True}
    assert client.requests == 2
    assert [c.args for c in throttle.call_args_list].count(("wikimedia-api", 0.3)) == 2
    assert all(c.kwargs["allow_redirects"] is False for c in get.call_args_list)
    assert get.call_args.args[0] == "https://en.wikipedia.org/w/rest.php/v1/revision/123/html"
    client.close()


@pytest.mark.parametrize("location", [None, "http://en.wikipedia.org/unsafe", "https://example.test/unexpected"])
def test_invalid_redirect_is_not_followed(location):
    client = BaseClient("test")
    redirect = Response(status=302)
    redirect.headers = {"Location": location} if location else {}
    redirect.close = lambda: None
    with patch.object(client._session, "get", return_value=redirect) as get, patch.object(_throttle, "wait"):
        with pytest.raises(TransportError, match="redirect"):
            client._get("https://en.wikipedia.org/w/rest.php/v1/page/Title/html")
    assert get.call_count == 1
    client.close()

def test_long_batches_use_read_only_post():
    client = BaseClient("test")
    with patch.object(client._session, "post", return_value=Response({"ok": 1})) as post, patch.object(_throttle, "wait"):
        assert client._get("https://example.org", {"titles": "A" * 8000}) == {"ok": 1}
        assert post.call_count == 1
        assert post.call_args.kwargs["headers"] == {"Promise-Non-Write-API-Action": "true"}


@pytest.mark.parametrize("retry_after", [None, "0", "1", "9"])
def test_maxlag_observes_minimum_wait_and_keeps_cooldown_on_exhaustion(retry_after):
    client = BaseClient("test")
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    response = Response({"error": {"code": "maxlag"}}, headers=headers)
    with patch.object(client._session, "get", return_value=response), \
         patch.object(_throttle, "wait"), patch.object(_throttle, "defer") as defer:
        with pytest.raises(TransportError, match="maxlag"):
            client._get("https://www.wikidata.org/w/api.php")
    assert defer.call_count == 5
    assert all(c.args[1] >= max(5, int(retry_after or 0)) for c in defer.call_args_list)
    client.close()


@pytest.mark.parametrize("interactive", [False, True])
def test_foreground_policy_applies_to_every_action_api_read(interactive):
    from wikidata_bulk_people import extract_person
    requests_seen = []
    with FixtureAPI() as api:
        api.failure = lambda url, params: requests_seen.append((url, dict(params)))
        person = extract_person("Q937", interactive=interactive)
    assert person.qid == "Q937"
    api_params = [p for url, p in requests_seen if url.endswith("/w/api.php")]
    assert api_params
    assert all(("maxlag" not in p) if interactive else p.get("maxlag") == "5" for p in api_params)


def test_foreground_mode_still_respects_rate_limits():
    client = BaseClient("test", interactive=True)
    with patch.object(client._session, "get", side_effect=[
        Response(status=429, headers={"Retry-After": "7"}), Response({"ok": True})]), \
         patch.object(_throttle, "wait") as wait, patch.object(_throttle, "defer") as defer:
        assert client._api_get("https://www.wikidata.org/w/api.php", {"action": "query"}) == {"ok": True}
    assert ("wikimedia-api", 0.3) in [c.args for c in wait.call_args_list]
    assert ("wikimedia-api", 7) in [c.args for c in defer.call_args_list]
    client.close()


def test_rest_json_error_response_is_not_cached_as_empty_alts():
    client = RestClient("test")
    response = Response(text='{"error":"server failure"}', headers={"Content-Type": "application/json"})
    with patch.object(client._session, "get", return_value=response), patch.object(_throttle, "wait"), patch.object(_throttle, "defer"):
        with pytest.raises(TransportError, match="HTML"):
            client.extract_image_alts("Title")
    assert client.cache.get("enwiki:alts:Title") == (False, None)


def test_preferred_current_spouse_does_not_drop_former_relationships():
    from tests.support import item, date
    current = {**item("Q20"), "rank": "preferred"}
    former = {**item("Q21"), "rank": "normal", "qualifiers": {"P582": [date(1920)["mainsnak"]]}}
    deprecated = {**item("Q22"), "rank": "deprecated"}
    entity = {"id": "Q1", "claims": {"P31": [item("Q5")], "P26": [current, former, deprecated]}}
    extractor = PersonExtractor("test")
    try:
        with FixtureAPI(), patch.object(extractor._wd, "get_entities", return_value={"Q1": entity}):
            person = extractor.extract_many(["Q1"])["Q1"]
        assert [(s.qid, s.name, s.is_former) for s in person.spouses] == [
            ("Q20", "Label Q20", False), ("Q21", "Label Q21", True)]
    finally:
        extractor.close()
def test_wikimedia_rate_budget_and_429_cooldown_are_shared_across_projects(monkeypatch):
    from wikidata_bulk_people import _clients as module
    from tests.support import Response
    waits, cooldowns = [], []
    monkeypatch.setattr(module._throttle, "wait", lambda host, interval: waits.append((host, interval)))
    monkeypatch.setattr(module._throttle, "defer", lambda host, delay: cooldowns.append((host, delay)))
    replies = iter([Response(status=429, headers={"Retry-After": "7"}), Response({}), Response({})])
    monkeypatch.setattr(module.requests.Session, "get", lambda *a, **kw: next(replies))
    client = module.BaseClient("test")
    try:
        client._get("https://commons.wikimedia.org/w/api.php")
        client._get("https://en.wikipedia.org/w/api.php")
    finally:
        client.close()
    assert waits.count(("wikimedia-api", 0.3)) == 3
    assert ("wikimedia-api", 7) in cooldowns


def test_network_concurrency_is_bounded_across_clients(monkeypatch):
    import concurrent.futures
    import threading
    from wikidata_bulk_people import _clients as module
    from tests.support import Response
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    active = peak = 0
    def request(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 3:
                entered.set()
        try:
            assert release.wait(5)
            return Response({})
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(module._throttle, "wait", lambda *a: None)
    monkeypatch.setattr(module.requests.Session, "get", request)
    clients = [module.BaseClient("test") for _ in range(4)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(client._get, "https://en.wikipedia.org/w/api.php") for client in clients]
            try:
                assert entered.wait(3)
                assert peak == 3
            finally:
                release.set()
            assert all(f.result() == {} for f in futures)
    finally:
        for client in clients:
            client.close()
    assert peak == 3
