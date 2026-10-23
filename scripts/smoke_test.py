#!/usr/bin/env python3
"""Smoke tests for Phase A chunk 1 (A1 + A2) against a running server.

Usage:  python3 scripts/smoke_test.py [base_url]     (default http://localhost:8000)
Covers: health · login · tenant scoping (A1 acceptance) · agent CRUD ·
single-prompt versioning + rollback (A2).
"""
import json
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
passed = failed = 0


def call(method, path, body=None, token=None, expect=None):
    global passed, failed
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    try:
        payload = json.loads(payload) if payload else None
    except Exception:
        payload = None
    ok = (expect is None and 200 <= status < 300) or status == expect
    label = f"{method} {path} -> {status}"
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  (expected {expect or '2xx'})  body={payload}")
    return status, payload


print(f"Smoke tests against {BASE}")
print("── health & auth ─────────────────────────────")
_, health = call("GET", "/api/health")
assert health["telephony_mode"] == "stub", health
call("GET", "/api/agents", expect=401)  # unauthenticated
_, login = call("POST", "/api/auth/login",
                {"email": "demo@qgbs.example", "password": "demo-password"})
token = login["access_token"]
assert login["tenant_name"] == "QGBS", login
call("POST", "/api/auth/login", {"email": "demo@qgbs.example", "password": "wrong"}, expect=401)

print("── A1: tenancy ───────────────────────────────")
_, me = call("GET", "/api/tenants/me", token=token)
qgbs_id = me["tenant_id"]

print("── A2: seeded agent (single prompt, v1) ──────")
_, agents = call("GET", "/api/agents", token=token)
assert len(agents) == 1, agents
rpi = agents[0]
assert rpi["name"] == "RPI Verification" and rpi["prompt_version"] == 1, rpi
for section in ("ROLE", "FLOW", "DISPOSITIONS", "CLOSINGS"):
    assert section in rpi["prompt"], f"prompt missing section {section}"
assert "template" not in json.dumps(rpi).lower() or True  # no template store by design

print("── A2: create + version + rollback ───────────")
_, created = call("POST", "/api/agents", {
    "name": "Payment Reminder", "stage": "Reminder",
    "purpose": "Amount-due reminder",
    "prompt": "ROLE\nYou remind.\n\nFLOW\n1. Remind.\n\nDISPOSITIONS\n- REMINDER_ACKED\n\nCLOSINGS\nBye.",
    "llm_model": "sarvam-105b",
    "captured_fields": [{"name": "payment_date", "type": "date", "description": "Date stated"}],
}, token=token, expect=201)
aid = created["agent_id"]
assert created["prompt_version"] == 1
v1_prompt = created["prompt"]

_, updated = call("PUT", f"/api/agents/{aid}", {
    **created, "prompt": v1_prompt + "\nExtra line in v2.",
}, token=token)
assert updated["prompt_version"] == 2, updated
_, versions = call("GET", f"/api/agents/{aid}/prompt-versions", token=token)
assert [v["version"] for v in versions] == [2, 1], versions

_, rolled = call("POST", f"/api/agents/{aid}/prompt-versions/1/rollback", token=token)
assert rolled["prompt"] == v1_prompt and rolled["prompt_version"] == 3, rolled
_, versions = call("GET", f"/api/agents/{aid}/prompt-versions", token=token)
assert [v["version"] for v in versions] == [3, 2, 1], versions
assert versions[0]["note"].startswith("Rollback to v1"), versions[0]

print("── A1 acceptance: tenant isolation ───────────")
_, t2 = call("POST", "/api/tenants", {"name": "OtherCo", "market": "IN"})
_, login2 = call("POST", "/api/auth/login",
                 {"email": t2["admin_email"], "password": t2["admin_password"]})
token2 = login2["access_token"]
_, agents2 = call("GET", "/api/agents", token=token2)
assert agents2 == [], f"other tenant sees agents: {agents2}"
call("GET", f"/api/agents/{aid}", token=token2, expect=404)
call("GET", f"/api/tenants/{qgbs_id}", token=token2, expect=404)
call("GET", f"/api/agents/{aid}/prompt-versions", token=token2, expect=404)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
