#!/usr/bin/env python3
"""Epic C, story C5 acceptance tests: roles + invites + allocation + audit.

Usage:  python3 scripts/test_roles.py [base_url] [db_path]
Defaults: base_url http://localhost:8000, db_path /tmp/c5.db.
Run against a FRESH database booted with:
  SUPER_ADMIN_EMAIL=super@platform.example
  SUPER_ADMIN_PASSWORD=super-password
  INVITE_DEV_OTP=true
  TELEPHONY_MODE=stub
(the runner ~/workspace/run_c5_test.sh does this).

Covers (plan "Epic C Plan", C5 acceptance + decided rules):
- Role matrix: each role can do only its own list (catalog writes,
  prompt editor, tenant creation = Super Admin; campaigns, uploads,
  exports, tenant settings = Tenant Admin; allocation = Team Leader
  only — Tenant Admin CANNOT allocate; Caller sees only own cases).
- Super Admin sees NO tenant customer data (campaigns/calls/queue/
  export all refused).
- Invite flow: Email OTP first, single-use expiring setup link,
  user sets own password; wrong OTP refused; setup re-use refused;
  SMS-channel invite works the same way.
- Allocation: TL allocates only to their own team's Callers;
  unallocated pool visible to TL; Caller sees only own leads/calls.
- Upload caller column: assigns by name/email, reports unresolved.
- Queue approvals: settlement items TL-only; other items Tenant
  Admin or TL; Caller never; expired approval -> 410; every
  approval and disposition update carries who + when.
"""
import json
import sqlite3
import sys
import urllib.request
import urllib.error
import urllib.parse

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
DB = sys.argv[2] if len(sys.argv) > 2 else "/tmp/c5.db"
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
        with urllib.request.urlopen(req, timeout=15) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    try:
        payload = json.loads(payload) if payload else None
    except Exception:
        payload = None
    if expect is not None:
        check(f"{method} {path} -> {status} (expected {expect})", status == expect,
              f"body={str(payload)[:200]}")
    return status, payload


def call_upload(path, csv_text, token, mapping, expect=None):
    boundary = "----c5test"
    fields = f"--{boundary}\r\nContent-Disposition: form-data; name=\"mapping\"\r\n\r\n{json.dumps(mapping)}\r\n".encode()
    fpart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"leads.csv\"\r\n"
             "Content-Type: text/csv\r\n\r\n").encode() + csv_text.encode() + b"\r\n"
    body = fields + fpart + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + path, method="POST", data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    try:
        payload = json.loads(payload) if payload else None
    except Exception:
        payload = None
    if expect is not None:
        check(f"POST {path} (csv) -> {status} (expected {expect})", status == expect,
              f"body={str(payload)[:200]}")
    return status, payload


def login(email, password):
    status, payload = call("POST", "/api/auth/login", {"email": email, "password": password},
                           expect=200)
    return payload["access_token"]


def invite_and_setup(inviter_token, email, name, role, team_id=None, channel="email",
                     password="caller-password", tenant_id=None):
    body = {"email": email, "name": name, "role": role,
            "team_id": team_id, "channel": channel}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    status, inv = call("POST", "/api/users/invites", body,
                       token=inviter_token, expect=201)
    assert status == 201, f"invite failed: {inv}"
    otp = inv.get("dev_otp")
    assert otp, "dev_otp missing — boot with INVITE_DEV_OTP=true"
    token = inv["setup_url"].split("token=")[1]
    status, setup = call("POST", "/api/auth/setup",
                         {"invite_id": inv["invite_id"], "token": "wrong-token",
                          "otp": otp, "password": password},
                         expect=401)
    status, setup = call("POST", "/api/auth/setup",
                         {"invite_id": inv["invite_id"], "token": token,
                          "otp": "000000", "password": password},
                         expect=401)
    status, setup = call("POST", "/api/auth/setup",
                         {"invite_id": inv["invite_id"], "token": token,
                          "otp": otp, "password": password},
                         expect=200)
    assert status == 200, f"setup failed: {setup}"
    # Single-use: the same token + otp must not work twice.
    call("POST", "/api/auth/setup",
         {"invite_id": inv["invite_id"], "token": token,
          "otp": otp, "password": password}, expect=401)
    return setup["access_token"], setup["user"]


