
import os
import uuid

os.environ.pop("DATABASE_URL", None)
os.environ["DB_PATH"] = "/tmp/test_bg_auth.db"
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-for-local-tests")
os.environ.pop("ANTHROPIC_API_KEY", None)

from fastapi.testclient import TestClient
from app.main import app
from app import db, auth

c = TestClient(app)

BAD = "Brightly is the WORLD'S BEST tool and we guarantee results! It's better than Acme. Try our 30-day free trial."
GOOD = "Brightly helps teams plan their week. Start with a 14-day free trial."


def create_test_user(email, role="Creator"):
    user_id = uuid.uuid4().hex
    with db.conn() as connection:
        connection.execute(
            """
            INSERT INTO users
            (id, name, email, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                "Test User",
                email,
                auth.hash_password("TestPassword123!"),
                role,
                1,
                1000000000,
            ),
        )
    return user_id


def login_as(email):
    response = c.post(
        "/api/auth/login",
        json={"email": email, "password": "TestPassword123!"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_authentication_required():
    response = c.post("/api/review", json={"text": GOOD})
    assert response.status_code == 401


def test_registration_and_login():
    email = f"{uuid.uuid4().hex}@example.com"

    response = c.post(
        "/api/auth/register",
        json={
            "name": "New Creator",
            "email": email,
            "password": "StrongPassword123!",
        },
    )
    assert response.status_code == 200, response.text

    login_response = c.post(
        "/api/auth/login",
        json={"email": email, "password": "StrongPassword123!"},
    )
    assert login_response.status_code == 200, login_response.text
    assert login_response.json()["user"]["role"] == "Creator"


def test_bad_content_flagged():
    email = f"{uuid.uuid4().hex}@example.com"
    create_test_user(email)
    headers = login_as(email)

    r = c.post("/api/review", json={"text": BAD}, headers=headers).json()

    cats = {f["category"] for f in r["findings"]}
    assert {"Claims", "Competitors", "Facts"} <= cats
    assert r["findings"][0]["severity"] == "critical"


def test_good_content_clean():
    email = f"{uuid.uuid4().hex}@example.com"
    create_test_user(email)
    headers = login_as(email)

    r = c.post("/api/review", json={"text": GOOD}, headers=headers).json()
    assert r["findings"] == []


def test_decision_flow():
    email = f"{uuid.uuid4().hex}@example.com"
    create_test_user(email)
    headers = login_as(email)

    r = c.post("/api/review", json={"text": BAD}, headers=headers).json()
    fid = r["findings"][0]["id"]

    decision = c.post(
        f"/api/reviews/{r['review_id']}/findings/{fid}/decision",
        json={"status": "approved"},
        headers=headers,
    )
    assert decision.status_code == 200, decision.text

    got = c.get(
        f"/api/reviews/{r['review_id']}",
        headers=headers,
    ).json()

    assert any(f["status"] == "approved" for f in got["findings"])
    assert c.get("/api/health").json()["status"] == "ok"


def test_creator_cannot_approve():
    email = f"{uuid.uuid4().hex}@example.com"
    create_test_user(email, "Creator")
    headers = login_as(email)

    response = c.post(
        "/api/reviews/nonexistent/approve",
        json={"actor": "Fake Manager", "comment": "Approved"},
        headers=headers,
    )
    assert response.status_code == 403


def test_manager_can_approve_endpoint():
    email = f"{uuid.uuid4().hex}@example.com"
    create_test_user(email, "Manager")
    headers = login_as(email)

    response = c.post(
        "/api/reviews/nonexistent/approve",
        json={"actor": "Fake Manager", "comment": "Approved"},
        headers=headers,
    )
    # Authorization succeeds; the review does not exist.
    assert response.status_code == 404

def test_admin_can_list_users():
    email = f"{uuid.uuid4().hex}@example.com"
    create_test_user(email, "Admin")
    headers = login_as(email)

    response = c.get("/api/auth/users", headers=headers)

    assert response.status_code == 200
    assert any(user["email"] == email for user in response.json())


def test_creator_cannot_list_users():
    email = f"{uuid.uuid4().hex}@example.com"
    create_test_user(email, "Creator")
    headers = login_as(email)

    response = c.get("/api/auth/users", headers=headers)

    assert response.status_code == 403


def test_admin_can_change_user_role():
    admin_email = f"{uuid.uuid4().hex}@example.com"
    creator_email = f"{uuid.uuid4().hex}@example.com"

    create_test_user(admin_email, "Admin")
    creator_id = create_test_user(creator_email, "Creator")

    headers = login_as(admin_email)

    response = c.patch(
        f"/api/auth/users/{creator_id}/role",
        json={"role": "Reviewer"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["role"] == "Reviewer"


def test_creator_cannot_change_user_role():
    creator_email = f"{uuid.uuid4().hex}@example.com"
    target_email = f"{uuid.uuid4().hex}@example.com"

    create_test_user(creator_email, "Creator")
    target_id = create_test_user(target_email, "Creator")

    headers = login_as(creator_email)

    response = c.patch(
        f"/api/auth/users/{target_id}/role",
        json={"role": "Admin"},
        headers=headers,
    )

    assert response.status_code == 403



def test_last_admin_cannot_be_demoted():
    # Reset test roles so this test has exactly one Admin.
    with db.conn() as connection:
        connection.execute(
            "UPDATE users SET role = ? WHERE role = ?",
            ("Creator", "Admin"),
        )

    admin_email = f"{uuid.uuid4().hex}@example.com"
    admin_id = create_test_user(admin_email, "Admin")
    headers = login_as(admin_email)

    response = c.patch(
        f"/api/auth/users/{admin_id}/role",
        json={"role": "Creator"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "last active Admin" in response.json()["detail"]

