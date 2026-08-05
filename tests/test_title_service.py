from __future__ import annotations

from app.services import title_service


class FakeUser:
    id = "u1"
    display_name = "何凯旋"
    username = "hekaixuan"


PRESET = {
    "name": "湖南承希科技有限公司",
    "tax_id": "9143011105389479XW",
    "address": "",
    "phone": "",
    "bank_name": "",
    "bank_account": "",
    "bank_code": "",
}


def test_parse_presets() -> None:
    assert title_service.parse_presets("") == []
    assert title_service.parse_presets("not-json") == []
    assert title_service.parse_presets("{}") == []
    data = '[{"name":"A公司","tax_id":"T1","bank_name":"B行"}]'
    assert title_service.parse_presets(data) == [
        {"name": "A公司", "tax_id": "T1", "address": "", "phone": "",
         "bank_name": "B行", "bank_account": "", "bank_code": ""}
    ]


def test_warning_rules(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(title_service, "env_presets", lambda: [PRESET])
    monkeypatch.setattr(title_service, "user_titles", lambda db, uid: [])

    assert title_service.title_warning(None, FakeUser(), "湖南承希科技有限公司", "9143011105389479XW") == ""
    assert title_service.title_warning(None, FakeUser(), "湖南承希科技有限公司", "其他税号") == ""
    assert title_service.title_warning(None, FakeUser(), "某外部公司", "9143011105389479XW") == ""
    assert title_service.title_warning(None, FakeUser(), "某外部公司", "999") != ""
    assert title_service.title_warning(None, FakeUser(), "湖南承希科技有限公司（华东分公司）", "") == ""
    assert title_service.title_warning(None, FakeUser(), "何凯旋", "") == ""
    assert title_service.title_warning(None, FakeUser(), "hekaixuan", "") == ""
    assert title_service.title_warning(None, FakeUser(), "", "") == ""


def test_no_presets_accepts_all(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(title_service, "env_presets", lambda: [])
    monkeypatch.setattr(title_service, "user_titles", lambda db, uid: [])
    assert title_service.title_warning(None, FakeUser(), "任意公司", "任意税号") == ""


def test_user_custom_title_accepted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(title_service, "env_presets", lambda: [PRESET])

    class Row:
        name = "个人代收抬头"
        tax_id = ""
        address = ""
        phone = ""
        bank_name = ""
        bank_account = ""
        bank_code = ""

    monkeypatch.setattr(title_service, "user_titles", lambda db, uid: [Row()])
    assert title_service.title_warning(None, FakeUser(), "个人代收抬头", "") == ""
