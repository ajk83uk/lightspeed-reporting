import os
import uuid
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
import pytest

# These tests need a throwaway Postgres loaded with feedback/tests/schema.sql. They never touch Neon.
TEST_DB = os.environ.get("FEEDBACK_TEST_DATABASE_URL")
if not TEST_DB:
    pytest.skip("set FEEDBACK_TEST_DATABASE_URL to a scratch Postgres to run these tests", allow_module_level=True)

from feedback import app as appmod  # noqa: E402
from feedback import config, mailer  # noqa: E402
from feedback import send as send_feedback  # noqa: E402

SOL = 1718940401139720
YDAY = date.today() - timedelta(days=1)


@pytest.fixture(autouse=True)
def _point_service_at_scratch_db(monkeypatch):
    """Per test only, so other test modules (notify) never see the scratch database."""
    monkeypatch.setenv("DATABASE_URL", TEST_DB)


class DB:
    def __init__(self):
        self.conn = psycopg2.connect(TEST_DB, cursor_factory=psycopg2.extras.RealDictCursor)
        self.conn.autocommit = True

    def execute(self, sql, params=None):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur

    def close(self):
        self.conn.close()


@pytest.fixture()
def db():
    conn = DB()
    for t in ("feedback_alerts", "feedback_category_ratings", "feedback_dish_ratings", "feedback_responses",
              "feedback_requests", "feedback_suppressions", "bookings", "v_feedback_dishes", "cand_src"):
        conn.execute(f"truncate {t} cascade")
    yield conn
    conn.close()


@pytest.fixture()
def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def add_guest(db, ref="R1", email="priya@example.com", name="priya", loc=SOL, vd=YDAY, dishes=("Butter Chicken", "Garlic Naan")):
    db.execute("insert into bookings values (%s,2084,%s,%s,%s,%s,%s,2,'Solihull')", (loc, ref, ref[1:], vd, name, email))
    db.execute("insert into cand_src values (2084,%s,%s,%s,'Solihull',%s,2,%s,%s)", (loc, ref, ref[1:], vd, name, email))
    for i, d in enumerate(dishes):
        db.execute("insert into v_feedback_dishes values (%s,%s,%s,%s,%s,%s)", (loc, ref, vd, f"S{i}", d, 2 + i))


def add_request(db, ref="R1", status="sent", sent_days_ago=0, loc=SOL, vd=YDAY, dry=False):
    tok = uuid.uuid4()
    db.execute(
        "insert into feedback_requests (token,business_location_id,ft_site_code,booking_ref_no,booking_code,visit_date,covers,status,dry_run,sent_at) "
        "values (%s,%s,2084,%s,%s,%s,2,%s,%s, now() - make_interval(days => %s))",
        (tok, loc, ref, ref[1:], vd, status, dry, sent_days_ago),
    )
    return tok


def good_payload(**over):
    p = {"categories": {"food": {"rating": 5, "tags": [], "comment": ""}, "drink": {"rating": 4},
                        "service": {"rating": 5}, "atmosphere": {"rating": 4}},
         "dishes": [{"dish": "Butter Chicken", "rating": 5}], "other": "", "contactOk": False}
    p.update(over)
    return p


# ---- survey data
def test_open_survey_returns_dishes_and_marks_opened(db, client):
    add_guest(db)
    tok = add_request(db)
    r = client.get(f"/api/survey/{tok}")
    j = r.get_json()
    assert r.status_code == 200 and j["status"] == "open" and j["site"] == "Solihull" and j["guestName"] == "Priya"
    assert [d["name"] for d in j["dishes"]] == ["Garlic Naan", "Butter Chicken"]  # highest quantity first
    assert j["sitePhone"] == "0121 711 1166"
    row = db.execute("select status, opened_at from feedback_requests where token=%s", (tok,)).fetchone()
    assert row["status"] == "opened" and row["opened_at"] is not None


def test_bad_and_unknown_tokens_404(db, client):
    assert client.get("/api/survey/not-a-uuid").status_code == 404
    assert client.get(f"/api/survey/{uuid.uuid4()}").status_code == 404
    tok = add_request(db, status="suppressed")
    assert client.get(f"/api/survey/{tok}").status_code == 404


def test_expired_link(db, client):
    add_guest(db)
    tok = add_request(db, sent_days_ago=15)
    assert client.get(f"/api/survey/{tok}").get_json()["status"] == "expired"
    assert client.post(f"/api/survey/{tok}/submit", json=good_payload()).status_code == 410


# ---- submit
def test_submit_is_one_time_only(db, client):
    add_guest(db)
    tok = add_request(db)
    assert client.post(f"/api/survey/{tok}/submit", json=good_payload()).status_code == 200
    assert client.post(f"/api/survey/{tok}/submit", json=good_payload()).status_code == 409
    assert client.get(f"/api/survey/{tok}").get_json()["status"] == "completed"
    assert db.execute("select count(*) n from feedback_responses").fetchone()["n"] == 1
    assert db.execute("select count(*) n from feedback_category_ratings").fetchone()["n"] == 4
    resp = db.execute("select overall_rating from feedback_responses").fetchone()
    assert resp["overall_rating"] == 5  # mean 4.5 rounds up


