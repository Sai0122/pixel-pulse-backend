import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal, Optional
from . import db, rules, llm, retrieval

app = FastAPI(title="BrandGuard API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
db.init()
ORDER = {"critical": 0, "warning": 1, "suggestion": 2}

class ReviewIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)

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

@app.get("/api/analytics")
def analytics():
    return db.get_analytics()

@app.get("/api/health")
def health():
    return {"status": "ok", "llm": bool(llm.API_KEY()), "retrieval": retrieval.backend_name()}

@app.get("/api/guidelines")
def guidelines():
    g = retrieval.GUIDE
    return {"brand": g["brand"], "rules": [{k: r[k] for k in ("id", "category", "severity", "text")} for r in g["rules"]],
            "facts": [{"id": f["id"], "text": f["text"]} for f in g["facts"]]}

@app.post("/api/review")
def review(body: ReviewIn):
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
def reviews():
    return db.list_reviews()

@app.get("/api/reviews/{rid}")
def get_review(rid: str):
    r = db.get_review(rid)
    if not r: raise HTTPException(404, "Review not found")
    return r

@app.post("/api/reviews/{rid}/findings/{fid}/decision")
def decide(rid: str, fid: str, d: DecisionIn):
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
        d.actor
    ):
        raise HTTPException(404, "Finding not found")

    return {"ok": True}
@app.post("/api/reviews/{rid}/submit")
def submit_approval(rid: str, body: SubmitApprovalIn):
    if not db.submit_for_approval(
        rid, body.submitted_by, body.manager
    ):
        raise HTTPException(404, "Review not found")

    return {"ok": True, "status": "pending_approval"}


@app.post("/api/reviews/{rid}/approve")
def approve_review(rid: str, body: ApprovalActionIn):
    if not db.update_approval(
        rid, "approved", body.actor, body.comment
    ):
        raise HTTPException(404, "Approval workflow not found")

    return {"ok": True, "status": "approved"}


@app.post("/api/reviews/{rid}/request-changes")
def request_changes(rid: str, body: ApprovalActionIn):
    if not body.comment:
        raise HTTPException(422, "A comment is required")

    if not db.update_approval(
        rid, "changes_requested", body.actor, body.comment
    ):
        raise HTTPException(404, "Approval workflow not found")

    return {"ok": True, "status": "changes_requested"}


@app.get("/api/reviews/{rid}/workflow")
def get_review_workflow(rid: str):
    workflow = db.get_workflow(rid)

    if not workflow:
        raise HTTPException(404, "Workflow not found")

    return workflow
# --------------------------------------------------
# COMMENTS API
# --------------------------------------------------

@app.post("/api/reviews/{rid}/comments")
def create_comment(rid: str, body: CommentIn):
    message = body.message.strip()

    if not message:
        raise HTTPException(422, "Comment cannot be empty")

    comment_id = db.add_comment(rid, body.actor, message)

    if not comment_id:
        raise HTTPException(404, "Review not found")

    db.add_revision(
        rid, body.actor, "comment_added", message
    )

    return {"ok": True, "comment_id": comment_id}


@app.get("/api/reviews/{rid}/comments")
def list_comments(rid: str):
    if not db.get_review(rid):
        raise HTTPException(404, "Review not found")

    return db.get_comments(rid)


# --------------------------------------------------
# REVISION HISTORY API
# --------------------------------------------------

@app.get("/api/reviews/{rid}/revisions")
def list_revisions(rid: str):
    if not db.get_review(rid):
        raise HTTPException(404, "Review not found")

    return db.get_revisions(rid)