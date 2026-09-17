import datetime as dt
import json

from f1hotel.models import Offer
from f1hotel.notify import (
    MAX_NEW_LISTED,
    Notifier,
    clip_bytes,
    build_diff_message,
    due_reminders,
    mark_error_notified,
    mark_reminder_sent,
    should_notify_error,
)
from f1hotel.state import Diff


def mk(i, price, tier=1, source="rakuten"):
    return Offer(
        source=source,
        hotel_id=str(i),
        hotel_name=f"宿{i}",
        area_label="鈴鹿",
        tier=tier,
        party="親子2人1室",
        checkin="2027-04-09",
        checkout="2027-04-11",
        nights=2,
        plan_id="p",
        plan_name="素泊まり",
        room_name="ツイン",
        total_price=price,
        url=f"https://example/{i}",
    )


def test_build_diff_message(cfg):
    d = Diff(new=[mk(1, 80000), mk(2, 30000)], price_changed=[(mk(3, 50000), mk(3, 45000))], gone=[mk(4, 1)])
    title, body, click = build_diff_message(d, cfg)
    assert "新規2件" in title and "(即1)" in title
    assert body.index("宿2") < body.index("宿1")  # 即 が先
    assert "50,000円 → 45,000円" in body and "消滅 1件" in body
    assert click == "https://example/2"
    assert build_diff_message(Diff(), cfg) == ("", "", None)


class Resp:
    def __init__(self, status=200, text="ok"):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Sess:
    def __init__(self, status=200):
        self.posts = []
        self.status = status

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return Resp(self.status)


def test_clip_bytes_counts_utf8():
    assert clip_bytes("あいう", 100) == "あいう"
    out = clip_bytes("あ" * 100, 30)
    assert len(out.encode("utf-8")) <= 30 + len("\n…（省略）".encode("utf-8"))
    assert out.endswith("（省略）")


def test_large_diff_body_fits_ntfy_limit(cfg, monkeypatch):
    many = [mk(i, 30000 + i, tier=(i % 4) + 1) for i in range(200)]
    for i, o in enumerate(many):
        o.party = ["親子2人1室", "4人1室", "4人2室"][i % 3]
        o.hotel_name = "とても長いホテル名前テスト施設" * 2
    d = Diff(new=many, gone=many[:150])
    title, body, _ = build_diff_message(d, cfg)
    assert "新規空き 200件" in body and "親子2人1室" in body.split("\n")[1]
    assert body.count("【") == MAX_NEW_LISTED
    assert "…他 188 件" in body and "消滅 150件" in body

    monkeypatch.setenv("NTFY_TOPIC", "t")
    s = Sess()
    assert Notifier(channels=["ntfy"], session=s).send(title, body) == []
    sent = s.posts[0][1]["data"]
    assert len(sent) < 4096  # 実際に送るバイト列が ntfy の上限を超えない
    payload = json.loads(sent)
    assert len(payload["message"].encode("utf-8")) <= 2600


def test_ntfy_payload(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "f1-test")
    monkeypatch.delenv("NTFY_ERROR_TOPIC", raising=False)
    s = Sess()
    n = Notifier(channels=["ntfy"], session=s)
    assert n.send("T", "B", click="https://x") == []
    url, kw = s.posts[0]
    payload = json.loads(kw["data"])
    assert url == "https://ntfy.sh" and payload["topic"] == "f1-test" and payload["click"] == "https://x"
    n.send("E", "B", error=True)
    assert json.loads(s.posts[1][1]["data"])["topic"] == "f1-test-errors"


def test_line_payload_and_failure(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("LINE_USER_ID", "U1")
    s = Sess()
    n = Notifier(channels=["line"], session=s)
    assert n.send("T", "B") == []
    url, kw = s.posts[0]
    assert "api.line.me" in url and kw["json"]["to"] == "U1" and kw["json"]["messages"][0]["text"] == "T\nB"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    bad = Notifier(channels=["line"], session=Sess(status=401))
    fails = bad.send("T", "B")
    assert len(fails) == 1 and fails[0].startswith("line:")


def test_missing_config_reports_failure(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    n = Notifier(channels=["ntfy", "bogus"], session=Sess())
    fails = n.send("T", "B")
    assert len(fails) == 2


def test_no_channels_is_noop():
    assert Notifier(channels=[], session=Sess()).send("T", "B") == []


def test_error_throttle(tmp_path):
    today = dt.date(2026, 11, 1)
    assert should_notify_error(tmp_path, today)
    mark_error_notified(tmp_path, today)
    assert not should_notify_error(tmp_path, today)
    assert should_notify_error(tmp_path, today + dt.timedelta(days=1))


def test_reminders(cfg):
    cfg.reminders = [{"date": "2026-10-31", "message": "m1"}, {"date": "2026-11-01", "message": "m2"}]
    day = dt.date(2026, 10, 31)
    due = due_reminders(cfg, cfg.data_dir, day)
    assert [r["message"] for r in due] == ["m1"]
    mark_reminder_sent(cfg.data_dir, due[0])
    assert due_reminders(cfg, cfg.data_dir, day) == []
