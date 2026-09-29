"""Live end-to-end check against a RUNNING DealPilot server (uses your real Hindsight + Groq keys).

    python scripts/smoke_test.py                # http://127.0.0.1:8000
    python scripts/smoke_test.py http://host:port
Standard library only.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
DEAL = "acme-corp"
failures = 0


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, ok, detail=""):
    global failures
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ->  {detail}" if detail else ""))


s, d = call("GET", "/health");                 check("GET /health", s == 200 and d.get("status") == "ok")
s, d = call("GET", "/api/status");             check("GET /api/status", s == 200, str(d))
s, d = call("POST", f"/api/deals/{DEAL}/interactions", {"content": "   "})
check("empty interaction rejected", s == 400, d.get("error", ""))
s, d = call("POST", f"/api/deals/{DEAL}/load-demo")
check("POST load-demo (retain x3)", s in (200, 409), d.get("error") or f"loaded={d.get('loaded')}")
s, d = call("POST", f"/api/deals/{DEAL}/interactions", {"content": "Acme's VP Sales wants a pilot for 20 seats before Q4."})
check("POST interactions (retain)", s == 201, d.get("error") or f"count={d.get('interaction_count')}")

print("Waiting 20s for Hindsight to finish extracting memories...")
time.sleep(20)

s, d = call("GET", f"/api/deals/{DEAL}/memories")
check("GET memories (recall)", s == 200 and d.get("recalled", 0) > 0, d.get("error") or f"recalled={d.get('recalled')}")
s, d = call("POST", f"/api/deals/{DEAL}/ask", {"question": "What are Acme's biggest objections?"})
check("POST ask (recall + Groq)", s == 200 and d.get("memory_count", 0) > 0, d.get("error") or d.get("answer", "")[:120])
s, d = call("POST", f"/api/deals/{DEAL}/ask", {"question": "Prepare me for my next Acme meeting."})
brief = d.get("brief") or {}
check("meeting brief JSON", s == 200 and bool(brief.get("recommended_next_action")), d.get("error") or json.dumps(brief)[:160])
s, no = call("POST", f"/api/deals/{DEAL}/ask-without-memory", {"question": "Tell me about Acme Corp and prepare me for the next meeting."})
check("POST ask-without-memory", s == 200 and bool(no.get("answer")), no.get("error") or no.get("answer", "")[:120])
s, yes = call("POST", f"/api/deals/{DEAL}/ask", {"question": "Tell me about Acme Corp and prepare me for the next meeting.", "brief": False})
check("with-memory answer differs from no-memory", s == 200 and yes.get("answer") != no.get("answer"))
print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
sys.exit(1 if failures else 0)
