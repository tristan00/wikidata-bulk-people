import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import unquote
from unittest.mock import patch

from wikidata_bulk_people._clients import _throttle

class Response:
    def __init__(self, body=None, status=200, text="", headers=None):
        self.body, self.status_code, self.text = body, status, text
        self.headers = headers or {}
    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

def item(qid):
    return {"mainsnak": {"snaktype": "value", "datavalue": {
        "type": "wikibase-entityid", "value": {"id": qid}}}}

def date(year=1900):
    return {"mainsnak": {"datavalue": {"value": {"time": f"+{year}-01-02T00:00:00Z",
            "precision": 11, "calendarmodel": "http://www.wikidata.org/entity/Q1985727"}}}}

class FixtureAPI:
    def __init__(self, count=12):
        self.count = count
        self.calls = Counter()
        self.failure = None
        self.images = 20

    def get(self, url, params=None, **kwargs):
        params = params or kwargs.get("data", {})
        if self.failure is not None:
            self.failure(url, params)
        if "query.wikidata.org" in url:
            self.calls["discovery"] += 1
            query = params["query"]
            cursor = re.search(r'FILTER\(STR\(\?item\) > "http://www.wikidata.org/entity/Q([0-9]+)"\)', query)
            start = int(cursor[1]) + 1 if cursor else 1000000
            page_limit = int(re.search(r"LIMIT (\d+)", query)[1])
            end = min(1000000 + self.count, start + page_limit)
            return Response({"results": {"bindings": [{"item": {"type": "uri",
                "value": f"http://www.wikidata.org/entity/Q{i}"}} for i in range(start, end)]}})
        if "www.wikidata.org" in url:
            ids = params.get("ids", "Q937").split("|")
            if params.get("props") == "labels":
                self.calls["labels"] += 1
                assert len(ids) <= 50
                return Response({"entities": {q: {"id": q, "labels": {"en": {"value": f"Label {q}"}}} for q in ids}})
            self.calls["entities"] += 1
            assert len(ids) <= 50
            return Response({"entities": {q: {"id": q, "lastrevid": int(q[1:]) * 10,
                "labels": {"en": {"value": f"Person {q}"}}, "descriptions": {"en": {"value": "A test person"}},
                "aliases": {"en": [{"value": f"{q} alias one"}, {"value": f"{q} alias two"}]},
                "sitelinks": {"enwiki": {"title": f"Person {q}"}},
                "claims": {"P31": [item("Q5")], "P569": [date()], "P570": [date(1980)],
                    "P19": [item("Q60")], "P20": [item("Q61")], "P21": [item("Q6581097")],
                    "P27": [item("Q30"), item("Q142")],
                    "P106": [item(f"Q{n}") for n in range(100, 105)],
                    "P26": [item("Q200"), item("Q201")]}} for q in ids}})
        if "commons.wikimedia.org" in url:
            self.calls["commons"] += 1
            titles = params["titles"].split("|")
            assert len(titles) <= 50
            return Response({"query": {"pages": {str(i): {"title": title, "imageinfo": [{
                "url": "https://upload.wikimedia.org/" + title[5:],
                "thumburl": "https://upload.wikimedia.org/thumb/" + title[5:],
                "width": 1600, "height": 1200,
                "extmetadata": {"ImageDescription": {"value": "<p>A complete image description</p>"},
                    "LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": "Test artist"}}}]}
                for i, title in enumerate(titles)}}})
        if "/w/rest.php/v1/page/" in url and url.endswith("/html"):
            self.calls["html"] += 1
            title = unquote(url.rsplit("/", 2)[-2]).replace("_", " ")
            qid = title.split()[-1]
            return Response(text="".join(f'<img src="//upload.wikimedia.org/wikipedia/commons/a/ab/{qid}_{i}.jpg" alt="Portrait {i}">' for i in range(self.images)))
        if "en.wikipedia.org/w/api.php" in url:
            self.calls["articles"] += 1
            titles = params["titles"].split("|")
            assert len(titles) <= 20
            return Response({"query": {"pages": {str(i): {"title": title,
                "extract": (f"{title} had a long career in science, literature and public service. " * 10)
                           + "\n\nSecond paragraph must not appear in the lead.",
                "images": [{"title": f"File:{title.split()[-1]}_{j}.jpg"} for j in range(self.images)]}
                for i, title in enumerate(titles)}}})
        raise AssertionError(f"Unexpected network request: {url}")

    def __enter__(self):
        self.patches = [
            patch("requests.Session.get", new=lambda session, url, **kw: self.get(url, **kw)),
            patch("requests.Session.post", new=lambda session, url, **kw: self.get(url, **kw)),
            patch.object(_throttle, "wait", new=lambda *a: None), patch.object(_throttle, "defer", new=lambda *a: None),
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *args):
        for p in reversed(self.patches):
            p.stop()