def db_exec(sql, params=()):
    con = sqlite3.connect(DB)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def db_one(sql, params=()):
    con = sqlite3.connect(DB)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


print(f"C5 roles tests against {BASE} (db {DB})")
TA = login("demo@qgbs.example", "demo-password")
_, me = call("GET", "/api/users/me", token=TA, expect=200)
check("seed user is tenant_admin after C5 migration", me["role"] == "tenant_admin",
      f"got={me['role']}")
SUPER = login("super@platform.example", "super-password")
_, me_s = call("GET", "/api/users/me", token=SUPER, expect=200)
check("bootstrap user is super_admin", me_s["role"] == "super_admin", f"got={me_s['role']}")

print("── Super Admin: platform powers, no customer data ──")
_, cat = call("POST", "/api/catalog/agents",
              {"slug": "c5-test-agent", "name": "C5 Test Agent", "stage": "Verification"},
              token=SUPER, expect=201)
_, cat_v = call("POST", "/api/catalog/agents/c5-test-agent/versions",
                {"prompt": "ROLE\nTest.\nDISPOSITIONS\n- TEST_BUCKET: test\nCLOSINGS\nBye.\n",
                 "suitable_for": "managed", "model": "gpt-4o", "validated_on": "Vapi managed"},
                token=SUPER, expect=201)
call("POST", "/api/campaigns", {"agent_id": "x", "name": "nope"}, token=SUPER, expect=403)
call("GET", "/api/campaigns", token=SUPER, expect=403)
call("GET", "/api/calls", token=SUPER, expect=403)
call("GET", "/api/queue", token=SUPER, expect=403)
_, t2 = call("POST", "/api/tenants", {"name": "OtherCo", "market": "IN"},
              token=SUPER, expect=201)
check("tenant creation returns invite, not password",
      "admin_password" not in t2 and "setup_url" in t2, f"got={list(t2.keys())}")
_, t2_setup = call("POST", "/api/auth/setup",
                   {"invite_id": t2["invite_id"],
                    "token": t2["setup_url"].split("token=")[1],
                    "otp": t2.get("dev_otp"), "password": "other-admin-pass"},
                   expect=200)
TA2 = t2_setup["access_token"]
check("new tenant admin is tenant_admin in the new tenant",
      t2_setup["user"]["role"] == "tenant_admin"
      and t2_setup["user"]["tenant_id"] == t2["tenant"]["tenant_id"])

print("── Tenant Admin: no catalog, no prompt editor, no tenants ──")
call("POST", "/api/catalog/agents", {"slug": "x", "name": "x"}, token=TA, expect=403)
_, agents = call("GET", "/api/agents", token=TA, expect=200)
rpi = next(a for a in agents if a["name"] == "RPI Verification")
stl = next(a for a in agents if a["name"] == "Settlement Negotiation")
call("POST", "/api/agents", {"name": "x", "prompt": "y"}, token=TA, expect=403)
call("PUT", f"/api/agents/{rpi['agent_id']}", dict(rpi, prompt="edited"),
     token=TA, expect=403)
call("POST", "/api/tenants", {"name": "Nope", "market": "IN"}, token=TA, expect=403)
_, tp = call("PUT", "/api/tenants/me", {"timezone": "America/Toronto"},
             token=TA, expect=200)
check("tenant settings updated by Tenant Admin", tp["timezone"] == "America/Toronto")
call("PUT", "/api/tenants/me", {"timezone": "Asia/Kolkata"}, token=SUPER, expect=403)
call("PUT", "/api/tenants/me", {"timezone": "Asia/Kolkata"}, token=TA, expect=200)

print("── Invite flow: Caller + Team Leader + SMS fallback ──")
CALLER_TOK, caller_user = invite_and_setup(TA, "caller1@test.example", "Caller One",
                                           "caller", password="caller-password")
check("invited Caller role is caller", caller_user["role"] == "caller")
_, me_tenant = call("GET", "/api/tenants/me", token=TA, expect=200)
TL_TOK, tl_user = invite_and_setup(SUPER, "lead1@test.example", "Lead One",
                                   "team_leader", password="lead-password",
                                   tenant_id=me_tenant["tenant_id"])
