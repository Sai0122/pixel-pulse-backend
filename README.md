# BrandGuard API (Backend — Dipankar Mohanty)

Run: `pip install -r requirements.txt && uvicorn app.main:app --reload`  (docs at /docs)
Test: `pytest`
Deploy: push to GitHub → Render "New Web Service" (uses render.yaml/Dockerfile). Set `ANTHROPIC_API_KEY`.

## Endpoints (for Karishma's frontend)
- `GET  /api/health`
- `GET  /api/guidelines` — rules + approved facts
- `POST /api/review` `{ "text": "..." }` → `{review_id, mode, summary, findings[]}`
  finding: `{id, rule_id, category, severity (critical|warning|suggestion), evidence, explanation, suggested_fix, status}`
- `GET  /api/reviews`, `GET /api/reviews/{id}`
- `POST /api/reviews/{id}/findings/{fid}/decision` `{ "status": "approved|rejected|edited", "edited_fix": "..." }`

Works without an API key (rules-only mode); with a key it adds LLM review. Edit `data/brand_guidelines.json` to change brand rules (Priyadarshini's severity list goes here).
