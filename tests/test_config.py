import pytest

from bot.config import ConfigError, _id_set


def test_unset_means_no_restriction(monkeypatch):
    monkeypatch.delenv("ALLOWED_CHATS", raising=False)
    assert _id_set("ALLOWED_CHATS") is None


def test_valid_lists_are_parsed(monkeypatch):
    monkeypatch.setenv("ALLOWED_CHATS", "-1001234567890, 42; 7")
    assert _id_set("ALLOWED_CHATS") == {-1001234567890, 42, 7}


def test_a_typo_never_silently_disables_the_allowlist(monkeypatch):
    # The dangerous case: dropping the unreadable id would leave an empty set,
    # and an empty set reads as "every chat is allowed".
    monkeypatch.setenv("ALLOWED_CHATS", "-100abc")
    with pytest.raises(ConfigError, match="not a numeric Telegram id"):
        _id_set("ALLOWED_CHATS")


def test_one_bad_id_among_good_ones_is_still_fatal(monkeypatch):
    monkeypatch.setenv("ALLOWED_CHATS", "-1001234567890,@mychat")
    with pytest.raises(ConfigError):
        _id_set("ALLOWED_CHATS")


def test_separators_only_is_fatal(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", " , ; ")
    with pytest.raises(ConfigError, match="lists no ids"):
        _id_set("ADMIN_IDS")