check("invited Team Leader role is team_leader", tl_user["role"] == "team_leader")
SMS_TOK, sms_user = invite_and_setup(TA, "smscaller@test.example", "SMS Caller",
                                     "caller", channel="sms", password="sms-password")
check("SMS-channel invite completes the same way", sms_user["role"] == "caller")
call("POST", "/api/users/invites",
     {"email": "x@test.example", "name": "X", "role": "super_admin"},
     token=SUPER, expect=422)
call("POST", "/api/users/invites",
     {"email": "y@test.example", "name": "Y", "role": "tenant_admin"},
     token=TA, expect=403)
_, team = call("POST", "/api/teams", {"name": "Team Alpha", "leader_user_id": tl_user["user_id"]},
               token=TA, expect=201)
_, caller_moved = call("PATCH", f"/api/users/{caller_user['user_id']}",
                       {"team_id": team["team_id"]}, token=TA, expect=200)
check("Caller moved into the TL's team", caller_moved["team_id"] == team["team_id"])
call("GET", "/api/users", token=CALLER_TOK, expect=403)
_, team_list = call("GET", "/api/teams", token=TL_TOK, expect=200)
check("TL lists only teams they lead",
      len(team_list) == 1 and team_list[0]["team_id"] == team["team_id"])

print("── Campaign + allocation (TL only) ──")
_, camp = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "C5 RPI", "product": "vehicle_loan",
    "provider_mode": "managed",
    "schedule": {"calling_days": "Every day", "window_start": "00:00", "window_end": "23:59"},
}, token=TA, expect=201)
call_upload(f"/api/campaigns/{camp['campaign_id']}/leads/upload",
            "phone_number,name,bank_name,state\n+10000000001,Case One,Test Bank,Uttar Pradesh\n+10000000002,Case Two,Test Bank,Uttar Pradesh\n",
            TA, {"phone_number": "phone_number", "name": "name",
                 "bank_name": "bank_name", "state": "state"}, expect=200)
_, leads = call("GET", f"/api/campaigns/{camp['campaign_id']}/leads", token=TA, expect=200)
lead1, lead2 = leads["leads"][0], leads["leads"][1]
call("POST", f"/api/campaigns/{camp['campaign_id']}/leads/{lead1['lead_id']}/allocate",
     {"caller_id": caller_user["user_id"]}, token=TA, expect=403)
call("POST", f"/api/campaigns/{camp['campaign_id']}/leads/{lead1['lead_id']}/allocate",
     {"caller_id": caller_user["user_id"]}, token=CALLER_TOK, expect=403)
_, alloc = call("POST", f"/api/campaigns/{camp['campaign_id']}/leads/{lead1['lead_id']}/allocate",
                {"caller_id": caller_user["user_id"]}, token=TL_TOK, expect=200)
check("TL allocation recorded with audit",
      alloc["assigned_caller_id"] == caller_user["user_id"]
      and alloc["allocated_by"] == tl_user["user_id"] and alloc["allocated_at"],
      f"got={alloc}")
# TL cannot allocate to a Caller outside their team (SMS caller has no team).
call("POST", f"/api/campaigns/{camp['campaign_id']}/leads/{lead2['lead_id']}/allocate",
     {"caller_id": sms_user["user_id"]}, token=TL_TOK, expect=422)
_, caller_leads = call("GET", f"/api/campaigns/{camp['campaign_id']}/leads",
                       token=CALLER_TOK, expect=200)
check("Caller sees only own leads",
      len(caller_leads["leads"]) == 1
      and caller_leads["leads"][0]["lead_id"] == lead1["lead_id"],
      f"got={[l['lead_id'] for l in caller_leads['leads']]}")
_, tl_leads = call("GET", f"/api/campaigns/{camp['campaign_id']}/leads",
                   token=TL_TOK, expect=200)
check("TL sees team's + unallocated leads", len(tl_leads["leads"]) == 2)
_, caller_camps = call("GET", "/api/campaigns", token=CALLER_TOK, expect=200)
check("Caller campaign list holds only own-case campaigns",
      any(c["campaign_id"] == camp["campaign_id"] for c in caller_camps))

