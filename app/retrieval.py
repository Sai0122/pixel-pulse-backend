"""Brand-rule retrieval. Uses Chroma if installed, else a lightweight TF-IDF-style fallback."""
import json, math, re
from pathlib import Path

GUIDE = json.loads((Path(__file__).parent.parent / "data" / "brand_guidelines.json").read_text())
DOCS = [{"id": r["id"], "kind": "rule", "text": f'{r["category"]}: {r["text"]}'} for r in GUIDE["rules"]] + \
       [{"id": f["id"], "kind": "fact", "text": f["text"]} for f in GUIDE["facts"]]

def _tok(s): return re.findall(r"[a-z0-9]+", s.lower())

_chroma = None
try:
    import chromadb
    _client = chromadb.Client()
    _chroma = _client.get_or_create_collection("brand")
    if _chroma.count() == 0:
        _chroma.add(ids=[d["id"] for d in DOCS], documents=[d["text"] for d in DOCS],
                    metadatas=[{"kind": d["kind"]} for d in DOCS])
except Exception:
    _chroma = None

def backend_name(): return "chroma" if _chroma else "tfidf"

def retrieve(text, k=6):
    if _chroma:
        res = _chroma.query(query_texts=[text[:2000]], n_results=min(k, len(DOCS)))
        ids = res["ids"][0]
        return [d for d in DOCS if d["id"] in ids]
    q = _tok(text)
    df = {}
    for d in DOCS:
        for t in set(_tok(d["text"])): df[t] = df.get(t, 0) + 1
    def score(d):
        toks = _tok(d["text"])
        return sum((toks.count(t) / (len(toks) or 1)) * math.log(1 + len(DOCS) / df[t])
                   for t in set(q) if t in df)
    ranked = sorted(DOCS, key=score, reverse=True)
    return ranked[:k]