def test_dishes_not_eaten_are_ignored(db, client):
    add_guest(db)
    tok = add_request(db)
    p = good_payload(dishes=[{"dish": "Butter Chicken", "rating": 4}, {"dish": "Lobster Thermidor", "rating": 1}])
    assert client.post(f"/api/survey/{tok}/submit", json=p).status_code == 200
    names = [r["dish_name"] for r in db.execute("select dish_name from feedback_dish_ratings")]
    assert names == ["Butter Chicken"]


def test_invalid_submission_does_not_consume_link(db, client):
    add_guest(db)
    tok = add_request(db)
    bad = {"categories": {c: {"rating": None} for c in ("food", "drink", "service", "atmosphere")}}
    assert client.post(f"/api/survey/{tok}/submit", json=bad).status_code == 400
    assert client.post(f"/api/survey/{tok}/submit", data="nope", content_type="text/plain").status_code == 400
    assert client.post(f"/api/survey/{tok}/submit", json=good_payload()).status_code == 200


def test_no_alert_for_happy_guest(db, client):
    add_guest(db)
    tok = add_request(db)
    client.post(f"/api/survey/{tok}/submit", json=good_payload())
    assert db.execute("select count(*) n from feedback_alerts").fetchone()["n"] == 0


def test_low_score_and_health_alerts(db, client):
    add_guest(db)
    tok = add_request(db)
    p = good_payload()
    p["categories"]["food"] = {"rating": 2, "tags": ["health"], "comment": "felt ill after the curry"}
    p["categories"]["service"] = {"rating": 3, "tags": [], "comment": "slow"}
    client.post(f"/api/survey/{tok}/submit", json=p)
    alerts = {r["kind"]: r["summary"] for r in db.execute("select kind, summary from feedback_alerts")}
    assert set(alerts) == {"low_score", "health"}
    assert "food 2*" in alerts["low_score"] and "service 3*" in alerts["low_score"]
    assert "felt ill" in alerts["health"] and "Solihull" in alerts["health"]
    assert "priya@example.com" not in alerts["low_score"]  # no email address in alert text
    assert db.execute("select flagged_health from feedback_responses").fetchone()["flagged_health"] is True


def test_low_dish_score_alerts(db, client):
    add_guest(db)
    tok = add_request(db)
    client.post(f"/api/survey/{tok}/submit", json=good_payload(dishes=[{"dish": "Garlic Naan", "rating": 1}]))
    s = db.execute("select summary from feedback_alerts where kind='low_score'").fetchone()["summary"]
    assert "Garlic Naan 1*" in s


# ---- unsubscribe
def test_unsubscribe_suppresses_future_requests(db, client):
    add_guest(db, ref="R1")
    tok = add_request(db, ref="R1")
    assert client.get(f"/u/{tok}").status_code == 200  # page only, no change
    assert db.execute("select count(*) n from feedback_suppressions").fetchone()["n"] == 0
    assert client.post(f"/u/{tok}").status_code == 200
    assert db.execute("select email_lower from feedback_suppressions").fetchone()["email_lower"] == "priya@example.com"
    add_guest(db, ref="R2", email="Priya@Example.com", vd=YDAY - timedelta(days=1))
    assert db.execute("select count(*) n from v_feedback_candidates where booking_ref_no='R2'").fetchone()["n"] == 0


# ---- mailer
def test_message_headers_use_single_sender():
    msg = mailer.build_message(token=uuid.uuid4(), to_email="a@b.com", first_name="priya",
                               site=config.SITES[SOL], visit_date_text="Thursday 26 November")
    from email.utils import parseaddr
    assert parseaddr(msg["From"]) == ("Tap & Tandoor Solihull", "feedback@tapandtandoor.co.uk")
    assert msg["Reply-To"] == "feedback@tapandtandoor.co.uk"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "Solihull" in msg["Subject"] and msg["Subject"].startswith("Priya, how")
    html_body = msg.get_body(preferencelist=("html",)).get_content()
    assert "/r/" in html_body and "/u/" in html_body and "feedback.tapandtandoor.co.uk/assets/logo.png" in html_body


def test_message_without_name_and_html_escaped():
    msg = mailer.build_message(token=uuid.uuid4(), to_email="a@b.com", first_name="<b>x</b>",
                               site=config.SITES[SOL], visit_date_text="Thursday 26 November")
    assert "<b>x</b>" not in msg.get_body(preferencelist=("html",)).get_content()
    msg2 = mailer.build_message(token=uuid.uuid4(), to_email="a@b.com", first_name="",
                                site=config.SITES[SOL], visit_date_text="Thursday 26 November")
    assert msg2["Subject"].startswith("How was")


# ---- sender job
class FakeProvider:
    name = "fake"

    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], fail_for

    def send(self, msg, key):
        if key in self.fail_for:
            raise RuntimeError("boom")
        self.sent.append((msg["To"], key))
        return key


