import os
os.environ["DB_PATH"] = "/tmp/test_bg.db"
os.environ.pop("ANTHROPIC_API_KEY", None)
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)

BAD = "Brightly is the WORLD'S BEST tool and we guarantee results! It's better than Acme. Try our 30-day free trial."
GOOD = "Brightly helps teams plan their week. Start with a 14-day free trial."

def test_bad_content_flagged():
    r = c.post("/api/review", json={"text": BAD}).json()
    cats = {f["category"] for f in r["findings"]}
    assert {"Claims", "Competitors", "Facts"} <= cats
    assert r["findings"][0]["severity"] == "critical"

def test_good_content_clean():
    r = c.post("/api/review", json={"text": GOOD}).json()
    assert r["findings"] == []

def test_decision_flow():
    r = c.post("/api/review", json={"text": BAD}).json()
    fid = r["findings"][0]["id"]
    assert c.post(f"/api/reviews/{r['review_id']}/findings/{fid}/decision", json={"status": "approved"}).status_code == 200
    got = c.get(f"/api/reviews/{r['review_id']}").json()
    assert any(f["status"] == "approved" for f in got["findings"])
    assert c.get("/api/health").json()["status"] == "ok"
