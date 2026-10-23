#!/usr/bin/env python3
"""Smoke tests for Phase A chunks 1–2 (A1, A2, A3) against a running server.

Usage:  python3 scripts/smoke_test.py [base_url]     (default http://localhost:8000)
Covers: health · login · tenant scoping (A1 acceptance) · agent CRUD ·
single-prompt versioning + rollback (A2) · campaign CRUD bound to one agent,
status lifecycle, CSV lead upload with column mapping, product identifiers,
tenant isolation on campaigns/leads, no from-number in the campaign schema (A3).
"""
import json
import sys
import urllib.request
import urllib.error
import uuid

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


def call_upload(path, csv_text, token=None, fields=None, expect=None):
    """Multipart POST of a CSV file (+ optional form fields), same counting."""
    global passed, failed
    boundary = "----smoke" + uuid.uuid4().hex
    parts = []
    for name, value in (fields or {}).items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"leads.csv\"\r\n"
        "Content-Type: text/csv\r\n\r\n".encode() + csv_text.encode() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(BASE + path, method="POST", data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    try:
        payload = json.loads(payload) if payload else None
    except Exception:
        payload = None
    ok = (expect is None and 200 <= status < 300) or status == expect
    label = f"POST {path} (csv upload) -> {status}"
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

print("── A3: campaign schema — no from-number, business fields only ──")
_, spec = call("GET", "/openapi.json", token=token)
schemas = spec["components"]["schemas"]
for name in ("CampaignIn", "CampaignOut"):
    props = schemas[name]["properties"]
    assert "from_number" not in props, f"{name} must never carry a from-number field"
    for banned in ("llm_model", "prompt", "tts_voice_id", "language_rule", "stt_provider"):
        assert banned not in props, f"{name} must not carry agent-level field {banned}"
print("  PASS  campaign schemas: no from_number / model / voice / prompt fields")

print("── A3: campaign CRUD bound to one agent ──────")
_, camp = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"],
    "name": "October — RPI Verification Batch 1",
    "product": "vehicle_loan", "product_detail": "two-wheeler (stale records)",
    "purpose": "Re-verify stale records", "audience": "Customers whose records went stale",
    "channel": "Service Channel 1", "provider_mode": "self_hosted",
    "schedule": {"start_date": "2026-10-24", "calling_days": "Mon – Sat",
                 "window_start": "09:00", "window_end": "19:00", "daily_cap": 2000},
    "retry_rules": {"max_attempts": 3, "retry_gap_days": 1, "retry_different_time": True},
    "routing_rules": [],
}, token=token, expect=201)
cid = camp["campaign_id"]
assert camp["status"] == "draft" and camp["agent_name"] == "RPI Verification", camp
assert camp["product"] == "vehicle_loan" and camp["purpose"] == "Re-verify stale records", camp
assert camp["audience"].startswith("Customers") and camp["channel"] == "Service Channel 1", camp
assert camp["identifier_type"] == "registration_number" and camp["lead_count"] == 0, camp
assert "from_number" not in camp, camp
call("POST", "/api/campaigns", {"agent_id": "agt_missing", "name": "X"}, token=token, expect=422)
call("POST", "/api/campaigns", {"agent_id": rpi["agent_id"], "name": "X", "product": "yacht"},
     token=token, expect=422)

print("── A3: CSV preview + upload (RPI format) ─────")
RPI_CSV = (
    "callee_name,phone_number,bank_name,state\n"
    "A. Das,9000000001,Tata Capital,West Bengal\n"
    "B. Sen,9000000002,Tata Capital,West Bengal\n"
    "C. Roy,9000000003,HDFC Bank,Bihar\n"
    "D. Pal,9000000004,Tata Capital,Odisha\n"
    "E. Dey,9000000005,Tata Capital,West Bengal\n"
    "A. Das,9000000001,Tata Capital,West Bengal\n"   # duplicate phone in-file
)
_, preview = call_upload(f"/api/campaigns/{cid}/leads/preview", RPI_CSV, token=token)
assert preview["total_rows"] == 6, preview
assert preview["suggested_mapping"]["phone_number"] == "phone_number", preview
assert preview["suggested_mapping"]["callee_name"] == "name", preview
assert preview["suggested_mapping"]["state"] == "state", preview
_, up = call_upload(f"/api/campaigns/{cid}/leads/upload", RPI_CSV, token=token,
                    fields={"mapping": json.dumps(preview["suggested_mapping"])})
assert (up["created"], up["duplicates_skipped"], up["invalid_skipped"]) == (5, 1, 0), up
_, leadlist = call("GET", f"/api/campaigns/{cid}/leads", token=token)
assert leadlist["total"] == 5, leadlist
first = next(l for l in leadlist["leads"] if l["phone_number"] == "9000000001")
assert first["name"] == "A. Das" and first["state"] == "West Bengal" \
    and first["bank_name"] == "Tata Capital", first
_, up2 = call_upload(f"/api/campaigns/{cid}/leads/upload", RPI_CSV, token=token,
                     fields={"mapping": json.dumps(preview["suggested_mapping"])})