class connect:
    """Context manager giving a fresh psycopg2 connection that is closed afterwards."""
    def __enter__(self):
        self.conn = psycopg2.connect(TEST_DB, cursor_factory=psycopg2.extras.RealDictCursor)
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


def test_dry_run_writes_previews_and_leaves_database_untouched(db, tmp_path):
    add_guest(db, ref="R1")
    add_guest(db, ref="R2", email="sam@example.com", name="sam")
    prov = mailer.DryRunProvider(str(tmp_path))
    with connect() as conn:
        out = send_feedback.run(conn, visit_date=YDAY, live=False, provider=prov)
    assert out == {"Solihull": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["solihull-R1.eml", "solihull-R1.html", "solihull-R2.eml", "solihull-R2.html"]
    assert db.execute("select count(*) n from feedback_requests").fetchone()["n"] == 0


def test_persisted_dry_run_rows_are_flagged_and_excluded_from_reports(db, tmp_path):
    add_guest(db)
    with connect() as conn:
        send_feedback.run(conn, visit_date=YDAY, live=False, persist_dry_run=True, provider=mailer.DryRunProvider(str(tmp_path)))
    row = db.execute("select dry_run, status, sent_at from feedback_requests").fetchone()
    assert row["dry_run"] is True and row["status"] == "queued" and row["sent_at"] is None


def test_live_run_sends_once_and_marks_sent(db):
    add_guest(db, ref="R1")
    add_guest(db, ref="R2", email="sam@example.com", name="sam")
    prov = FakeProvider()
    with connect() as conn:
        assert send_feedback.run(conn, visit_date=YDAY, live=True, provider=prov) == {"Solihull": 2}
    assert len(prov.sent) == 2
    assert db.execute("select count(*) n from feedback_requests where status='sent' and sent_at is not null and dry_run=false").fetchone()["n"] == 2
    with connect() as conn:  # re-run the same day must not email again
        assert send_feedback.run(conn, visit_date=YDAY, live=True, provider=prov) == {}
    assert len(prov.sent) == 2


def test_failed_send_is_marked_failed_and_not_retried(db):
    add_guest(db, ref="R1")
    prov = FakeProvider(fail_for=("solihull-R1",))
    with connect() as conn:
        out = send_feedback.run(conn, visit_date=YDAY, live=True, provider=prov)
    assert out == {"failed:Solihull": 1}
    assert db.execute("select status from feedback_requests").fetchone()["status"] == "failed"
    with connect() as conn:
        assert send_feedback.run(conn, visit_date=YDAY, live=True, provider=prov) == {}


def test_same_person_not_emailed_twice_in_14_days(db):
    add_guest(db, ref="OLD", vd=YDAY - timedelta(days=5))
    add_request(db, ref="OLD", status="sent", sent_days_ago=4, vd=YDAY - timedelta(days=5))
    add_guest(db, ref="NEW", email="PRIYA@example.com")
    prov = FakeProvider()
    with connect() as conn:
        assert send_feedback.run(conn, visit_date=YDAY, live=True, provider=prov) == {}
    assert prov.sent == []


def test_duplicate_email_within_run_and_site_filter_and_cap(db):
    add_guest(db, ref="R1", email="same@example.com")
    add_guest(db, ref="R2", email="SAME@example.com")
    add_guest(db, ref="R3", email="third@example.com")
    add_guest(db, ref="R4", email="fourth@example.com")
    prov = FakeProvider()
    with connect() as conn:
        out = send_feedback.run(conn, visit_date=YDAY, live=True, limit=2, provider=prov)
    assert out == {"Solihull": 2, "duplicate_email": 1, "capped": 1}
    with connect() as conn:
        assert send_feedback.run(conn, visit_date=YDAY, site="Portsmouth", live=False, provider=FakeProvider()) == {}


def test_live_flag_without_enable_env_is_refused(db, monkeypatch, tmp_path, capsys):
    add_guest(db)
    monkeypatch.delenv("FEEDBACK_SEND_ENABLED", raising=False)
    rc = send_feedback.main(["--live", "--out", str(tmp_path), "--date", YDAY.isoformat()])
    assert rc == 0 and "Refusing to send" in capsys.readouterr().err
    assert db.execute("select count(*) n from feedback_requests").fetchone()["n"] == 0


def test_live_without_provider_exits_without_sending(db, monkeypatch, tmp_path, capsys):
    add_guest(db)
    monkeypatch.setenv("FEEDBACK_SEND_ENABLED", "true")
    monkeypatch.delenv("FEEDBACK_PROVIDER", raising=False)
    assert send_feedback.main(["--live", "--out", str(tmp_path)]) == 2
    assert db.execute("select count(*) n from feedback_requests").fetchone()["n"] == 0


def test_purge_removes_only_unsent_dry_runs(db):
    add_request(db, ref="D1", status="queued", dry=True)
    keep = add_request(db, ref="L1", status="sent", dry=False)
    assert send_feedback.main(["--purge-dry-runs"]) == 0
    left = [r["token"] for r in db.execute("select token from feedback_requests")]
    assert left == [keep]


def test_smtp_provider_needs_credentials(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(mailer.ProviderNotConfigured):
        mailer.SmtpProvider()
