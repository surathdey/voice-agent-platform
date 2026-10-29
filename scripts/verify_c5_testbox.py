#!/usr/bin/env python3
"""Verify C5 on a deployed tenant box: role gates + invite flow.

Usage:  VAP_EMAIL=<box seed email> VAP_PASSWORD=<box seed password>
        VAP_SUPER_EMAIL=<super email> VAP_SUPER_PASSWORD=<super password>
        python3 scripts/verify_c5_testbox.py [base_url]
Default base_url http://127.0.0.1:8000 (in-container backend).
The box must have been booted with SUPER_ADMIN_* set and
INVITE_DEV_OTP=true (the dev gate returns the OTP in the invite
response, so this reduced check can complete invites without a
wired Email/SMS provider). The FULL role matrix runs locally
(scripts/test_roles.py); this is the deployed-box smoke of it.
Credentials come only from the environment.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
EMAIL = os.environ.get("VAP_EMAIL", "demo@qgbs.example")
PASSWORD = os.environ.get("VAP_PASSWORD", "")
SUPER_EMAIL = os.environ.get("VAP_SUPER_EMAIL", "")
SUPER_PASSWORD = os.environ.get("VAP_SUPER_PASSWORD", "")
if not PASSWORD or not SUPER_EMAIL or not SUPER_PASSWORD:
    sys.exit("VAP_PASSWORD, VAP_SUPER_EMAIL and VAP_SUPER_PASSWORD are required "
             "(pass them via docker compose exec -e)")

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    payload = json.loads(payload) if payload else None
    if expect is not None:
        check(f"{method} {path} -> {status} (expected {expect})", status == expect,
              f"body={str(payload)[:200]}")
    return status, payload


def login(email, password):
    status, payload = call("POST", "/api/auth/login",
                           {"email": email, "password": password}, expect=200)
    assert status == 200, f"login failed for {email}: {payload}"
    return payload["access_token"], payload["user"]


TA, ta_user = login(EMAIL, PASSWORD)
check("box seed user is tenant_admin after C5 migration",
      ta_user["role"] == "tenant_admin", f"got={ta_user['role']}")
SUPER, super_user = login(SUPER_EMAIL, SUPER_PASSWORD)
check("super login is super_admin", super_user["role"] == "super_admin")

print("── Tenant Admin gates ──")
call("POST", "/api/catalog/agents", {"slug": "x", "name": "x"}, token=TA, expect=403)
_, agents = call("GET", "/api/agents", token=TA, expect=200)
if agents:
    call("PUT", f"/api/agents/{agents[0]['agent_id']}",
         dict(agents[0], prompt="edited"), token=TA, expect=403)
_, camp = call("POST", "/api/campaigns", {
    "agent_id": agents[0]["agent_id"], "name": "C5 verify (deleted after)",
    "product": "vehicle_loan", "provider_mode": "managed",
    "schedule": {"calling_days": "Every day", "window_start": "00:00",
                 "window_end": "23:59"},
}, token=TA, expect=201)
call("DELETE", f"/api/campaigns/{camp['campaign_id']}", token=TA, expect=200)

print("── Invite flow on the box ──")
_, inv = call("POST", "/api/users/invites",
              {"email": "c5caller@test.example", "name": "C5 Caller",
               "role": "caller"}, token=TA, expect=201)
assert inv.get("dev_otp"), "dev_otp missing — box needs INVITE_DEV_OTP=true"
_, setup = call("POST", "/api/auth/setup",
                {"invite_id": inv["invite_id"],
                 "token": inv["setup_url"].split("token=")[1],
                 "otp": inv["dev_otp"], "password": "c5-caller-pass"}, expect=200)
CALLER = setup["access_token"]
check("invited Caller role is caller", setup["user"]["role"] == "caller")
_, agents_caller = call("GET", "/api/agents", token=CALLER, expect=200)
check("Caller agent list has prompts redacted",
      all(a["prompt"] == "" for a in agents_caller))
call("GET", "/api/catalog/agents", token=CALLER, expect=403)
call("POST", "/api/campaigns", {"agent_id": "x", "name": "nope"},
     token=CALLER, expect=403)
call("GET", "/api/users", token=CALLER, expect=403)

print("── Team Leader path on the box ──")
_, me_tenant = call("GET", "/api/tenants/me", token=TA, expect=200)
_, inv_tl = call("POST", "/api/users/invites",
                 {"email": "c5lead@test.example", "name": "C5 Lead",
                  "role": "team_leader",
                  "tenant_id": me_tenant["tenant_id"]},
                 token=SUPER, expect=201)
_, setup_tl = call("POST", "/api/auth/setup",
                   {"invite_id": inv_tl["invite_id"],
                    "token": inv_tl["setup_url"].split("token=")[1],
                    "otp": inv_tl["dev_otp"], "password": "c5-lead-pass"},
                   expect=200)
TL = setup_tl["access_token"]
check("invited Team Leader role is team_leader",
      setup_tl["user"]["role"] == "team_leader")
_, team = call("POST", "/api/teams",
               {"name": "C5 Team", "leader_user_id": setup_tl["user"]["user_id"]},
               token=TA, expect=201)
_, tl_teams = call("GET", "/api/teams", token=TL, expect=200)
check("TL lists the team they lead",
      len(tl_teams) == 1 and tl_teams[0]["team_id"] == team["team_id"])

print("── Super Admin on the box ──")
_, cat = call("POST", "/api/catalog/agents",
              {"slug": "c5-test-agent", "name": "C5 Test Agent",
               "stage": "Verification"}, token=SUPER, expect=201)
check("super catalog write works (throwaway slug, disposable box)",
      cat["slug"] == "c5-test-agent")
call("GET", "/api/campaigns", token=SUPER, expect=403)
call("GET", "/api/queue", token=SUPER, expect=403)

print(f"\nC5 TESTBOX RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
