import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal, Optional
from . import db, rules, llm, retrieval, auth
import time
import uuid
from fastapi import Depends

app = FastAPI(title="BrandGuard API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
db.init()
ORDER = {"critical": 0, "warning": 1, "suggestion": 2}

class ReviewIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=128)


class LoginIn(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)


class RoleUpdateIn(BaseModel):
    role: Literal["Creator", "Reviewer", "Manager", "Admin"]

class DecisionIn(BaseModel):
    status: Literal["approved", "rejected", "edited"]
    edited_fix: Optional[str] = None
    actor: str = "User"
class SubmitApprovalIn(BaseModel):
    submitted_by: str = "Creator"
    manager: Optional[str] = "Manager"


class ApprovalActionIn(BaseModel):
    actor: str = "Manager"
    comment: Optional[str] = None
class CommentIn(BaseModel):
    actor: str = "Creator"
    message: str = Field(min_length=1, max_length=5000)

def _dedupe(findings):
    seen, out = set(), []
    for f in findings:
        key = (f["evidence"].strip().lower(), f["category"])
        if key not in seen:
            seen.add(key); out.append(f)
    return out

@app.post("/api/auth/register")
def register(body: RegisterIn):
    name = body.name.strip()
    email = body.email.strip().lower()

    auth.validate_registration(name, email, body.password)

    user_id = uuid.uuid4().hex

    try:
        with db.conn() as c:
            c.execute(
                """
                INSERT INTO users
                (id, name, email, password_hash, role, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    email,
                    auth.hash_password(body.password),
                    "Creator",
                    1,
                    time.time(),
                ),
            )
    except Exception:
        raise HTTPException(409, "An account with this email may already exist")

    return {"message": "Account created successfully. Please log in."}
@app.post("/api/auth/login")
def login(body: LoginIn):
    email = body.email.strip().lower()

    with db.conn() as c:
        user = c.execute(
            """
            SELECT id, name, email, password_hash, role, is_active
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()

    if not user or not auth.verify_password(
        body.password, user["password_hash"]
    ):
        raise HTTPException(401, "Incorrect email or password")

    if not user["is_active"]:
        raise HTTPException(403, "Your account is inactive")

    user_data = dict(user)

    token = auth.create_access_token(user_data)

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user_data["id"],
            "name": user_data["name"],
            "email": user_data["email"],
            "role": user_data["role"],
        },
    }
@app.get("/api/auth/me")
def get_me(user=Depends(auth.get_current_user)):
    return user

@app.get("/api/auth/users")
def list_users(admin=Depends(auth.require_roles("Admin"))):
    with db.conn() as c:
        users = c.execute(
            """
            SELECT id, name, email, role, is_active, created_at
            FROM users
            ORDER BY created_at DESC
            """
        ).fetchall()

    return [dict(user) for user in users]


@app.patch("/api/auth/users/{user_id}/role")
def update_user_role(
    user_id: str,
    body: RoleUpdateIn,
    admin=Depends(auth.require_roles("Admin")),
):
    with db.conn() as c:
        target = c.execute(
            "SELECT id, role FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if not target:
            raise HTTPException(404, "User not found")

        if target["role"] == "Admin" and body.role != "Admin":
            admin_count = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = ? AND is_active = ?",
                ("Admin", 1),
            ).fetchone()["n"]

            if admin_count <= 1:
                raise HTTPException(
                    400,
                    "Cannot remove the last active Admin",
                )

        c.execute(
            "UPDATE users SET role = ? WHERE id = ?",
            (body.role, user_id),
        )

    return {"message": "User role updated successfully", "role": body.role}

@app.get("/api/analytics")
def analytics(user=Depends(auth.get_current_user)):
    return db.get_analytics()

@app.get("/api/demo/analytics")
def demo_analytics():
    return db.get_demo_analytics()
@app.get("/api/health")
def health():
    return {"status": "ok", "llm": bool(llm.API_KEY()), "retrieval": retrieval.backend_name()}