print("── Upload caller column ──")
_, camp2 = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "C5 RPI upload", "product": "vehicle_loan",
    "provider_mode": "managed",
    "schedule": {"calling_days": "Every day", "window_start": "00:00", "window_end": "23:59"},
}, token=TA, expect=201)
_, up = call_upload(f"/api/campaigns/{camp2['campaign_id']}/leads/upload",
                    "phone_number,name,bank_name,state,caller\n+10000000003,Case Three,Test Bank,Uttar Pradesh,Caller One\n+10000000004,Case Four,Test Bank,Uttar Pradesh,Nobody Known\n",
                    TA, {"phone_number": "phone_number", "name": "name",
                         "bank_name": "bank_name", "state": "state", "caller": "caller"},
                    expect=200)
check("upload resolves known caller, reports unknown",
      up["created"] == 2 and up["unresolved_callers"] == ["Nobody Known"],
      f"got={up}")
_, leads2 = call("GET", f"/api/campaigns/{camp2['campaign_id']}/leads", token=TA, expect=200)
by_phone = {l["phone_number"]: l for l in leads2["leads"]}
check("known-caller lead assigned at upload",
      by_phone["+10000000003"]["assigned_caller_id"] == caller_user["user_id"])
check("unknown-caller lead stays unallocated",
      by_phone["+10000000004"]["assigned_caller_id"] is None)

print("── Dispatch + queue approvals + disposition audit ──")
call("POST", f"/api/campaigns/{camp['campaign_id']}/status",
     {"status": "active"}, token=TA, expect=200)
call("POST", f"/api/campaigns/{camp['campaign_id']}/dispatch", token=TA, expect=200)
_, camp_stl = call("POST", "/api/campaigns", {
    "agent_id": stl["agent_id"], "name": "C5 Settlement", "product": "vehicle_loan",
    "provider_mode": "managed",
    "schedule": {"calling_days": "Every day", "window_start": "00:00", "window_end": "23:59"},
}, token=TA, expect=201)
call_upload(f"/api/campaigns/{camp_stl['campaign_id']}/leads/upload",
            "phone_number,name,bank_name,state\n+10000000005,Case Five,Test Bank,Uttar Pradesh\n",
            TA, {"phone_number": "phone_number", "name": "name",
                 "bank_name": "bank_name", "state": "state"}, expect=200)
_, stl_leads = call("GET", f"/api/campaigns/{camp_stl['campaign_id']}/leads", token=TA, expect=200)
call("POST", f"/api/campaigns/{camp_stl['campaign_id']}/leads/{stl_leads['leads'][0]['lead_id']}/allocate",
     {"caller_id": caller_user["user_id"]}, token=TL_TOK, expect=200)
call("POST", f"/api/campaigns/{camp_stl['campaign_id']}/status",
     {"status": "active"}, token=TA, expect=200)
call("POST", f"/api/campaigns/{camp_stl['campaign_id']}/dispatch", token=TA, expect=200)
_, rpi_calls = call("GET", f"/api/campaigns/{camp['campaign_id']}/calls", token=TA, expect=200)
_, stl_calls = call("GET", f"/api/campaigns/{camp_stl['campaign_id']}/calls", token=TA, expect=200)
rpi_session = rpi_calls["calls"][0]["session_id"]
stl_session = stl_calls["calls"][0]["session_id"]
# Flag both results for review with a live approval window.
db_exec("UPDATE call_results SET review_required = 1, "
        "approval_expires_at = datetime('now', '+1 day') "
        "WHERE session_id IN (?, ?)", (rpi_session, stl_session))
_, queue_ta = call("GET", "/api/queue", token=TA, expect=200)
check("Tenant Admin queue holds both flagged items", len(queue_ta) >= 2)
_, queue_caller = call("GET", "/api/queue", token=CALLER_TOK, expect=200)
check("Caller queue holds only own items",
      all(q["assigned_caller_id"] == caller_user["user_id"] for q in queue_caller)
      and len(queue_caller) >= 1)
