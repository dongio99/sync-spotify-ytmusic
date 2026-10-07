from datetime import datetime, timedelta, timezone

from sync import ran_today


def test_ran_today():
    now = datetime.now(timezone.utc)
    assert ran_today({"last_success": now.isoformat()})
    assert not ran_today({"last_success": (now - timedelta(days=2)).isoformat()})
    assert not ran_today({"last_success": None})