@app.get("/api/guidelines")
def guidelines(user=Depends(auth.get_current_user)):
    g = retrieval.GUIDE
    return {"brand": g["brand"], "rules": [{k: r[k] for k in ("id", "category", "severity", "text")} for r in g["rules"]],
            "facts": [{"id": f["id"], "text": f["text"]} for f in g["facts"]]}
@app.post("/api/demo/review")
def public_demo_review(body: ReviewIn):
    text = body.text.strip()

    findings = rules.rule_findings(text) + rules.fact_findings(text)
    ctx = retrieval.retrieve(text)
    ai = llm.llm_review(text, ctx)

    mode = "llm+rules" if ai else "rules-only"
    summary = ""

    if ai:
        findings += ai.get("findings", [])
        summary = ai.get("summary", "")

    findings = sorted(_dedupe(findings), key=lambda f: ORDER[f["severity"]])

    for f in findings:
        f.setdefault("rule_id", "LLM")

    if not summary:
        c = {s: sum(1 for f in findings if f["severity"] == s) for s in ORDER}
        summary = (
            f'{len(findings)} issue(s): {c["critical"]} critical, '
            f'{c["warning"]} warning, {c["suggestion"]} suggestion.'
            if findings else "No brand issues found."
        )

    rid = db.save_review(text, findings, summary, mode, is_demo=True)

    return {
        "review_id": rid,
        "mode": mode,
        "summary": summary,
        "findings": findings
    }
@app.post("/api/review")
def review(body: ReviewIn, user=Depends(auth.get_current_user)):
    text = body.text.strip()
    findings = rules.rule_findings(text) + rules.fact_findings(text)
    ctx = retrieval.retrieve(text)
    ai = llm.llm_review(text, ctx)
    mode = "llm+rules" if ai else "rules-only"
    summary = ""
    if ai:
        findings += ai.get("findings", [])
        summary = ai.get("summary", "")
    findings = sorted(_dedupe(findings), key=lambda f: ORDER[f["severity"]])
    for f in findings:
        f.setdefault("rule_id", "LLM")
    if not summary:
        c = {s: sum(1 for f in findings if f["severity"] == s) for s in ORDER}
        summary = (f'{len(findings)} issue(s): {c["critical"]} critical, {c["warning"]} warning, '
                   f'{c["suggestion"]} suggestion.' if findings else "No brand issues found.")
    rid = db.save_review(text, findings, summary, mode)
    return {"review_id": rid, "mode": mode, "summary": summary, "findings": findings}

@app.get("/api/reviews")
def reviews(user=Depends(auth.get_current_user)):
    return db.list_reviews()

@app.get("/api/reviews/{rid}")
def get_review(rid: str, user=Depends(auth.get_current_user)):
    r = db.get_review(rid)
    if not r: raise HTTPException(404, "Review not found")
    return r

@app.post("/api/demo/reviews/{rid}/findings/{fid}/decision")
def demo_finding_decision(rid: str, fid: str, d: DecisionIn):
    if not rid.startswith("demo_"):
        raise HTTPException(403, "Demo reviews only")

    if d.status == "edited" and not d.edited_fix:
        raise HTTPException(422, "edited_fix required")

    if not db.set_decision(
        rid,
        fid,
        d.status,
        d.edited_fix,
        "Demo Creator"
    ):
        raise HTTPException(404, "Finding not found")

    return {"ok": True}
@app.post("/api/reviews/{rid}/findings/{fid}/decision")
def decide(rid: str, fid: str, d: DecisionIn, user=Depends(auth.get_current_user)):
    if d.status == "edited" and not d.edited_fix:
        raise HTTPException(
            422,
            "edited_fix required when status is 'edited'"
        )

    if not db.set_decision(
        rid,
        fid,
        d.status,
        d.edited_fix,
        user["name"]
    ):
        raise HTTPException(404, "Finding not found")

    return {"ok": True}