rpi_result = next(q["result_id"] for q in queue_ta if q["session_id"] == rpi_session)
stl_result = next(q["result_id"] for q in queue_ta if q["session_id"] == stl_session)
check("settlement item flagged as settlement",
      next(q["is_settlement"] for q in queue_ta if q["session_id"] == stl_session) is True)
call("POST", f"/api/queue/{stl_result}/approve", {"decision": "approved"},
     token=TA, expect=403)
call("POST", f"/api/queue/{rpi_result}/approve", {"decision": "approved"},
     token=CALLER_TOK, expect=403)
_, appr = call("POST", f"/api/queue/{rpi_result}/approve", {"decision": "approved"},
               token=TA, expect=200)
check("Tenant Admin approval resolves the item",
      appr["approval_decision"] == "approved")
_, appr_row = call("GET", "/api/queue", token=TA, expect=200)
check("approved item leaves the queue",
      all(q["result_id"] != rpi_result for q in appr_row))
# Audit check via DB (approved_by must be the Tenant Admin's user id).
ta_id = db_one("SELECT user_id FROM users WHERE email = 'demo@qgbs.example'")[0]
row = db_one("SELECT approval_decision, approved_by, approved_at FROM call_results "
             "WHERE result_id = ?", (rpi_result,))
check("approval audit row: decision + approver + timestamp",
      row[0] == "approved" and row[1] == ta_id and row[2] is not None, f"got={row}")
_, appr_stl = call("POST", f"/api/queue/{stl_result}/approve", {"decision": "approved"},
                   token=TL_TOK, expect=200)
check("Team Leader approves the settlement item", appr_stl["approval_decision"] == "approved")
# Expired approval -> 410 (re-flag the settlement result with a past deadline).
db_exec("UPDATE call_results SET review_required = 1, approval_decision = NULL, "
        "approved_by = NULL, approved_at = NULL, "
        "approval_expires_at = datetime('now', '-1 hour') WHERE result_id = ?",
        (stl_result,))
call("POST", f"/api/queue/{stl_result}/approve", {"decision": "approved"},
     token=TL_TOK, expect=410)

print("── Disposition PATCH audit ──")
_, before = call("GET", f"/api/campaigns/{camp['campaign_id']}/calls", token=TA, expect=200)
old_disp = before["calls"][0]["disposition"]
buckets = ["RPC_CONFIRMED", "CPC_CONFIRMED", "FINAL_DENIAL", "CALLBACK_REQUESTED"]
new_disp = next(b for b in buckets if b != old_disp)
call("PATCH", f"/api/calls/{rpi_session}/disposition", {"disposition": "NOT_A_BUCKET"},
     token=TA, expect=422)
_, patched = call("PATCH", f"/api/calls/{rpi_session}/disposition",
                  {"disposition": new_disp}, token=CALLER_TOK, expect=200)
check("Caller corrects own case's disposition", patched["disposition"] == new_disp)
row = db_one("SELECT disposition_updated_by, disposition_updated_at FROM call_results "
             "WHERE session_id = ?", (rpi_session,))
check("disposition audit row: who + when",
      row[0] == caller_user["user_id"] and row[1] is not None, f"got={row}")
_, sms_calls = call("GET", "/api/calls", token=SMS_TOK, expect=200)
check("unassigned Caller sees no calls", sms_calls["total"] == 0)
call("PATCH", f"/api/calls/{stl_session}/disposition", {"disposition": new_disp},
     token=SMS_TOK, expect=404)

print("── Prompt redaction for TL / Caller ──")
_, agents_caller = call("GET", "/api/agents", token=CALLER_TOK, expect=200)
check("Caller agent list has prompts redacted",
      all(a["prompt"] == "" for a in agents_caller), f"got={[len(a['prompt']) for a in agents_caller]}")
call("GET", f"/api/agents/{rpi['agent_id']}/prompt-versions", token=CALLER_TOK, expect=403)
_, versions_ta = call("GET", f"/api/agents/{rpi['agent_id']}/prompt-versions",
                      token=TA, expect=200)
check("Tenant Admin still reads prompt history", len(versions_ta) >= 1)

print(f"\nC5 RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