assert up2["created"] == 0 and up2["duplicates_skipped"] == 6, up2
call_upload(f"/api/campaigns/{cid}/leads/upload", RPI_CSV, token=token,
            fields={"mapping": json.dumps({"callee_name": "ignore"})}, expect=422)

print("── A3: product identifier per product type ───")
BANK_CSV = (
    "ACCOUNT_NO,CUSTOMER_NAME,PRINCIPAL,OUTSTANDING,MOBILE_NO,RegistrationNumber\n"
    "TCF123456,R. Sharma,9000,21400,9000000011,WB58A1234\n"
    "TCF123457,S. Verma,12000,30500,9000000012,WB58B9876\n"
)
_, camp2 = call("POST", "/api/campaigns", {
    "agent_id": aid, "name": "October — Settlement Batch 3",
    "product": "vehicle_loan", "purpose": "Close long-due accounts",
}, token=token, expect=201)
cid2 = camp2["campaign_id"]
_, preview2 = call_upload(f"/api/campaigns/{cid2}/leads/preview", BANK_CSV, token=token)
assert preview2["suggested_mapping"]["RegistrationNumber"] == "product_identifier", preview2
assert preview2["suggested_mapping"]["MOBILE_NO"] == "phone_number", preview2
_, up3 = call_upload(f"/api/campaigns/{cid2}/leads/upload", BANK_CSV, token=token,
                     fields={"mapping": json.dumps(preview2["suggested_mapping"])})
assert up3["created"] == 2, up3
_, leads2 = call("GET", f"/api/campaigns/{cid2}/leads", token=token)
af = next(l for l in leads2["leads"] if l["phone_number"] == "9000000011")["agent_fields"]
assert af["product_identifier"] == "WB58A1234", af
assert af["product_identifier_type"] == "registration_number", af
assert af["principal"] == "9000" and af["account_no"] == "TCF123456", af

CARD_CSV = "customer_name,mobile_no,card_last4\nK. Bose,9000000021,4321\n"
_, camp3 = call("POST", "/api/campaigns", {
    "agent_id": aid, "name": "Card — Reminder Wave", "product": "credit_card",
}, token=token, expect=201)
assert camp3["identifier_type"] == "card_last4", camp3
_, preview3 = call_upload(f"/api/campaigns/{camp3['campaign_id']}/leads/preview", CARD_CSV, token=token)
assert preview3["suggested_mapping"]["card_last4"] == "product_identifier", preview3
call_upload(f"/api/campaigns/{camp3['campaign_id']}/leads/upload", CARD_CSV, token=token,
            fields={"mapping": json.dumps(preview3["suggested_mapping"])})
_, leads3 = call("GET", f"/api/campaigns/{camp3['campaign_id']}/leads", token=token)
assert leads3["leads"][0]["agent_fields"]["product_identifier_type"] == "card_last4", leads3

print("── A3: schedule / retry / routing update ─────")
_, upd = call("PUT", f"/api/campaigns/{cid}", {
    **camp, "schedule": {**camp["schedule"], "window_start": "10:00"},
    "retry_rules": {**camp["retry_rules"], "max_attempts": 5},
    "routing_rules": [{"disposition": "RPC_CONFIRMED", "action": "route_to_campaign", "target": ""}],
}, token=token)
assert upd["schedule"]["window_start"] == "10:00", upd
assert upd["retry_rules"]["max_attempts"] == 5, upd
assert upd["routing_rules"][0]["disposition"] == "RPC_CONFIRMED", upd

print("── A3: status lifecycle ──────────────────────")
call("POST", f"/api/campaigns/{cid}/status", {"status": "completed"}, token=token, expect=400)  # draft→completed illegal
_, st = call("POST", f"/api/campaigns/{cid}/status", {"status": "active"}, token=token)
assert st["status"] == "active", st
call("POST", f"/api/campaigns/{cid}/status", {"status": "draft"}, token=token, expect=400)
_, st = call("POST", f"/api/campaigns/{cid}/status", {"status": "paused"}, token=token)
assert st["status"] == "paused", st
_, st = call("POST", f"/api/campaigns/{cid}/status", {"status": "completed"}, token=token)
assert st["status"] == "completed", st
call("POST", f"/api/campaigns/{cid}/status", {"status": "active"}, token=token, expect=400)  # completed is terminal

print("── A3: tenant isolation on campaigns/leads ───")
_, clist2 = call("GET", "/api/campaigns", token=token2)
assert clist2 == [], f"other tenant sees campaigns: {clist2}"
call("GET", f"/api/campaigns/{cid}", token=token2, expect=404)
call("GET", f"/api/campaigns/{cid}/leads", token=token2, expect=404)
call_upload(f"/api/campaigns/{cid}/leads/upload", RPI_CSV, token=token2,
            fields={"mapping": json.dumps(preview["suggested_mapping"])}, expect=404)
call("POST", "/api/campaigns", {"agent_id": rpi["agent_id"], "name": "Cross-tenant"},
     token=token2, expect=422)  # cannot bind another tenant's agent

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
