import pytest

from api.core import query_cache


def test_execute_with_cache_does_not_cache_errors(monkeypatch):
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_ENABLED", True)
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_TTL_SECONDS", 900)
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_MAX_ROWS", 1000)
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_MAX_ENTRIES", 512)
    query_cache._QUERY_CACHE.clear()

    calls = {"count": 0}

    def failing_executor(_sql):
        calls["count"] += 1
        raise RuntimeError("database failed")

    with pytest.raises(RuntimeError):
        query_cache.execute_with_cache(
            failing_executor,
            "SELECT * FROM broken_table",
            db_url="postgresql://example/db",
            db_type="postgres",
        )

    with pytest.raises(RuntimeError):
        query_cache.execute_with_cache(
            failing_executor,
            "SELECT * FROM broken_table",
            db_url="postgresql://example/db",
            db_type="postgres",
        )

    assert calls["count"] == 2
    assert len(query_cache._QUERY_CACHE) == 0


def test_execute_with_cache_caches_successful_select(monkeypatch):
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_ENABLED", True)
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_TTL_SECONDS", 900)
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_MAX_ROWS", 1000)
    monkeypatch.setattr(query_cache.Config, "SQL_QUERY_CACHE_MAX_ENTRIES", 512)
    query_cache._QUERY_CACHE.clear()

    calls = {"count": 0}

    def executor(_sql):
        calls["count"] += 1
        return [{"value": calls["count"]}]

    first = query_cache.execute_with_cache(
        executor,
        "SELECT 1",
        db_url="postgresql://example/db",
        db_type="postgres",
    )
    second = query_cache.execute_with_cache(
        executor,
        "SELECT 1",
        db_url="postgresql://example/db",
        db_type="postgres",
    )

    assert first == [{"value": 1}]
    assert second == [{"value": 1}]
    assert calls["count"] == 1
