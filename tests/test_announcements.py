"""Announcement feed: category tagging, parsing, cache and degradation."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services import announcements

DAY = date(2026, 11, 11)


def stamp(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day).timestamp() * 1000)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return FakeResponse(self.payload, self.status)


PAYLOAD = {
    "announcements": [
        {
            "announcementTitle": "<em>招商银行</em>股份有限公司2026年第三季度报告",
            "announcementTime": stamp(2026, 10, 31),
            "adjunctUrl": "finalpage/2026-10-31/1225649281.PDF",
        },
        {
            "announcementTitle": "关于回购股份进展的公告",
            "announcementTime": stamp(2026, 11, 2),
            "adjunctUrl": "finalpage/2026-11-02/2.PDF",
        },
    ]
}


@pytest.fixture(autouse=True)
def temp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(announcements, "CACHE_DIR", tmp_path)


def test_classify_categories() -> None:
    assert announcements.classify("2026年第三季度报告") == "业绩"
    assert announcements.classify("关于回购股份的公告") == "股东"
    assert announcements.classify("关于诉讼事项的风险提示") == "风险"
    assert announcements.classify("重大合同中标公告") == "经营"
    assert announcements.classify("[H股公告]更换秘书") == "H股"
    assert announcements.classify("董事会决议") == "公告"


def test_get_announcements_parses_and_sorts() -> None:
    transport = FakeTransport(PAYLOAD)
    result = announcements.get_announcements("600036", transport=transport, today=DAY)

    assert result["available"] is True
    assert result["count"] == 2
    assert [item["date"] for item in result["entries"]] == ["2026-11-02", "2026-10-31"]
    assert result["entries"][0]["type"] == "股东"
    assert result["entries"][1]["title"].startswith("招商银行股份有限公司")
    assert result["entries"][1]["url"] == "http://static.cninfo.com.cn/finalpage/2026-10-31/1225649281.PDF"

    sent = transport.calls[0]["data"]
    assert sent["stock"] == "600036,gssh0600036"
    assert sent["seDate"].endswith(DAY.isoformat())


def test_get_announcements_uses_cache() -> None:
    first = announcements.get_announcements("600036", transport=FakeTransport(PAYLOAD), today=DAY)
    assert first["available"] is True

    class Broken:
        def __call__(self, *args, **kwargs):
            raise AssertionError("cache should prevent this call")

    second = announcements.get_announcements("600036", transport=Broken(), today=DAY)
    assert second["count"] == 2


def test_get_announcements_handles_failure() -> None:
    class Failing:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("network down")

    result = announcements.get_announcements("601988", transport=Failing(), today=DAY)
    assert result["available"] is False
    assert result["entries"] == []
    assert result["error"]


def test_get_announcements_stale_cache_after_failure() -> None:
    announcements.get_announcements("600036", transport=FakeTransport(PAYLOAD), today=DAY)

    class Failing:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("network down")

    stale = announcements.get_announcements(
        "600036", transport=Failing(), today=DAY, refresh=True
    )
    assert stale["available"] is True
    assert stale.get("stale") is True