@app.post("/api/demo/reviews/{rid}/submit")
def demo_submit_approval(rid: str):
    if not rid.startswith("demo_"):
        raise HTTPException(403, "Demo reviews only")

    if not db.submit_for_approval(rid, "Demo Creator", "Demo Manager"):
        raise HTTPException(404, "Demo review not found")

    return {"ok": True, "status": "pending_approval"}


@app.post("/api/demo/reviews/{rid}/approve")
def demo_approve_review(rid: str, body: ApprovalActionIn):
    if not rid.startswith("demo_"):
        raise HTTPException(403, "Demo reviews only")

    if not db.update_approval(
        rid, "approved", "Demo Manager", body.comment
    ):
        raise HTTPException(404, "Demo approval workflow not found")

    return {"ok": True, "status": "approved"}


@app.post("/api/demo/reviews/{rid}/request-changes")
def demo_request_changes(rid: str, body: ApprovalActionIn):
    if not rid.startswith("demo_"):
        raise HTTPException(403, "Demo reviews only")

    if not body.comment or not body.comment.strip():
        raise HTTPException(422, "A comment is required")

    if not db.update_approval(
        rid, "changes_requested", "Demo Manager", body.comment
    ):
        raise HTTPException(404, "Demo approval workflow not found")

    return {"ok": True, "status": "changes_requested"}
@app.post("/api/reviews/{rid}/submit")
def submit_approval(rid: str, body: SubmitApprovalIn, user=Depends(auth.require_roles("Creator"))):
    if not db.submit_for_approval(
        rid, user["name"], body.manager
    ):
        raise HTTPException(404, "Review not found")

    return {"ok": True, "status": "pending_approval"}


@app.post("/api/reviews/{rid}/approve")
def approve_review(rid: str, body: ApprovalActionIn, user=Depends(auth.require_roles("Manager", "Admin"))):
    if not db.update_approval(
        rid, "approved", user["name"], body.comment
    ):
        raise HTTPException(404, "Approval workflow not found")

    return {"ok": True, "status": "approved"}


@app.post("/api/reviews/{rid}/request-changes")
def request_changes(rid: str, body: ApprovalActionIn, user=Depends(auth.require_roles("Manager", "Admin"))):
    if not body.comment:
        raise HTTPException(422, "A comment is required")

    if not db.update_approval(
        rid, "changes_requested", user["name"], body.comment
    ):
        raise HTTPException(404, "Approval workflow not found")

    return {"ok": True, "status": "changes_requested"}


@app.get("/api/reviews/{rid}/workflow")
def get_review_workflow(rid: str, user=Depends(auth.get_current_user)):
    workflow = db.get_workflow(rid)

    if not workflow:
        raise HTTPException(404, "Workflow not found")

    return workflow
# --------------------------------------------------
# COMMENTS API
# --------------------------------------------------

@app.post("/api/reviews/{rid}/comments")
def create_comment(rid: str, body: CommentIn, user=Depends(auth.get_current_user)):
    message = body.message.strip()

    if not message:
        raise HTTPException(422, "Comment cannot be empty")

    comment_id = db.add_comment(rid, user["name"], message)

    if not comment_id:
        raise HTTPException(404, "Review not found")

    db.add_revision(
        rid, user["name"], "comment_added", message
    )

    return {"ok": True, "comment_id": comment_id}


@app.get("/api/reviews/{rid}/comments")
def list_comments(rid: str, user=Depends(auth.get_current_user)):
    if not db.get_review(rid):
        raise HTTPException(404, "Review not found")

    return db.get_comments(rid)


# --------------------------------------------------
# REVISION HISTORY API
# --------------------------------------------------

@app.get("/api/reviews/{rid}/revisions")
def list_revisions(rid: str, user=Depends(auth.get_current_user)):
    if not db.get_review(rid):
        raise HTTPException(404, "Review not found")

    return db.get_revisions(rid)
