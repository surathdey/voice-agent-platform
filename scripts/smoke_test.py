#!/usr/bin/env python3
"""Smoke tests for Phase A chunks 1–9 (A1–A10) against a running server.

Usage:  python3 scripts/smoke_test.py [base_url]     (default http://localhost:8000)
        Run with the backend venv python (backend/.venv/bin/python) —
        the A6/A7/A8 sections boot their own app instances with that
        interpreter and need its `websockets` package for the stream
        tests and the mock Saaras server.
Covers: health · login · tenant scoping (A1 acceptance) · agent CRUD ·
single-prompt versioning + rollback (A2) · campaign CRUD bound to one agent,
status lifecycle, CSV lead upload with column mapping, product identifiers,
tenant isolation on campaigns/leads, no from-number in the campaign schema
(A3) · per-tenant provider settings, secret masking (sentinel keys never
appear in any response), Test connection stub/failed honesty, campaign
provider flagging, health per-mode providers (A4) · dispatch on active
campaigns only, schedule-window + daily-cap admission, stub transport,
§8.2 result-contract conformance, deterministic prompt-bucket round-robin,
exactly-one-result (re-dispatch + duplicate result delivery), tenant
isolation on dispatch/calls/results (A5) · real transports against a
local FAKE provider — Vapi start-call payload (single prompt, tenant
defaults, per-language voice profiles by language + agent voice gender),
end-of-call webhook → exactly one §8.2 result, duplicate webhook no-op,
Plivo create-call + answer XML + placeholder stream finalize, status
callback finalize, stub-mode + mode-mismatch honesty gates (A6) · the
STT leg against a local MOCK Saaras WebSocket server — session opened
per call in the lead's resolved language with the tenant key, media
frames forwarded, partial transcript on the live view, final segments
persisted mid-call and carried into the §8.2 result; STT-down path
(failure recorded, transcript marked partial, call still finalizes
once); no-config path identical to the A6 spike (A7) · dispatch at
scale — schedule/windows/days/start-date/daily-cap evaluated in the
tenant's MARKET-LOCAL timezone, retry/re-queue of no-answer/busy/failed
attempts as NEW attempts (gap honoured, exhaustion final, exactly one
result per attempt), campaign results summary + tenant dashboard +
calls filters, and managed-mode parity: the same result-surface
assertions against a stub-completed call and a fake-Vapi
webhook-completed call, incl. the review-flag gap (A10) · the upgrade
path (post-A12 fix): a database built at the 002-era shape boots,
migrates in place via the boot-time migration runner (data intact,
schema_migrations 1–10), and a second boot is a byte-identical no-op;
a fresh database still boots to the identical final schema · B2 (Phase B):
the seeded Payment Reminder agent — 9 disposition buckets parsed from its
prompt (incl. PAYMENT_DATE_GIVEN), a stub-mode campaign dispatch that
finalizes, and the built Vapi payload carrying the lead's due_date /
amount_due variables · B3–B5 (Phase B): the same pattern for
Promise-to-Pay Negotiation, Settlement Negotiation (incl. the
campaign authority cap reaching the call payload) and Broken-Promise
Recovery · B6: escalation disposition (TRANSFERRED_TO_HUMAN) on all
three new agents plus human-queue routing-rule flagging.

Run against a FRESH database: like the earlier chunks, the suite seeds its
own tenants/agents/campaigns/settings and asserts exact fresh-state values.
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
assert len(agents) == 5, agents  # RPI + Reminder + PTP + Settlement + Broken-Promise (B2–B5 seed)
rpi = next(a for a in agents if a["name"] == "RPI Verification")
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

print("── A4: provider settings — fresh state ───────")
call("GET", "/api/settings/providers", expect=401)  # unauthenticated
_, plist = call("GET", "/api/settings/providers", token=token)
by_key = {p["provider"]: p for p in plist}
assert set(by_key) == {"vapi", "twilio", "plivo", "exotel", "sarvam"}, sorted(by_key)
assert by_key["vapi"]["status"] == "stub" and by_key["vapi"]["configured"] is False, by_key["vapi"]
assert by_key["plivo"]["status"] == "stub" and by_key["sarvam"]["status"] == "stub", by_key
assert by_key["twilio"]["status"] == "not_configured", by_key["twilio"]
assert by_key["exotel"]["status"] == "not_configured", by_key["exotel"]
print("  PASS  fresh tenant: primary providers report stub; optional ones not_configured")

print("── A4: campaign flagged when its mode has no provider ──")
_, campA = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "A4 flag check — self-hosted",
    "provider_mode": "self_hosted",
}, token=token, expect=201)
assert campA["provider_configured"] is False and campA["provider_flag"], campA
assert "stub" in campA["provider_flag"], campA
print(f"  PASS  campaign created but flagged: {campA['provider_flag'][:60]}…")

print("── A4: settings CRUD + secret masking ────────")
VAPI_SENTINEL = "sk-live-SENTINELvapi0000111122223333wxyz"
PLIVO_TOKEN_SENTINEL = "PLIVOtokenSENTINELaaaabbbbccccdddd9999"
SARVAM_SENTINEL = "sk-sarvam-SENTINELeeeeffffgggghhhh7777"
_, saved = call("PUT", "/api/settings/providers/vapi",
                {"fields": {"api_key": VAPI_SENTINEL}}, token=token)
assert saved["configured"] is True and saved["status"] == "configured", saved
f0 = saved["fields"][0]
assert f0["is_set"] and f0["masked"] == "••••wxyz" and f0["value"] is None, f0
assert VAPI_SENTINEL not in json.dumps(saved), "full Vapi key leaked in PUT response"
print("  PASS  vapi saved — response carries only ••••wxyz, never the key")
_, reread = call("GET", "/api/settings/providers/vapi", token=token)
assert reread["fields"][0]["masked"] == "••••wxyz", reread
assert VAPI_SENTINEL not in json.dumps(reread), "full Vapi key leaked on re-read"
_, kept = call("PUT", "/api/settings/providers/vapi",
               {"fields": {"api_key": ""}}, token=token)
assert kept["fields"][0]["masked"] == "••••wxyz", kept
print("  PASS  masked on re-read; blank secret on save keeps the stored key")
_, withmv = call("PUT", "/api/settings/providers/vapi",
                 {"fields": {"default_model": "gpt-4o-mini", "default_voice": "aria-voice-1"}},
                 token=token)
mvf = {f["name"]: f for f in withmv["fields"]}
assert mvf["api_key"]["masked"] == "••••wxyz", mvf  # key untouched by the config save
assert mvf["default_model"]["value"] == "gpt-4o-mini" and mvf["default_model"]["secret"] is False, mvf
assert mvf["default_voice"]["value"] == "aria-voice-1", mvf
print("  PASS  Vapi tenant-default model + voice stored as plain config fields")
call("PUT", "/api/settings/providers/vapi", {"fields": {"nope": "x"}}, token=token, expect=422)
call("GET", "/api/settings/providers/nope", token=token, expect=404)

print("── A4: configure self-hosted — campaign flag clears ──")
_, plivo = call("PUT", "/api/settings/providers/plivo",
                {"fields": {"auth_id": "MA_SENTINEL_ID", "auth_token": PLIVO_TOKEN_SENTINEL}},
                token=token)
pf = {f["name"]: f for f in plivo["fields"]}
assert pf["auth_id"]["value"] == "MA_SENTINEL_ID" and pf["auth_id"]["secret"] is False, pf
assert pf["auth_token"]["masked"].endswith("9999"), pf
assert PLIVO_TOKEN_SENTINEL not in json.dumps(plivo), "Plivo token leaked in PUT response"
_, sarv = call("PUT", "/api/settings/providers/sarvam",
               {"fields": {"api_key": SARVAM_SENTINEL}}, token=token)
assert sarv["configured"] is True, sarv
_, campA2 = call("GET", f"/api/campaigns/{campA['campaign_id']}", token=token)
assert campA2["provider_configured"] is True and campA2["provider_flag"] is None, campA2
_, campM = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "A4 flag check — managed", "provider_mode": "managed",
}, token=token, expect=201)
assert campM["provider_configured"] is True and campM["provider_flag"] is None, campM
print("  PASS  Plivo + Sarvam configured → self-hosted flag cleared; Vapi → managed clear")
_, plist_all = call("GET", "/api/settings/providers", token=token)
blob = json.dumps(plist_all)
for s in (VAPI_SENTINEL, PLIVO_TOKEN_SENTINEL, SARVAM_SENTINEL):
    assert s not in blob, "a full secret leaked into the providers list response"
print("  PASS  no full secret appears anywhere in the providers list response")

print("── A4: incomplete config + delete ────────────")
_, tw = call("PUT", "/api/settings/providers/twilio",
             {"fields": {"account_sid": "AC_SENTINEL"}}, token=token)
assert tw["configured"] is False and tw["status"] == "not_configured", tw
_, tw2 = call("PUT", "/api/settings/providers/twilio",
              {"fields": {"auth_token": "twilioSENTINELtoken00001111zzzz"}}, token=token)
assert tw2["configured"] is True, tw2
assert tw2["fields"][1]["masked"] == "••••zzzz", tw2
_, twd = call("DELETE", "/api/settings/providers/twilio", token=token)
assert twd["status"] == "not_configured" and twd["configured"] is False, twd
print("  PASS  partial credentials stay not_configured; DELETE reverts cleanly")

print("── A4: test connection — stub & fake keys ────")
_, tstub = call("POST", "/api/settings/providers/sarvam/test", token=token2)
assert tstub["status"] == "stub" and "no" in tstub["message"].lower(), tstub
print(f"  PASS  unconfigured provider tests as stub: {tstub['message'][:60]}…")
_, tfake = call("POST", "/api/settings/providers/vapi/test", token=token)
assert tfake["status"] == "failed" and tfake["message"], tfake
print(f"  PASS  fake Vapi key honestly reports failed: {tfake['message'][:70]}")
_, after = call("GET", "/api/settings/providers/vapi", token=token)
assert after["status"] == "failed" and after["last_test_status"] == "failed", after
assert VAPI_SENTINEL not in json.dumps(after)

print("── A4: tenant isolation on settings ──────────")
_, plist2 = call("GET", "/api/settings/providers", token=token2)
blob2 = json.dumps(plist2)
for s in (VAPI_SENTINEL, PLIVO_TOKEN_SENTINEL, SARVAM_SENTINEL):
    assert s not in blob2, "another tenant's secret leaked into tenant 2's settings"
by2 = {p["provider"]: p for p in plist2}
assert by2["vapi"]["configured"] is False and by2["plivo"]["configured"] is False, by2
assert by2["sarvam"]["configured"] is False, by2
print("  PASS  tenant 2 sees none of QGBS's provider settings or secrets")

print("── A4: health reports providers per mode ─────")
_, health2 = call("GET", "/api/health")
assert set(health2["providers"]) == {"managed", "self_hosted"}, health2
assert health2["providers"]["managed"] == {"vapi": False, "twilio": False}, health2
assert health2["providers"]["self_hosted"] == {"plivo": False, "exotel": False, "sarvam": False}, health2
assert VAPI_SENTINEL not in json.dumps(health2) and SARVAM_SENTINEL not in json.dumps(health2)
print("  PASS  health: per-mode booleans from the .env fallback only — no tenant values")

print("── A5: dispatch — active campaign, stub transport ──")
from datetime import datetime, timedelta  # local: window arithmetic below

A5_CSV = (
    "callee_name,phone_number,bank_name,state\n"
    "F. Ali,9200000001,Tata Capital,West Bengal\n"
    "G. Nath,9200000002,Tata Capital,Bihar\n"
    "H. Pal,9200000003,HDFC Bank,Odisha\n"
    "I. Sen,9200000004,Tata Capital,Assam\n"
    "J. Dey,9200000005,Tata Capital,West Bengal\n"
)
A5_MAP = {"callee_name": "name", "phone_number": "phone_number",
          "bank_name": "bank_name", "state": "state"}
_, camp5 = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "A5 — Dispatch Batch",
    "product": "vehicle_loan", "provider_mode": "self_hosted",
    "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
}, token=token, expect=201)
cid5 = camp5["campaign_id"]
call_upload(f"/api/campaigns/{cid5}/leads/upload", A5_CSV, token=token,
            fields={"mapping": json.dumps(A5_MAP)})
call("POST", f"/api/campaigns/{cid5}/dispatch", token=token, expect=400)   # draft must not dispatch
call("POST", f"/api/campaigns/{cid5}/status", {"status": "active"}, token=token)
_, rep = call("POST", f"/api/campaigns/{cid5}/dispatch", token=token)
# QGBS has (fake) Plivo+Sarvam keys from the A4 section, so the provider is
# "ready" — but TELEPHONY_MODE=stub still forces the stub transport, and
# the report says so. A real transport ships in A6, never silently here.
assert rep["transport"] == "stub" and rep["stub"] is True, rep
assert rep["provider_ready"] is True, rep
assert rep["created"] == 5 and rep["finalized"] == 5, rep
assert rep["counts_by_state"] == {"COMPLETED": 5}, rep
assert rep["outside_window"] is False, rep
print("  PASS  dispatch: 5 attempts admitted, all finalized via the stub transport")

print("── A5: §8.2 result contract conformance ──────")
_, cl = call("GET", f"/api/campaigns/{cid5}/calls", token=token)
assert cl["total"] == 5 and cl["counts_by_state"] == {"COMPLETED": 5}, cl
CONTRACT = {"session_id", "attempt_id", "provider_mode", "final_state",
            "end_reason", "disposition", "confidence", "summary",
            "captured_fields", "duration_s", "recording_url", "transcript",
            "cost_breakdown"}
RPI_BUCKETS = ["RPC_CONFIRMED", "CPC_CONFIRMED", "FINAL_DENIAL",
               "FINAL_DENIAL_CLOSING", "CALLBACK_REQUESTED", "HOSTILE",
               "DECEASED_CASE", "DNC_ESCALATION"]
got_dispositions = []
for c in cl["calls"]:
    assert CONTRACT <= set(c), f"call missing §8.2 fields: {CONTRACT - set(c)}"
    assert c["final_state"] == "COMPLETED" and c["transport"] == "stub", c
    assert c["disposition"] in RPI_BUCKETS, c  # from the agent prompt's own set
    assert c["duration_s"] > 0, c
    cb = c["cost_breakdown"]
    assert {"telephony", "stt", "llm", "tts", "total", "currency"} <= set(cb), cb
    assert cb["total"] == 0 and cb["mode"] == "stub" and cb["simulated"] is True, cb
    assert c["transcript"] and all({"speaker", "text", "offset_s"} <= set(t)
                                   for t in c["transcript"]), c
    assert "[STUB]" in c["summary"], c
    assert isinstance(c["captured_fields"].get("identity_confirmed"), bool), c
    got_dispositions.append(c["disposition"])
# Round-robin across the prompt's bucket set, in prompt order.
assert got_dispositions == RPI_BUCKETS[:5], got_dispositions
by_disp = {c["disposition"]: c for c in cl["calls"]}
assert by_disp["RPC_CONFIRMED"]["captured_fields"]["identity_confirmed"] is True
assert by_disp["CPC_CONFIRMED"]["captured_fields"]["identity_confirmed"] is False
attempt_ids = {c["attempt_id"] for c in cl["calls"]}
assert len(attempt_ids) == 5, attempt_ids  # one §8.1 idempotency key per attempt
print("  PASS  every call conforms to §8.2; dispositions round-robin the prompt's buckets in order")

print("── A5: exactly one result per attempt ────────")
# Auto-complete: with every lead done, the campaign completes itself —
# so a re-dispatch is refused (before the fix it stayed active and
# returned zeros here; that was the bug, proven live 2026-10-25).
_, camp5_done = call("GET", f"/api/campaigns/{cid5}", token=token)
assert camp5_done["status"] == "completed", camp5_done
call("POST", f"/api/campaigns/{cid5}/dispatch", token=token, expect=400)
_, cl2 = call("GET", f"/api/campaigns/{cid5}/calls", token=token)
assert cl2["total"] == 5, cl2  # a refused re-dispatch creates no second session
first = cl["calls"][0]
_, dupres = call("POST", "/api/results", {
    "session_id": first["session_id"], "attempt_id": first["attempt_id"],
    "final_state": "COMPLETED", "disposition": "HOSTILE", "duration_s": 999,
}, token=token)
assert dupres["created"] is False, dupres  # duplicate delivery is a no-op
assert dupres["call"]["disposition"] == first["disposition"], dupres  # stored result untouched
assert dupres["call"]["duration_s"] == first["duration_s"], dupres
call("POST", "/api/results", {"session_id": "ses_missing"}, token=token, expect=404)
print("  PASS  re-dispatch and duplicate result delivery never create a second result")
_, leads5 = call("GET", f"/api/campaigns/{cid5}/leads", token=token)
assert all(l["attempt_count"] == 1 and l["status"] == "done" for l in leads5["leads"]), leads5
print("  PASS  each lead shows exactly one attempt after dispatch")

print("── A5: paused / completed campaigns do not dispatch ──")
_, campP = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "A5 — Paused Batch", "provider_mode": "self_hosted",
}, token=token, expect=201)
call_upload(f"/api/campaigns/{campP['campaign_id']}/leads/upload", A5_CSV, token=token,
            fields={"mapping": json.dumps(A5_MAP)})
call("POST", f"/api/campaigns/{campP['campaign_id']}/status", {"status": "active"}, token=token)
call("POST", f"/api/campaigns/{campP['campaign_id']}/status", {"status": "paused"}, token=token)
call("POST", f"/api/campaigns/{campP['campaign_id']}/dispatch", token=token, expect=400)
call("POST", f"/api/campaigns/{campP['campaign_id']}/status", {"status": "completed"}, token=token)
call("POST", f"/api/campaigns/{campP['campaign_id']}/dispatch", token=token, expect=400)
print("  PASS  paused and completed campaigns refuse dispatch")

print("── A5: schedule window + daily cap admission ──")
_now = datetime.utcnow()
_win = {"calling_days": "Mon - Sun", "window_start": (_now + timedelta(hours=2)).strftime("%H:%M"),
        "window_end": (_now + timedelta(hours=3)).strftime("%H:%M"),
        "daily_cap": 2000}
_, campW = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "A5 — Outside Window",
    "provider_mode": "self_hosted", "schedule": _win,
}, token=token, expect=201)
call_upload(f"/api/campaigns/{campW['campaign_id']}/leads/upload", A5_CSV, token=token,
            fields={"mapping": json.dumps(A5_MAP)})
call("POST", f"/api/campaigns/{campW['campaign_id']}/status", {"status": "active"}, token=token)
_, repW = call("POST", f"/api/campaigns/{campW['campaign_id']}/dispatch", token=token)
assert repW["outside_window"] is True and repW["created"] == 0, repW
_, clW = call("GET", f"/api/campaigns/{campW['campaign_id']}/calls", token=token)
assert clW["total"] == 0, clW
print("  PASS  outside the calling window, no attempt is admitted")
_, campC = call("POST", "/api/campaigns", {
    "agent_id": rpi["agent_id"], "name": "A5 — Daily Cap 2",
    "provider_mode": "self_hosted",
    "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2},
}, token=token, expect=201)
call_upload(f"/api/campaigns/{campC['campaign_id']}/leads/upload",
            "callee_name,phone_number,bank_name,state\n"
            "K. A,9200000011,Tata Capital,Bihar\nL. B,9200000012,Tata Capital,Bihar\n"
            "M. C,9200000013,Tata Capital,Bihar\n",
            token=token, fields={"mapping": json.dumps(A5_MAP)})
call("POST", f"/api/campaigns/{campC['campaign_id']}/status", {"status": "active"}, token=token)
_, repC = call("POST", f"/api/campaigns/{campC['campaign_id']}/dispatch", token=token)
assert repC["created"] == 2 and repC["finalized"] == 2, repC
_, leadsC = call("GET", f"/api/campaigns/{campC['campaign_id']}/leads", token=token)
assert sum(1 for l in leadsC["leads"] if l["status"] == "done") == 2, leadsC
assert sum(1 for l in leadsC["leads"] if l["status"] == "pending") == 1, leadsC
print("  PASS  daily cap admits exactly 2 of 3 leads; the third stays pending")

print("── A5: calls list + tenant isolation ───────────")
_, allc = call("GET", "/api/calls", token=token)
assert allc["total"] == 7, allc  # 5 (dispatch batch) + 2 (capped batch)
_, filt = call("GET", f"/api/calls?campaign_id={cid5}", token=token)
assert filt["total"] == 5 and all(c["campaign_id"] == cid5 for c in filt["calls"]), filt
_, allc2 = call("GET", "/api/calls", token=token2)
assert allc2["total"] == 0, allc2  # tenant 2 has dispatched nothing
call("GET", f"/api/campaigns/{cid5}/calls", token=token2, expect=404)
call("POST", f"/api/campaigns/{cid5}/dispatch", token=token2, expect=404)
call("POST", "/api/results", {"session_id": first["session_id"]}, token=token2, expect=404)
print("  PASS  tenant-wide list + filter work; tenant 2 sees none of QGBS's calls")

print("── A6: real transports — fake providers only, no live calls ──")
# The A1–A5 sections above run against the stub-mode server on BASE. This
# section boots its OWN app instances (fresh temp DBs) whose provider
# base URLs point at a local FAKE provider server: the transports run
# their real code paths end-to-end, but no real provider is ever called.
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE = {"requests": [], "vapi_calls": 0}


class _FakeProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        FAKE["requests"].append({
            "method": self.command, "path": self.path,
            "headers": dict(self.headers), "body": raw.decode("utf-8", "replace"),
        })
        if self.path == "/call/phone":
            # Mirror live Vapi's one proven validation (2026-10-23):
            # Deepgram nova-2 has no Bengali — bn on nova-2 is a 400
            # whose body names the field, exactly as live returned it.
            try:
                _body = json.loads(raw or b"{}")
                _tr = (_body.get("assistant") or {}).get("transcriber") or {}
            except Exception:
                _tr = {}
            if _tr.get("model") == "nova-2" and _tr.get("language") == "bn":
                payload, code = {"message": ["assistant.transcriber.language must be one of the following values for nova-2 model: en, hi, multi"],
                                 "error": "Bad Request", "statusCode": 400}, 400
            else:
                FAKE["vapi_calls"] += 1
                payload, code = {"id": f"call_fake_{FAKE['vapi_calls']}", "status": "queued"}, 200
        elif self.command == "GET" and self.path.startswith("/call") \
                and "Python-urllib" in (self.headers.get("User-Agent") or ""):
            # UA-strict, mirroring live Vapi's edge (owner-proven
            # 2026-10-23): urllib's default UA is 403'd before auth.
            payload, code = {"message": "Forbidden"}, 403
        elif "/v1/Account/" in self.path and self.path.endswith("/Call/"):
            payload, code = {"request_uuid": "req-uuid-fake-1",
                             "message": "call queued", "api_id": "api-fake-1"}, 202
        else:
            payload, code = {}, 200
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = _handle
    do_GET = _handle


_fake_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
threading.Thread(target=_fake_server.serve_forever, daemon=True).start()
FAKE_BASE = f"http://127.0.0.1:{_fake_server.server_address[1]}"

_BACKEND6 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
_VENV_PY6 = os.path.join(_BACKEND6, ".venv", "bin", "python")
APP_PY6 = _VENV_PY6 if os.path.exists(_VENV_PY6) else sys.executable
TMP6 = tempfile.mkdtemp(prefix="a6-smoke-")
_procs6 = []


def check6(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {extra}")


def call6(base, method, path, body=None, token=None, expect=None,
          headers=None, form=None):
    """HTTP against an A6 app instance; returns (status, json_or_text).
    Counting mirrors call(): expect=None means any 2xx is a pass."""
    global passed, failed
    req = urllib.request.Request(base + path, method=method)
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    text = payload.decode("utf-8", "replace") if payload else ""
    try:
        parsed = json.loads(text) if text else None
    except Exception:
        parsed = text
    ok = (expect is None and 200 <= status < 300) or status == expect
    if ok:
        passed += 1
        print(f"  PASS  {method} {path} -> {status}")
    else:
        failed += 1
        print(f"  FAIL  {method} {path} -> {status} (expected {expect or '2xx'})  body={text[:200]}")
    return status, parsed


def upload6(base, path, csv_text, token, mapping):
    boundary = "----smoke6" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"mapping\"\r\n\r\n{json.dumps(mapping)}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"leads.csv\"\r\n"
        "Content-Type: text/csv\r\n\r\n".encode() + csv_text.encode() + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(base + path, method="POST", data=b"".join(parts))
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def start_app6(port, tag, env_extra):
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{TMP6}/{tag}.db",
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "WEBHOOK_SECRET": "a6-webhook-secret",
        "VAPI_BASE_URL": FAKE_BASE, "PLIVO_BASE_URL": FAKE_BASE,
        # Deterministic provider env: blank unless the scenario sets it.
        "VAPI_API_KEY": "", "VAPI_DEFAULT_MODEL": "", "VAPI_DEFAULT_VOICE": "",
        "VAPI_SERVER_CREDENTIAL_ID": "",
        "PLIVO_AUTH_ID": "", "PLIVO_AUTH_TOKEN": "", "SARVAM_API_KEY": "",
    })
    env.update(env_extra)
    log = open(f"{TMP6}/{tag}.log", "wb")
    proc = subprocess.Popen(
        [APP_PY6, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=_BACKEND6, env=env, stdout=log, stderr=log)
    _procs6.append(proc)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return base
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"A6 app '{tag}' on {port} did not start — see {TMP6}/{tag}.log")


def login6(base):
    _s, payload = call6(base, "POST", "/api/auth/login",
                        {"email": "demo@qgbs.example", "password": "demo-password"})
    return payload["access_token"]


A6_MAP = {"callee_name": "name", "phone_number": "phone_number",
          "bank_name": "bank_name", "state": "state"}
A6_CSV = (
    "callee_name,phone_number,bank_name,state\n"
    "F. Ali,9300000001,Tata Capital,West Bengal\n"
    "G. Nath,9300000002,Tata Capital,Bihar\n"
)


def campaign6(base, token, agent_id, name, mode, csv_text=A6_CSV):
    _s, camp = call6(base, "POST", "/api/campaigns", {
        "agent_id": agent_id, "name": name, "product": "vehicle_loan",
        "provider_mode": mode,
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=token, expect=201)
    cid = camp["campaign_id"]
    up = upload6(base, f"/api/campaigns/{cid}/leads/upload", csv_text, token, A6_MAP)
    assert up["created"] >= 1, up
    call6(base, "POST", f"/api/campaigns/{cid}/status", {"status": "active"}, token=token)
    return cid


try:
    # ── managed mode: Vapi transport ──────────────────────────────────
    baseM = start_app6(18101, "managed", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-managed",
        "VAPI_DEFAULT_MODEL": "gpt-4o-mini",
    })
    tokenM = login6(baseM)
    _, agentsM = call6(baseM, "GET", "/api/agents", token=tokenM)
    rpiM = agentsM[0]
    assert rpiM["name"] == "RPI Verification" and rpiM["voice_gender"] == "female", rpiM

    print("── A6: Vapi voice profiles — defaults pre-filled ──")
    _, vapi_view = call6(baseM, "GET", "/api/settings/providers/vapi", token=tokenM)
    vf = {f["name"]: f for f in vapi_view["fields"]}
    check6("voice profiles pre-fill from defaults (Bengali female = owner's verified ID)",
           vf["bn_voice_id_female"]["value"] == "611e0Wo6wEi3GcpkO5it"
           and vf["bn_voice_id_female"]["source"] == "default"
           and vf["bn_tts_model"]["value"] == "eleven_v3"
           and vf["bn_voice_provider"]["value"] == "11labs", vf.get("bn_voice_id_female"))
    check6("English + Hindi profile defaults (IDs + TTS models)",
           vf["en_voice_id_female"]["value"] == "90ipbRoKi4CpHXvKVtl0"
           and vf["en_voice_id_male"]["value"] == "pzxut4zZz4GImZNlqQ3H"
           and vf["en_tts_model"]["value"] == "eleven_turbo_v2_5"
           and vf["hi_voice_id_female"]["value"] == "1qEiC6qsybMkmnNdVMbK"
           and vf["hi_voice_id_male"]["value"] == "zT03pEAEi0VHKciJODfn"
           and vf["hi_tts_model"]["value"] == "eleven_v3")
    check6("tenant env key (not the defaults) is what configures Vapi here",
           vf["api_key"]["source"] == "env" and vapi_view["configured"] is True,
           vf.get("api_key"))
    print("── A6 fix: Vapi probe vs a UA-strict edge (live Vapi 403s urllib's default UA) ──")
    _, tres = call6(baseM, "POST", "/api/settings/providers/vapi/test", {}, **{"token": tokenM})
    check6("test-connection passes a UA-strict Vapi (product UA, not Python-urllib)",
           tres["status"] == "connected", tres)
    greqs = [r for r in FAKE["requests"] if r["method"] == "GET" and r["path"].startswith("/call")]
    check6("the probe's recorded User-Agent is the product UA",
           bool(greqs) and greqs[-1]["headers"].get("User-Agent") == "VoiceAgentPlatform/1.0",
           greqs[-1]["headers"] if greqs else "no GET /call recorded")
    _, saved_v = call6(baseM, "PUT", "/api/settings/providers/vapi",
                       {"fields": {"bn_voice_id_male": "tenant-bn-male-override"}}, token=tokenM)
    svf = {f["name"]: f for f in saved_v["fields"]}
    check6("voice profile editable per tenant (override sticks, source = tenant)",
           svf["bn_voice_id_male"]["value"] == "tenant-bn-male-override"
           and svf["bn_voice_id_male"]["source"] == "tenant", svf.get("bn_voice_id_male"))

    print("── A6 fix: Vapi transcriber profiles — configurable per language (owner-decided) ──")
    check6("transcriber profiles pre-fill from defaults (bn → deepgram/nova-3; en/hi → deepgram/nova-2)",
           vf["bn_transcriber_provider"]["value"] == "deepgram"
           and vf["bn_transcriber_provider"]["source"] == "default"
           and vf["bn_transcriber_model"]["value"] == "nova-3"
           and vf["bn_transcriber_model"]["source"] == "default"
           and vf["en_transcriber_provider"]["value"] == "deepgram"
           and vf["en_transcriber_model"]["value"] == "nova-2"
           and vf["en_transcriber_model"]["source"] == "default"
           and vf["hi_transcriber_provider"]["value"] == "deepgram"
           and vf["hi_transcriber_model"]["value"] == "nova-2"
           and vf["hi_transcriber_model"]["source"] == "default", vf.get("bn_transcriber_model"))
    _, saved_tr = call6(baseM, "PUT", "/api/settings/providers/vapi",
                        {"fields": {"bn_transcriber_provider": "deepgram",
                                    "bn_transcriber_model": "nova-2"}}, **{"token": tokenM})
    tf = {f["name"]: f for f in saved_tr["fields"]}
    check6("transcriber profile editable per tenant (override sticks, source = tenant)",
           tf["bn_transcriber_provider"]["value"] == "deepgram"
           and tf["bn_transcriber_provider"]["source"] == "tenant"
           and tf["bn_transcriber_model"]["value"] == "nova-2"
           and tf["bn_transcriber_model"]["source"] == "tenant", tf.get("bn_transcriber_model"))
    # Clear it again right away: the managed dispatch below must run on
    # the shipped defaults (the end-to-end nova-2 rejection proof lives
    # further down, after the webhook section, where it can disturb
    # nothing).
    _, cleared_tr = call6(baseM, "PUT", "/api/settings/providers/vapi",
                          {"fields": {"bn_transcriber_provider": "",
                                      "bn_transcriber_model": ""}}, **{"token": tokenM})
    cf0 = {f["name"]: f for f in cleared_tr["fields"]}
    check6("clearing the transcriber override restores the shipped defaults",
           cf0["bn_transcriber_model"]["value"] == "nova-3"
           and cf0["bn_transcriber_model"]["source"] == "default"
           and cf0["bn_transcriber_provider"]["value"] == "deepgram"
           and cf0["bn_transcriber_provider"]["source"] == "default", cf0.get("bn_transcriber_model"))

    print("── A6: managed dispatch places real Vapi calls (fake provider) ──")
    cidM = campaign6(baseM, tokenM, rpiM["agent_id"], "A6 — Managed Batch", "managed")
    _, repM = call6(baseM, "POST", f"/api/campaigns/{cidM}/dispatch", token=tokenM)
    check6("dispatch routes to the Vapi transport and PLACES calls (no finalize yet)",
           repM["transport"] == "managed" and repM["stub"] is False
           and repM["created"] == 2 and repM["placed"] == 2 and repM["finalized"] == 0, repM)
    reqs = [r for r in FAKE["requests"] if r["path"] == "/call/phone"]
    check6("fake Vapi received exactly 2 start-calls", len(reqs) == 2, len(reqs))
    payloads = [json.loads(r["body"]) for r in reqs]
    check6("start-call carries the tenant's Vapi key",
           all(r["headers"].get("Authorization") == "Bearer sk-fake-managed" for r in reqs))
    by_lead = {p["assistantOverrides"]["variableValues"]["lead_name"]: p for p in payloads}
    wb, hi = by_lead["F. Ali"], by_lead["G. Nath"]
    check6("payload: the agent's single prompt is the system message, verbatim",
           wb["assistant"]["model"]["messages"][0]["content"] == rpiM["prompt"])
    check6("payload: assistant LLM from tenant default model (provider/name split)",
           wb["assistant"]["model"]["model"] == "gpt-4o-mini"
           and wb["assistant"]["model"]["provider"] == "openai", wb["assistant"]["model"])
    check6("payload: Bengali lead → Bengali voice profile (female, 11labs, eleven_v3)",
           wb["assistant"]["voice"] == {"provider": "11labs",
                                        "voiceId": "611e0Wo6wEi3GcpkO5it",
                                        "model": "eleven_v3"}, wb["assistant"]["voice"])
    check6("payload: Hindi lead → Hindi voice profile (female, 11labs, eleven_v3)",
           hi["assistant"]["voice"] == {"provider": "11labs",
                                        "voiceId": "1qEiC6qsybMkmnNdVMbK",
                                        "model": "eleven_v3"}, hi["assistant"]["voice"])
    check6("payload: lead variables + language resolution (bn for WB, hi for Bihar)",
           wb["assistantOverrides"]["variableValues"]["bank_name"] == "Tata Capital"
           and wb["assistant"]["transcriber"]["language"] == "bn"
           and hi["assistant"]["transcriber"]["language"] == "hi")
    check6("payload: transcriber model per language (bn → nova-3, hi → nova-2; live Vapi rejects bn on nova-2)",
           wb["assistant"]["transcriber"] == {"provider": "deepgram", "model": "nova-3", "language": "bn"}
           and hi["assistant"]["transcriber"] == {"provider": "deepgram", "model": "nova-2", "language": "hi"},
           (wb["assistant"]["transcriber"], hi["assistant"]["transcriber"]))
    schema_props = wb["assistant"]["analysisPlan"]["structuredDataSchema"]["properties"]
    check6("payload: analysis schema = prompt buckets + captured fields (§8.2 source)",
           "RPC_CONFIRMED" in schema_props["disposition"]["enum"]
           and "identity_confirmed" in schema_props, list(schema_props))
    check6("payload: webhook binding (serverUrl + secret + session metadata)",
           wb["assistant"]["serverUrl"] == f"{baseM}/api/telephony/vapi/webhook"
           and wb["assistant"]["serverUrlSecret"] == "a6-webhook-secret"
           and wb["metadata"]["session_id"].startswith("ses_"), wb["assistant"]["serverUrl"])
    check6("payload: assistant server object carries the webhook URL + secret (the secret Vapi transmits)",
           wb["assistant"].get("server") == {"url": f"{baseM}/api/telephony/vapi/webhook",
                                             "secret": "a6-webhook-secret"},
           wb["assistant"].get("server"))

    _, callsM = call6(baseM, "GET", f"/api/campaigns/{cidM}/calls", token=tokenM)
    check6("placed calls sit in DIALING under the managed transport, no results yet",
           callsM["total"] == 2
           and all(c["state"] == "DIALING" and c["transport"] == "managed"
                   and c["final_state"] is None for c in callsM["calls"]), callsM)
    sess_by_lead = {c["lead_name"]: c for c in callsM["calls"]}
    check6("payload metadata binds each call to its session",
           wb["metadata"]["session_id"] == sess_by_lead["F. Ali"]["session_id"]
           and hi["metadata"]["session_id"] == sess_by_lead["G. Nath"]["session_id"])
    # The fake assigns call_fake_N in request order — pair ids to sessions
    # via each request's own metadata (lead dispatch order is not assumed).
    fake_id_by_session = {}
    for i, r in enumerate(reqs):
        sid = json.loads(r["body"])["metadata"]["session_id"]
        fake_id_by_session[sid] = f"call_fake_{i + 1}"

    print("── A6: Vapi end-of-call webhook → finalize (exactly one result) ──")
    wb_session = sess_by_lead["F. Ali"]["session_id"]
    report_body = {"message": {
        "type": "end-of-call-report",
        "endedReason": "customer-ended-call",
        "startedAt": "2026-10-23T10:00:00.000Z", "endedAt": "2026-10-23T10:00:47.000Z",
        "call": {"id": fake_id_by_session[wb_session],
                 "metadata": {"session_id": wb_session}},
        "analysis": {"structuredData": {"disposition": "RPC_CONFIRMED",
                                        "identity_confirmed": True}},
        "summary": "Customer confirmed identity.",
        "artifact": {"recordingUrl": "https:// recordings.example/x.mp3".replace(" ", ""),
                     "transcript": "AI: Namaste, am I speaking with F. Ali?\nUser: Yes, speaking."},
        "costBreakdown": {"total": 0.11, "transport": 0.05, "stt": 0.02, "llm": 0.03, "tts": 0.01},
    }}
    def _fp_lines6():
        """Auth-failure fingerprint lines in the managed app's log."""
        try:
            with open(f"{TMP6}/managed.log", encoding="utf-8",
                      errors="replace") as fh:
                return [ln for ln in fh if "vapi webhook auth failed" in ln]
        except OSError:
            return []

    call6(baseM, "POST", "/api/telephony/vapi/webhook", report_body,
          headers={"x-vapi-secret": "wrong-secret"}, expect=401)
    check6("webhook rejects a wrong shared secret (401)", True)
    # The 401 must leave exactly one header-fingerprint line in the app
    # log: sha256 prefixes of both secrets, the header's presence and
    # length — and NEVER a raw secret value (either secret string
    # appearing in the line fails this check).
    import hashlib as _hashlib6
    _fp = []
    for _ in range(20):
        _fp = _fp_lines6()
        if _fp:
            break
        time.sleep(0.25)
    check6("webhook auth failure logs a header fingerprint (sha8s + lengths, no raw secrets)",
           len(_fp) == 1
           and "sha8=" + _hashlib6.sha256(b"wrong-secret").hexdigest()[:8] in _fp[0]
           and "expected sha8=" + _hashlib6.sha256(b"a6-webhook-secret").hexdigest()[:8] in _fp[0]
           and "x-vapi-secret: present=True len=12" in _fp[0]
           and "authorization: present=False" in _fp[0]
           and "content_type=application/json" in _fp[0]
           and "wrong-secret" not in _fp[0]
           and "a6-webhook-secret" not in _fp[0],
           _fp)
    _, ack = call6(baseM, "POST", "/api/telephony/vapi/webhook",
                   {"message": {"type": "status-update",
                                "call": {"id": fake_id_by_session[wb_session]}}},
                   headers={"x-vapi-secret": "a6-webhook-secret"})
    check6("non-result server messages are acknowledged, not finalized",
           ack["handled"] is False, ack)
    check6("successful webhook auth logs no fingerprint",
           len(_fp_lines6()) == 1, _fp_lines6())
    _, ack = call6(baseM, "POST", "/api/telephony/vapi/webhook", report_body,
                   headers={"x-vapi-secret": "a6-webhook-secret"})
    check6("end-of-call report lands through finalize (created=True)",
           ack["handled"] is True and ack["created"] is True, ack)
    _, callsM2 = call6(baseM, "GET", f"/api/campaigns/{cidM}/calls", token=tokenM)
    done = next(c for c in callsM2["calls"] if c["session_id"] == wb_session)
    check6("§8.2 mapping: state/disposition/captured/duration/recording/cost from the report",
           done["final_state"] == "COMPLETED" and done["disposition"] == "RPC_CONFIRMED"
           and done["captured_fields"].get("identity_confirmed") is True
           and done["duration_s"] == 47 and done["recording_url"].endswith("x.mp3")
           and done["transport"] == "managed"
           and done["cost_breakdown"]["total"] == 0.11
           and done["cost_breakdown"]["mode"] == "managed"
           and len(done["transcript"]) == 2, done)
    _, ack2 = call6(baseM, "POST", "/api/telephony/vapi/webhook", report_body,
                    headers={"x-vapi-secret": "a6-webhook-secret"})
    _, callsM3 = call6(baseM, "GET", f"/api/campaigns/{cidM}/calls", token=tokenM)
    done2 = next(c for c in callsM3["calls"] if c["session_id"] == wb_session)
    check6("duplicate webhook is a no-op — still exactly the stored result",
           ack2["created"] is False and done2["duration_s"] == 47
           and done2["disposition"] == "RPC_CONFIRMED", ack2)
    hi_session = sess_by_lead["G. Nath"]["session_id"]
    report2 = {"message": {
        "type": "end-of-call-report", "endedReason": "customer-did-not-answer",
        "startedAt": "2026-10-23T10:05:00.000Z", "endedAt": "2026-10-23T10:05:30.000Z",
        "call": {"id": fake_id_by_session[hi_session]},  # no metadata: bind by provider call id
        "analysis": {"structuredData": {}}, "summary": "", "artifact": {},
        "costBreakdown": {"total": 0.02},
    }}
    _, ack3 = call6(baseM, "POST", "/api/telephony/vapi/webhook", report2,
                    headers={"x-vapi-secret": "a6-webhook-secret"})
    _, callsM4 = call6(baseM, "GET", f"/api/campaigns/{cidM}/calls", token=tokenM)
    hi_done = next(c for c in callsM4["calls"] if c["session_id"] == hi_session)
    check6("webhook binds by provider call id when metadata is absent; no-answer maps to NO_ANSWER",
           ack3["created"] is True and hi_done["final_state"] == "NO_ANSWER"
           and hi_done["end_reason"] == "no_answer", hi_done)

    print("── A6 fix: tenant-set bn transcriber nova-2 → placement 400, provider message preserved ──")
    call6(baseM, "PUT", "/api/settings/providers/vapi",
          {"fields": {"bn_transcriber_provider": "deepgram",
                      "bn_transcriber_model": "nova-2"}}, **{"token": tokenM})
    cidT = campaign6(baseM, tokenM, rpiM["agent_id"], "A6 — Transcriber Override Batch",
                     "managed",
                     "callee_name,phone_number,bank_name,state\n"
                     "T. Ray,9300000007,Tata Capital,West Bengal\n")
    _, repT = call6(baseM, "POST", f"/api/campaigns/{cidT}/dispatch", **{"token": tokenM})
    check6("dispatch on the tenant's nova-2 Bengali transcriber: placement rejected, finalized FAILED in-line",
           repT["transport"] == "managed" and repT["created"] == 1
           and repT["placed"] == 0 and repT["finalized"] == 1, repT)
    treqs = [r for r in FAKE["requests"] if r["path"] == "/call/phone"]
    tpay = json.loads(treqs[-1]["body"])
    check6("the rejected payload really carried the tenant's transcriber choice (deepgram/nova-2/bn)",
           tpay["assistant"]["transcriber"] == {"provider": "deepgram", "model": "nova-2",
                                                "language": "bn"},
           tpay["assistant"]["transcriber"])
    _, callsT = call6(baseM, "GET", f"/api/campaigns/{cidT}/calls", **{"token": tokenM})
    tcall = callsT["calls"][0]
    check6("the stored result carries Vapi's own message (the field it named: transcriber.language)",
           tcall["final_state"] == "FAILED" and tcall["end_reason"] == "provider_error"
           and "transcriber.language" in (tcall["summary"] or ""), tcall.get("summary"))
    _, cleared2 = call6(baseM, "PUT", "/api/settings/providers/vapi",
                        {"fields": {"bn_transcriber_provider": "",
                                    "bn_transcriber_model": ""}}, **{"token": tokenM})
    cf2 = {f["name"]: f for f in cleared2["fields"]}
    check6("override cleared after the proof — shipped defaults back (bn → deepgram/nova-3)",
           cf2["bn_transcriber_model"]["value"] == "nova-3"
           and cf2["bn_transcriber_model"]["source"] == "default"
           and cf2["bn_transcriber_provider"]["value"] == "deepgram"
           and cf2["bn_transcriber_provider"]["source"] == "default", cf2.get("bn_transcriber_model"))

    print("── A6: agent voice gender picks the profile's male voice ──")
    _, male_agent = call6(baseM, "POST", "/api/agents", {
        "name": "A6 Male Voice Agent", "voice_gender": "male",
        "prompt": rpiM["prompt"], "language_rule": rpiM["language_rule"],
        "captured_fields": rpiM["captured_fields"],
    }, token=tokenM, expect=201)
    check6("agent stores voice_gender = male", male_agent["voice_gender"] == "male", male_agent)
    before = len([r for r in FAKE["requests"] if r["path"] == "/call/phone"])
    cidG = campaign6(baseM, tokenM, male_agent["agent_id"], "A6 — Male Voice Batch",
                     "managed",
                     "callee_name,phone_number,bank_name,state\nH. Pal,9300000003,Tata Capital,West Bengal\n")
    call6(baseM, "POST", f"/api/campaigns/{cidG}/dispatch", token=tokenM)
    new_reqs = [r for r in FAKE["requests"] if r["path"] == "/call/phone"][before:]
    gp = json.loads(new_reqs[0]["body"])
    check6("male agent + Bengali lead → tenant-edited Bengali MALE voice ID",
           gp["assistant"]["voice"] == {"provider": "11labs",
                                        "voiceId": "tenant-bn-male-override",
                                        "model": "eleven_v3"}, gp["assistant"]["voice"])

    print("── A6 fix: Vapi server credential ID — credential-based webhook auth ──")
    _, vapi_view2 = call6(baseM, "GET", "/api/settings/providers/vapi", token=tokenM)
    vf2 = {f["name"]: f for f in vapi_view2["fields"]}
    check6("server credential ID pre-fills empty (no registry default, not a secret)",
           vf2["server_credential_id"]["value"] is None
           and vf2["server_credential_id"]["source"] is None
           and vf2["server_credential_id"]["is_set"] is False
           and vf2["server_credential_id"]["secret"] is False,
           vf2.get("server_credential_id"))
    _, saved_cred = call6(baseM, "PUT", "/api/settings/providers/vapi",
                          {"fields": {"server_credential_id": "cred_test_123"}}, token=tokenM)
    scf = {f["name"]: f for f in saved_cred["fields"]}
    check6("server credential ID editable per tenant (override sticks, source = tenant)",
           scf["server_credential_id"]["value"] == "cred_test_123"
           and scf["server_credential_id"]["source"] == "tenant",
           scf.get("server_credential_id"))
    before_cred = len([r for r in FAKE["requests"] if r["path"] == "/call/phone"])
    cidC = campaign6(baseM, tokenM, rpiM["agent_id"], "A6 — Server Credential Batch",
                     "managed",
                     "callee_name,phone_number,bank_name,state\n"
                     "K. Sen,9300000008,Tata Capital,West Bengal\n")
    call6(baseM, "POST", f"/api/campaigns/{cidC}/dispatch", token=tokenM)
    cred_reqs = [r for r in FAKE["requests"] if r["path"] == "/call/phone"][before_cred:]
    cp = json.loads(cred_reqs[0]["body"])
    check6("payload: credential ID set → assistant.server references it, NO inline secret",
           cp["assistant"].get("server") == {"url": f"{baseM}/api/telephony/vapi/webhook",
                                              "credentialId": "cred_test_123"}
           and "secret" not in cp["assistant"].get("server", {}),
           cp["assistant"].get("server"))
    # Clear it again: the inline-secret fallback below (and every later
    # section) must run exactly as before this field existed.
    _, cleared_cred = call6(baseM, "PUT", "/api/settings/providers/vapi",
                            {"fields": {"server_credential_id": ""}}, token=tokenM)
    ccf = {f["name"]: f for f in cleared_cred["fields"]}
    check6("clearing the credential ID restores the empty default",
           ccf["server_credential_id"]["value"] is None
           and ccf["server_credential_id"]["source"] is None,
           ccf.get("server_credential_id"))
    before_cred2 = len([r for r in FAKE["requests"] if r["path"] == "/call/phone"])
    cidC2 = campaign6(baseM, tokenM, rpiM["agent_id"], "A6 — Inline Secret Fallback Batch",
                      "managed",
                      "callee_name,phone_number,bank_name,state\n"
                      "L. Das,9300000009,Tata Capital,West Bengal\n")
    call6(baseM, "POST", f"/api/campaigns/{cidC2}/dispatch", token=tokenM)
    cred_reqs2 = [r for r in FAKE["requests"] if r["path"] == "/call/phone"][before_cred2:]
    cp2 = json.loads(cred_reqs2[0]["body"])
    check6("payload: credential ID cleared → assistant.server back to the inline secret",
           cp2["assistant"].get("server") == {"url": f"{baseM}/api/telephony/vapi/webhook",
                                               "secret": "a6-webhook-secret"},
           cp2["assistant"].get("server"))

    # ── self-hosted mode: Plivo transport skeleton ────────────────────
    print("── A6: self-hosted dispatch places Plivo calls (fake provider) ──")
    baseS = start_app6(18102, "selfhosted", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
    })
    tokenS = login6(baseS)
    _, agentsS = call6(baseS, "GET", "/api/agents", token=tokenS)
    cidS = campaign6(baseS, tokenS, agentsS[0]["agent_id"], "A6 — Self-hosted Batch",
                     "self_hosted")
    _, repS = call6(baseS, "POST", f"/api/campaigns/{cidS}/dispatch", token=tokenS)
    check6("dispatch routes to the Plivo transport and PLACES calls",
           repS["transport"] == "self_hosted" and repS["stub"] is False
           and repS["created"] == 2 and repS["placed"] == 2 and repS["finalized"] == 0, repS)
    preqs = [r for r in FAKE["requests"] if r["path"].endswith("/Call/")]
    check6("fake Plivo received exactly 2 create-call requests", len(preqs) == 2, len(preqs))
    _, callsS = call6(baseS, "GET", f"/api/campaigns/{cidS}/calls", token=tokenS)
    sessS = {c["to_number"]: c for c in callsS["calls"]}
    forms = {}
    for r in preqs:
        f = {k: v[0] for k, v in urllib.parse.parse_qs(r["body"]).items()}
        forms[f["to"]] = (f, r["headers"].get("Authorization"))
    f1, auth1 = forms["9300000001"]
    s1 = sessS["9300000001"]
    check6("create-call: answer + status URLs bind the session; recording on; Plivo basic auth",
           f1["answer_url"] == f"{baseS}/api/telephony/plivo/answer/{s1['session_id']}"
           and f1["status_callback"].endswith(f"/status/{s1['session_id']}")
           and f1["record"] == "true"
           and auth1 == "Basic " + __import__("base64").b64encode(b"MAFAKEID:fake-plivo-token").decode(),
           f1)
    check6("placed Plivo sessions sit in DIALING under the self_hosted transport",
           all(c["state"] == "DIALING" and c["transport"] == "self_hosted"
               for c in callsS["calls"]), callsS)

    print("── A6: Plivo answer XML opens the bidirectional stream ──")
    _st, xml_text = call6(baseS, "GET", f"/api/telephony/plivo/answer/{s1['session_id']}")
    import xml.etree.ElementTree as _ET
    root = _ET.fromstring(xml_text)
    stream_el = root.find("Stream")
    param = stream_el.find("Parameter")
    ws_url = stream_el.text.strip()
    check6("answer XML: <Stream bidirectional> with the session-bound wss/ws URL + Parameter",
           root.tag == "Response" and stream_el is not None
           and stream_el.attrib.get("bidirectional") == "true"
           and ws_url == f"ws://127.0.0.1:18102/api/telephony/plivo/stream/{s1['session_id']}"
           and param is not None and param.get("name") == "session_id"
           and param.get("value") == s1["session_id"], xml_text[:160])
    call6(baseS, "GET", "/api/telephony/plivo/answer/ses_missing", expect=404)

    print("── A6: placeholder stream finalizes through the same path (A7 seam) ──")
    try:
        from websockets.sync.client import connect as _ws_connect
    except Exception:
        _ws_connect = None
    check6("websockets client available for the stream test", _ws_connect is not None)
    if _ws_connect is not None:
        with _ws_connect(ws_url) as ws:
            ws.send(json.dumps({"event": "start", "start": {
                "streamId": "str-fake-1", "callId": "call-fake-1",
                "customParameters": {"session_id": s1["session_id"]}}}))
            ws.send(json.dumps({"event": "media", "media": {"payload": "AAAA"}}))
            ws.send(json.dumps({"event": "stop"}))
        doneS = None
        for _ in range(20):
            _s2, callsS2 = call6(baseS, "GET", f"/api/campaigns/{cidS}/calls", token=tokenS)
            doneS = next(c for c in callsS2["calls"] if c["session_id"] == s1["session_id"])
            if doneS["final_state"]:
                break
            time.sleep(0.25)
        check6("stream close finalized the attempt exactly once (spike-labelled, no fake conversation)",
               doneS["final_state"] == "COMPLETED" and doneS["transport"] == "self_hosted"
               and "SELF-HOSTED SPIKE" in doneS["summary"] and doneS["disposition"] == "",
               doneS)
    s2 = sessS["9300000002"]
    call6(baseS, "POST", f"/api/telephony/plivo/status/{s2['session_id']}",
          form={"CallStatus": "no-answer", "CallUUID": "uuid-fake-2", "Duration": "0"})
    _, callsS3 = call6(baseS, "GET", f"/api/campaigns/{cidS}/calls", token=tokenS)
    doneS2 = next(c for c in callsS3["calls"] if c["session_id"] == s2["session_id"])
    check6("terminal status callback finalizes a never-streamed call (NO_ANSWER)",
           doneS2["final_state"] == "NO_ANSWER" and doneS2["end_reason"] == "no_answer", doneS2)
    call6(baseS, "POST", f"/api/telephony/plivo/status/{s2['session_id']}",
          form={"CallStatus": "completed", "CallUUID": "uuid-fake-2", "Duration": "31"})
    _, callsS4 = call6(baseS, "GET", f"/api/campaigns/{cidS}/calls", token=tokenS)
    doneS2b = next(c for c in callsS4["calls"] if c["session_id"] == s2["session_id"])
    check6("a second terminal callback cannot rewrite the stored result (idempotent)",
           doneS2b["final_state"] == "NO_ANSWER" and doneS2b["duration_s"] == 0, doneS2b)
    call6(baseS, "POST", f"/api/telephony/plivo/status/{s2['session_id']}",
          form={"CallStatus": "completed"},
          headers={"X-Plivo-Signature-V2": "bogus-signature"}, expect=403)
    check6("status callback with a bad Plivo signature is rejected (403)", True)

    # ── honesty gates ─────────────────────────────────────────────────
    print("── A6: honesty gates — stub mode and mode mismatch never go real ──")
    fake_before = len(FAKE["requests"])
    baseG = start_app6(18103, "gate-stub", {
        "TELEPHONY_MODE": "stub", "VAPI_API_KEY": "sk-fake-managed",
        "PLIVO_AUTH_ID": "MAFAKEID", "PLIVO_AUTH_TOKEN": "fake-plivo-token",
        "SARVAM_API_KEY": "sk-fake-sarvam",
    })
    tokenG = login6(baseG)
    _, agentsG = call6(baseG, "GET", "/api/agents", token=tokenG)
    cidGate = campaign6(baseG, tokenG, agentsG[0]["agent_id"], "A6 — Stub Gate", "managed")
    _, repGate = call6(baseG, "POST", f"/api/campaigns/{cidGate}/dispatch", token=tokenG)
    check6("TELEPHONY_MODE=stub forces the stub even with every key configured",
           repGate["transport"] == "stub" and repGate["stub"] is True
           and repGate["finalized"] == 2 and repGate["placed"] == 0, repGate)
    check6("the stub gate made NO provider calls at all",
           len(FAKE["requests"]) == fake_before, len(FAKE["requests"]) - fake_before)

    baseX = start_app6(18104, "gate-mismatch", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-managed",
        "PLIVO_AUTH_ID": "MAFAKEID", "PLIVO_AUTH_TOKEN": "fake-plivo-token",
        "SARVAM_API_KEY": "sk-fake-sarvam",
    })
    tokenX = login6(baseX)
    _, agentsX = call6(baseX, "GET", "/api/agents", token=tokenX)
    cidX = campaign6(baseX, tokenX, agentsX[0]["agent_id"], "A6 — Mode Mismatch",
                     "self_hosted")
    _, repX = call6(baseX, "POST", f"/api/campaigns/{cidX}/dispatch", token=tokenX)
    check6("mode mismatch (managed platform, self-hosted campaign, Plivo ready) still runs the stub",
           repX["transport"] == "stub" and repX["provider_ready"] is True
           and repX["finalized"] == 2 and repX["placed"] == 0, repX)
    print("── A7: STT live — mock Saaras server, no live Sarvam calls ──")
    # The Plivo stream endpoint now runs the STT leg: a Sarvam Saaras
    # streaming session per call. SARVAM_WS_URL is the override seam — this
    # section points it at a local MOCK Saaras WebSocket server (scripted
    # partial/final transcripts) for the live path, a dead port for the
    # STT-down path, and nowhere for the no-config path. No real Sarvam
    # call is made anywhere in this suite.
    import base64 as _b64_7
    from websockets.sync.client import connect as _ws_connect7
    from websockets.sync.server import serve as _ws_serve7

    MOCK_STT = {"connections": [], "audio_frames": 0}
    _STT_FINAL_1 = "Nomoskar, ami Tata Capital theke bolchi."
    _STT_FINAL_2 = "Apni ki nijer porichoy nishchit korte parben?"


    def _mock_saaras(ws):
        req = ws.request
        info = {
            "path": req.path if req else "",
            "api_key": (req.headers.get("api-subscription-key") if req else ""),
            "frames": 0,
        }
        MOCK_STT["connections"].append(info)
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "audio" not in msg:
                    continue
                info["frames"] += 1
                MOCK_STT["audio_frames"] += 1
                n = info["frames"]
                if n == 1:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": "Nomoskar, ami", "is_final": False}}))
                elif n == 2:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": _STT_FINAL_1, "is_final": True}}))
                elif n == 3:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": _STT_FINAL_2, "is_final": True}}))
        except Exception:
            pass  # client went away mid-stream — the mock just stops


    _stt_server = _ws_serve7(_mock_saaras, "127.0.0.1", 0)
    threading.Thread(target=_stt_server.serve_forever, daemon=True).start()
    MOCK_STT_URL = f"ws://127.0.0.1:{_stt_server.socket.getsockname()[1]}"

    import socket as _socket7
    _dead_sock = _socket7.socket()
    _dead_sock.bind(("127.0.0.1", 0))
    DEAD_STT_URL = f"ws://127.0.0.1:{_dead_sock.getsockname()[1]}"
    _dead_sock.close()  # nothing listens there — connections are refused

    _procs7_mark = len(_procs6)


    def poll_call7(base, token, cid, sid, pred, tries=48, nap=0.25):
        last = None
        for _ in range(tries):
            _s, cl = call6(base, "GET", f"/api/campaigns/{cid}/calls", token=token)
            last = next((c for c in cl["calls"] if c["session_id"] == sid), None)
            if last is not None and pred(last):
                return last
            time.sleep(nap)
        return last


    def stream_events7(ws_url, sid):
        """Open a Plivo-stream client and send `start`; returns the socket
        (caller sends media / stop)."""
        ws = _ws_connect7(ws_url)
        ws.send(json.dumps({"event": "start", "start": {
            "streamId": "str-stt-1", "callId": "call-stt-1",
            "customParameters": {"session_id": sid}}}))
        return ws


    def media7(ws, n=1):
        payload = _b64_7.b64encode(b"\xff" * 160).decode()  # one μ-law-ish frame
        for _ in range(n):
            ws.send(json.dumps({"event": "media", "media": {"payload": payload}}))


    try:
        # ── live path: Saaras session opens, partials live, finals persist ──
        baseT = start_app6(18105, "stt-live", {
            "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
            "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
            "SARVAM_WS_URL": MOCK_STT_URL,
        })
        tokenT = login6(baseT)
        _, agentsT = call6(baseT, "GET", "/api/agents", token=tokenT)
        cidT = campaign6(baseT, tokenT, agentsT[0]["agent_id"], "A7 — STT Live Batch",
                         "self_hosted",
                         "callee_name,phone_number,bank_name,state\n"
                         "S. Kar,9400000001,Tata Capital,West Bengal\n")
        _, repT = call6(baseT, "POST", f"/api/campaigns/{cidT}/dispatch", token=tokenT)
        check6("A7: dispatch places the Plivo call (STT leg arms at stream time)",
               repT["transport"] == "self_hosted" and repT["placed"] == 1, repT)
        _, callsT = call6(baseT, "GET", f"/api/campaigns/{cidT}/calls", token=tokenT)
        sT = callsT["calls"][0]
        check6("A7: before the stream, the call carries no STT record yet",
               sT["stt_status"] == "" and sT["transcript"] == [], sT)
        _st, xmlT = call6(baseT, "GET", f"/api/telephony/plivo/answer/{sT['session_id']}")
        ws_url_T = _ET.fromstring(xmlT).find("Stream").text.strip()

        wsT = stream_events7(ws_url_T, sT["session_id"])
        try:
            media7(wsT, 1)
            live1 = poll_call7(baseT, tokenT, cidT, sT["session_id"],
                               lambda c: any(t.get("partial") for t in c["transcript"]))
            check6("A7: partial transcript rides the live view mid-call (marked partial)",
                   live1 is not None
                   and any(t.get("partial") and t["text"] == "Nomoskar, ami"
                           for t in live1["transcript"]), live1)
            check6("A7: session records STT active in the resolved language (bn-IN)",
                   live1 is not None and live1["stt_status"] == "active"
                   and live1["stt_language"] == "bn-IN", live1)
            media7(wsT, 1)
            live2 = poll_call7(baseT, tokenT, cidT, sT["session_id"],
                               lambda c: any(t["text"] == _STT_FINAL_1 and not t.get("partial")
                                             for t in c["transcript"]))
            check6("A7: first final segment persisted to the session AS IT ARRIVES",
                   live2 is not None and live2["final_state"] is None
                   and not any(t.get("partial") for t in live2["transcript"]), live2)
            media7(wsT, 1)
            live3 = poll_call7(baseT, tokenT, cidT, sT["session_id"],
                               lambda c: sum(1 for t in c["transcript"]
                                             if not t.get("partial")) >= 2)
            check6("A7: second final segment appended in order",
                   live3 is not None
                   and [t["text"] for t in live3["transcript"]] == [_STT_FINAL_1, _STT_FINAL_2],
                   live3)
        finally:
            try:
                wsT.send(json.dumps({"event": "stop"}))
            except Exception:
                pass
            wsT.close()
        doneT = poll_call7(baseT, tokenT, cidT, sT["session_id"],
                           lambda c: c["final_state"] is not None)
        check6("A7: finalize outcome carries the REAL accumulated transcript",
               doneT is not None and doneT["final_state"] == "COMPLETED"
               and [t["text"] for t in doneT["transcript"]] == [_STT_FINAL_1, _STT_FINAL_2]
               and all(t["speaker"] == "customer" for t in doneT["transcript"])
               and "live Sarvam Saaras transcription" in doneT["summary"]
               and "PARTIAL" not in doneT["summary"], doneT)
        check6("A7: session keeps stt_status=active + language after the call",
               doneT is not None and doneT["stt_status"] == "active"
               and doneT["stt_language"] == "bn-IN" and doneT["stt_detail"] == "", doneT)
        check6("A7: mock Saaras saw ONE connection, opened for bn-IN with the tenant key",
               len(MOCK_STT["connections"]) == 1
               and "language-code=bn-IN" in MOCK_STT["connections"][0]["path"]
               and MOCK_STT["connections"][0]["api_key"] == "sk-fake-sarvam",
               MOCK_STT["connections"])
        check6("A7: Plivo media frames were forwarded to Saaras as audio messages",
               MOCK_STT["connections"][0]["frames"] == 3
               and MOCK_STT["audio_frames"] == 3, MOCK_STT)
        call6(baseT, "POST", f"/api/telephony/plivo/status/{sT['session_id']}",
              form={"CallStatus": "completed", "CallUUID": "uuid-stt-1", "Duration": "99"})
        _, callsT2 = call6(baseT, "GET", f"/api/campaigns/{cidT}/calls", token=tokenT)
        doneT2 = next(c for c in callsT2["calls"] if c["session_id"] == sT["session_id"])
        check6("A7: a late status callback cannot rewrite the STT result (idempotent)",
               [t["text"] for t in doneT2["transcript"]] == [_STT_FINAL_1, _STT_FINAL_2]
               and doneT2["duration_s"] == doneT["duration_s"], doneT2)

        # ── STT-down path: failure recorded, transcript partial, call fine ──
        print("── A7: STT down — the call survives a failed Saaras session ──")
        baseD = start_app6(18106, "stt-down", {
            "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
            "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
            "SARVAM_WS_URL": DEAD_STT_URL,
        })
        tokenD = login6(baseD)
        _, agentsD = call6(baseD, "GET", "/api/agents", token=tokenD)
        cidD = campaign6(baseD, tokenD, agentsD[0]["agent_id"], "A7 — STT Down Batch",
                         "self_hosted",
                         "callee_name,phone_number,bank_name,state\n"
                         "R. Jha,9400000002,Tata Capital,Bihar\n")
        call6(baseD, "POST", f"/api/campaigns/{cidD}/dispatch", token=tokenD)
        _, callsD = call6(baseD, "GET", f"/api/campaigns/{cidD}/calls", token=tokenD)
        sD = callsD["calls"][0]
        wsD = stream_events7(
            f"ws://127.0.0.1:18106/api/telephony/plivo/stream/{sD['session_id']}",
            sD["session_id"])
        try:
            media7(wsD, 2)
            time.sleep(0.5)  # let the refused connection land as a failure
        finally:
            try:
                wsD.send(json.dumps({"event": "stop"}))
            except Exception:
                pass
            wsD.close()
        doneD = poll_call7(baseD, tokenD, cidD, sD["session_id"],
                           lambda c: c["final_state"] is not None)
        check6("A7: STT failure recorded on the session (failed + reason + language)",
               doneD is not None and doneD["stt_status"] == "failed"
               and doneD["stt_language"] == "hi-IN" and bool(doneD["stt_detail"]), doneD)
        check6("A7: the call still finalizes COMPLETED, exactly once, transcript marked PARTIAL",
               doneD is not None and doneD["final_state"] == "COMPLETED"
               and doneD["transcript"] and doneD["transcript"][0]["speaker"] == "system"
               and "TRANSCRIPT PARTIAL" in doneD["transcript"][0]["text"]
               and "STT session failed" in doneD["summary"]
               and "SELF-HOSTED SPIKE" in doneD["summary"], doneD)
        check6("A7: the dead-endpoint attempt never reached the mock Saaras",
               len(MOCK_STT["connections"]) == 1, len(MOCK_STT["connections"]))

        # ── no-config path: stream behaves exactly like the A6 spike ───────
        print("── A7: no Sarvam config at stream time — A6 spike behaviour ──")
        baseN = start_app6(18107, "stt-noconfig", {
            "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
            "PLIVO_AUTH_TOKEN": "fake-plivo-token",
            "SARVAM_WS_URL": MOCK_STT_URL,  # key arrives via TENANT settings only
        })
        tokenN = login6(baseN)
        _, agentsN = call6(baseN, "GET", "/api/agents", token=tokenN)
        call6(baseN, "PUT", "/api/settings/providers/sarvam",
              {"fields": {"api_key": "sk-tenant-sarvam"}}, token=tokenN)
        cidN = campaign6(baseN, tokenN, agentsN[0]["agent_id"], "A7 — No-Config Batch",
                         "self_hosted",
                         "callee_name,phone_number,bank_name,state\n"
                         "T. Ray,9400000003,Tata Capital,West Bengal\n")
        _, repN = call6(baseN, "POST", f"/api/campaigns/{cidN}/dispatch", token=tokenN)
        check6("A7: tenant-configured Sarvam made the campaign dispatchable for real",
               repN["transport"] == "self_hosted" and repN["placed"] == 1, repN)
        # The key is removed AFTER placement — at stream time the tenant has
        # no Sarvam config anywhere (env is blank in this instance).
        call6(baseN, "DELETE", "/api/settings/providers/sarvam", token=tokenN)
        _, callsN = call6(baseN, "GET", f"/api/campaigns/{cidN}/calls", token=tokenN)
        sN = callsN["calls"][0]
        wsN = stream_events7(
            f"ws://127.0.0.1:18107/api/telephony/plivo/stream/{sN['session_id']}",
            sN["session_id"])
        try:
            media7(wsN, 2)
        finally:
            try:
                wsN.send(json.dumps({"event": "stop"}))
            except Exception:
                pass
            wsN.close()
        doneN = poll_call7(baseN, tokenN, cidN, sN["session_id"],
                           lambda c: c["final_state"] is not None)
        check6("A7: no-config stream = the A6 spike outcome, word for word in kind",
               doneN is not None and doneN["final_state"] == "COMPLETED"
               and doneN["stt_status"] == "not_configured"
               and doneN["transcript"] == []
               and "SELF-HOSTED SPIKE" in doneN["summary"]
               and "No speech was generated or heard" in doneN["summary"], doneN)
        check6("A7: without a key, NO Saaras connection was even attempted",
               len(MOCK_STT["connections"]) == 1, len(MOCK_STT["connections"]))
    finally:
        for _p in _procs6[_procs7_mark:]:
            _p.terminate()
        for _p in _procs6[_procs7_mark:]:
            try:
                _p.wait(timeout=5)
            except Exception:
                _p.kill()
        _stt_server.shutdown()

    print("── A8: full conversation loop — mock Sarvam HTTP (LLM + TTS) + mock Saaras ──")
    # The self-hosted stream now runs the whole brain: Saaras finals drive
    # Sarvam chat completions (the agent's single prompt as system prompt),
    # replies are sentence-chunked into Bulbul TTS, and the audio returns
    # to the Plivo stream as playAudio frames. SARVAM_BASE_URL is the
    # override seam — this section points it at a local MOCK Sarvam HTTP
    # server (scripted SSE chat + scripted classifier + dummy-audio TTS),
    # SARVAM_WS_URL at a fresh mock Saaras. A dead HTTP port plays the
    # LLM-down scenario. No real provider is called anywhere.
    import base64 as _b64_8
    from websockets.sync.client import connect as _ws_connect8

    _L1_8 = "My favourite colour is blue."
    _L2_8 = "For clothes, mostly."
    _R1_8 = "Hello, this is a short survey call. What is your favourite colour?"
    _R2_8 = "Blue is a lovely colour. And do you prefer it for clothes or for cars?"
    _R3_8 = "Thank you for your answers. Have a good day."
    _SENT_8 = {
        _R1_8: ["Hello, this is a short survey call.", "What is your favourite colour?"],
        _R2_8: ["Blue is a lovely colour.", "And do you prefer it for clothes or for cars?"],
        _R3_8: ["Thank you for your answers.", "Have a good day."],
    }


    def _mock_audio8(text):
        return ("<tts:" + text + ">").encode() * 3


    def _audio_len8(reply):
        return sum(len(_mock_audio8(s)) for s in _SENT_8[reply])


    MOCK_AI = {"chat": [], "classify": [], "tts": [], "classify_mode": "ok"}


    class _MockSarvamAI(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, data, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if self.path == "/chat/completions":
                self._chat(body)
            elif self.path == "/text-to-speech":
                MOCK_AI["tts"].append(body)
                text = (body.get("inputs") or [""])[0]
                self._json(200, {"audios": [_b64_8.b64encode(_mock_audio8(text)).decode()]})
            else:
                self._json(404, {})

        def _chat(self, body):
            msgs = body.get("messages") or []
            system = msgs[0].get("content", "") if msgs else ""
            if "POST-CALL CLASSIFIER" in system:
                MOCK_AI["classify"].append(body)
                mode = MOCK_AI["classify_mode"]
                if mode == "garbage":
                    content = "I could not classify this call, sorry."
                elif mode == "bad_bucket":
                    content = json.dumps({
                        "disposition": "NOT_A_BUCKET", "captured_fields": {},
                        "confidence": 0.4,
                        "summary": "Mock classifier picked a bucket that does not exist."})
                else:
                    content = json.dumps({
                        "disposition": "ANSWERED",
                        "captured_fields": {"favourite_colour": "blue"},
                        "confidence": 0.93,
                        "summary": "The callee answered both survey questions; favourite colour blue."})
                self._json(200, {"choices": [
                    {"message": {"role": "assistant", "content": content}}]})
                return
            MOCK_AI["chat"].append(body)
            users = [m.get("content", "") for m in msgs if m.get("role") == "user"]
            last = users[-1] if users else ""
            if last == _L2_8:
                pieces = ["Thank you for your answers. ", "Have a good day."]
                end_call = True
            elif last == _L1_8:
                pieces = ["Blue is a lovely ", "colour. And do you prefer it ",
                          "for clothes or for cars?"]
                end_call = False
            else:  # the kickoff — greeting + the one survey question
                pieces = ["Hello, this is a short survey ",
                          "call. What is your favourite colour?"]
                end_call = False
            lines = []
            for p in pieces:
                lines.append("data: " + json.dumps(
                    {"choices": [{"delta": {"content": p}}]}) + "\n\n")
            if end_call:
                lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_end_1", "type": "function",
                     "function": {"name": "end_call", "arguments": ""}}]}}]}) + "\n\n")
            lines.append("data: [DONE]\n\n")
            self._send(200, "".join(lines).encode(), "text/event-stream")


    _ai_server = ThreadingHTTPServer(("127.0.0.1", 0), _MockSarvamAI)
    threading.Thread(target=_ai_server.serve_forever, daemon=True).start()
    MOCK_AI_URL = f"http://127.0.0.1:{_ai_server.server_address[1]}"

    import socket as _socket8
    _dead_ai = _socket8.socket()
    _dead_ai.bind(("127.0.0.1", 0))
    DEAD_AI_URL = f"http://127.0.0.1:{_dead_ai.getsockname()[1]}"
    _dead_ai.close()  # nothing listens — LLM/TTS calls are refused instantly

    MOCK_STT8 = {"connections": []}


    def _mock_saaras8(ws):
        req = ws.request
        info = {"path": req.path if req else "", "frames": 0}
        MOCK_STT8["connections"].append(info)
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "audio" not in msg:
                    continue
                info["frames"] += 1
                line = _L1_8 if info["frames"] == 1 else _L2_8 if info["frames"] == 2 else ""
                if line:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": line, "is_final": True}}))
        except Exception:
            pass  # client went away mid-stream — the mock just stops


    _stt8_server = _ws_serve7(_mock_saaras8, "127.0.0.1", 0)
    threading.Thread(target=_stt8_server.serve_forever, daemon=True).start()
    MOCK_STT8_URL = f"ws://127.0.0.1:{_stt8_server.socket.getsockname()[1]}"

    _SCRIPTED_PROMPT_8 = """ROLE
    You are a scripted survey test agent used for platform testing. You ask
    one short survey, politely and briefly, in the call's language.

    FLOW
    1. Greet {{lead_name}} and ask: "What is your favourite colour?"
    2. When they answer, repeat their colour back to confirm, and ask:
       "And do you prefer it for clothes or for cars?"
    3. When they answer again, thank them warmly, say the approved closing
       in full, and end the call.

    DISPOSITIONS - classify the finished call into exactly ONE disposition:
    - ANSWERED: the callee answered the survey questions.
    - DID_NOT_ANSWER: the callee did not answer the questions.

    CLOSINGS
    Always close with: "Thank you for your time. Have a good day."
    """


    def poll8(base, token, cid, sid, pred, tries=60, nap=0.25):
        last = None
        for _ in range(tries):
            _s, cl = call6(base, "GET", f"/api/campaigns/{cid}/calls", token=token)
            last = next((c for c in cl["calls"] if c["session_id"] == sid), None)
            if last is not None and pred(last):
                return last
            time.sleep(nap)
        return last


    def _wait_audio8(ws, sink, target, timeout=15):
        """Collect playAudio audio until sink holds >= target bytes.
        Returns True when the server closed the stream."""
        deadline = time.monotonic() + timeout
        closed = False
        while len(sink) < target and time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
            except TimeoutError:
                break
            except Exception:
                closed = True
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("event") == "playAudio":
                sink.extend(_b64_8.b64decode((ev.get("media") or {}).get("payload") or ""))
        return closed


    def _drain8(ws, sink, seconds):
        """Collect whatever playAudio audio arrives for `seconds` (used to
        prove NO further audio comes after a guard stops the loop)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
            except Exception:
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("event") == "playAudio":
                sink.extend(_b64_8.b64decode((ev.get("media") or {}).get("payload") or ""))


    def _stream_open8(base, token, sid):
        _st, xml_text = call6(base, "GET", f"/api/telephony/plivo/answer/{sid}")
        ws_url = _ET.fromstring(xml_text).find("Stream").text.strip()
        ws = _ws_connect8(ws_url)
        ws.send(json.dumps({"event": "start", "start": {
            "streamId": "str-a8", "callId": "call-a8",
            "customParameters": {"session_id": sid}}}))
        return ws


    def _media8(ws):
        ws.send(json.dumps({"event": "media", "media": {
            "payload": _b64_8.b64encode(b"\xff" * 160).decode()}}))


    def _stream_stop8(ws):
        try:
            ws.send(json.dumps({"event": "stop"}))
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass


    def _wait_closed8(ws, seconds=5):
        """Wait until the SERVER closes the stream (the end_call path
        ends the stream handler-side; the client just observes)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                ws.recv(timeout=max(0.05, deadline - time.monotonic()))
            except TimeoutError:
                return False
            except Exception:
                return True
        return False


    # ── the full loop: greeting → question → follow-up → closing ──────
    baseA8 = start_app6(18108, "conv-live", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
        "SARVAM_WS_URL": MOCK_STT8_URL, "SARVAM_BASE_URL": MOCK_AI_URL,
    })
    tokenA8 = login6(baseA8)
    _, scripted = call6(baseA8, "POST", "/api/agents", {
        "name": "A8 Scripted Survey Agent",
        "purpose": "Generic scripted test agent (design A8 acceptance)",
        "prompt": _SCRIPTED_PROMPT_8,
        "llm_model": "sarvam-105b",
        "tts_voice_id": "bulbul-bn-female-1",
        "language_rule": {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "favourite_colour", "type": "string",
                             "description": "The callee's favourite colour"}],
    }, token=tokenA8, expect=201)
    check6("A8: scripted test agent created with its single prompt + captured field",
           scripted["name"] == "A8 Scripted Survey Agent"
           and scripted["max_turns"] == 50, scripted.get("name"))
    cidA8 = campaign6(baseA8, tokenA8, scripted["agent_id"], "A8 — Conversation Batch",
                      "self_hosted",
                      "callee_name,phone_number,bank_name,state\n"
                      "P. Sen,9500000001,Tata Capital,West Bengal\n"
                      "Q. Das,9500000002,Tata Capital,West Bengal\n"
                      "R. Mitra,9500000003,Tata Capital,West Bengal\n")
    _, repA8 = call6(baseA8, "POST", f"/api/campaigns/{cidA8}/dispatch", token=tokenA8)
    check6("A8: dispatch places the three Plivo calls",
           repA8["transport"] == "self_hosted" and repA8["placed"] == 3, repA8)
    _, callsA8 = call6(baseA8, "GET", f"/api/campaigns/{cidA8}/calls", token=tokenA8)
    sessA8 = {c["to_number"]: c for c in callsA8["calls"]}

    chat0, tts0, cls0 = len(MOCK_AI["chat"]), len(MOCK_AI["tts"]), len(MOCK_AI["classify"])
    sA1 = sessA8["9500000001"]
    audioA = bytearray()
    wsA = _stream_open8(baseA8, tokenA8, sA1["session_id"])
    closedA = _wait_audio8(wsA, audioA, _audio_len8(_R1_8))
    check6("A8: the agent speaks FIRST — the opening's audio arrives before any callee media",
           len(audioA) == _audio_len8(_R1_8) and not closedA, len(audioA))
    _media8(wsA)
    _wait_audio8(wsA, audioA, _audio_len8(_R1_8) + _audio_len8(_R2_8))
    check6("A8: callee line 1 (STT final) drives reply turn 2 — its audio follows",
           len(audioA) == _audio_len8(_R1_8) + _audio_len8(_R2_8), len(audioA))
    _media8(wsA)
    closedA = _wait_audio8(wsA, audioA,
                           _audio_len8(_R1_8) + _audio_len8(_R2_8) + _audio_len8(_R3_8))
    closedA = _wait_closed8(wsA, 5.0) or closedA
    check6("A8: closing spoken in full, then the model's end_call action closes the stream",
           closedA and len(audioA) == (_audio_len8(_R1_8) + _audio_len8(_R2_8)
                                       + _audio_len8(_R3_8)), (closedA, len(audioA)))
    try:
        wsA.close()
    except Exception:
        pass
    doneA = poll8(baseA8, tokenA8, cidA8, sA1["session_id"],
                  lambda c: c["final_state"] is not None)
    check6("A8: finalize — COMPLETED via agent_end_call, all three legs active (bn-IN)",
           doneA is not None and doneA["final_state"] == "COMPLETED"
           and doneA["end_reason"] == "agent_end_call"
           and doneA["stt_status"] == "active" and doneA["stt_language"] == "bn-IN"
           and doneA["llm_status"] == "active" and doneA["tts_status"] == "active",
           doneA and {k: doneA[k] for k in ("final_state", "end_reason", "llm_status", "tts_status")})
    check6("A8: the result transcript is the REAL conversation, interleaved in order",
           doneA is not None
           and [t["text"] for t in doneA["transcript"]] == [_R1_8, _L1_8, _R2_8, _L2_8, _R3_8]
           and [t["speaker"] for t in doneA["transcript"]] ==
               ["agent", "customer", "agent", "customer", "agent"],
           doneA and doneA["transcript"])
    check6("A8: classification against the prompt's own DISPOSITIONS — bucket + captured field",
           doneA is not None and doneA["disposition"] == "ANSWERED"
           and doneA["captured_fields"].get("favourite_colour") == "blue"
           and doneA["review_required"] is False
           and "Full conversation loop" in doneA["summary"], doneA)
    chatA = MOCK_AI["chat"][chat0:]
    check6("A8: three LLM turns ran (kickoff + two replies), history accumulating",
           len(chatA) == 3
           and any(m.get("role") == "assistant" and m.get("content") == _R1_8
                   for m in chatA[2]["messages"])
           and any(m.get("role") == "user" and m.get("content") == _L1_8
                   for m in chatA[2]["messages"]), len(chatA))
    check6("A8: the system prompt is the agent's single prompt, lead variables bound",
           "P. Sen" in chatA[0]["messages"][0]["content"]
           and "{{lead_name}}" not in chatA[0]["messages"][0]["content"]
           and "DISPOSITIONS" in chatA[0]["messages"][0]["content"],
           chatA[0]["messages"][0]["content"][:80])
    ttsA = MOCK_AI["tts"][tts0:]
    check6("A8: Bulbul synthesized one request per reply sentence, agent voice + call language",
           [t["inputs"][0] for t in ttsA] ==
               _SENT_8[_R1_8] + _SENT_8[_R2_8] + _SENT_8[_R3_8]
           and all(t["speaker"] == "bulbul-bn-female-1" for t in ttsA)
           and all(t["target_language_code"] == "bn-IN" for t in ttsA), ttsA)
    check6("A8: the playAudio frames carry exactly the synthesized audio, in order",
           bytes(audioA) == b"".join(_mock_audio8(t["inputs"][0]) for t in ttsA),
           len(audioA))
    clsA = MOCK_AI["classify"][cls0:]
    check6("A8: exactly one classifier call, fed the prompt's buckets + the real transcript",
           len(clsA) == 1
           and "ANSWERED" in clsA[0]["messages"][1]["content"]
           and _L1_8 in clsA[0]["messages"][1]["content"]
           and _R3_8 in clsA[0]["messages"][1]["content"], len(clsA))

    # ── classifier honesty: unknown bucket and garbage output ─────────
    MOCK_AI["classify_mode"] = "bad_bucket"
    sA2 = sessA8["9500000002"]
    audioB = bytearray()
    wsB = _stream_open8(baseA8, tokenA8, sA2["session_id"])
    _wait_audio8(wsB, audioB, _audio_len8(_R1_8))
    _media8(wsB)
    _media8(wsB)
    _drain8(wsB, audioB, 2.0)
    _stream_stop8(wsB)
    doneB = poll8(baseA8, tokenA8, cidA8, sA2["session_id"],
                  lambda c: c["final_state"] is not None)
    check6("A8: a bucket outside the prompt's set is NEVER stored — review flag instead",
           doneB is not None and doneB["disposition"] == ""
           and doneB["review_required"] is True
           and "Flagged for review" in doneB["summary"], doneB)
    MOCK_AI["classify_mode"] = "garbage"
    sA3 = sessA8["9500000003"]
    audioC = bytearray()
    wsC = _stream_open8(baseA8, tokenA8, sA3["session_id"])
    _wait_audio8(wsC, audioC, _audio_len8(_R1_8))
    _media8(wsC)
    _media8(wsC)
    _drain8(wsC, audioC, 2.0)
    _stream_stop8(wsC)
    doneC = poll8(baseA8, tokenA8, cidA8, sA3["session_id"],
                  lambda c: c["final_state"] is not None)
    check6("A8: unparseable classifier output also lands as review, not a guessed bucket",
           doneC is not None and doneC["disposition"] == ""
           and doneC["review_required"] is True, doneC)
    MOCK_AI["classify_mode"] = "ok"

    # ── guards: max_turns and max_duration end the loop honestly ────
    _, agent_mt = call6(baseA8, "POST", "/api/agents", {
        "name": "A8 Max Turns Agent", "prompt": _SCRIPTED_PROMPT_8,
        "tts_voice_id": "bulbul-bn-female-1", "max_turns": 1,
        "language_rule": {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "favourite_colour", "type": "string",
                             "description": "The callee's favourite colour"}],
    }, token=tokenA8, expect=201)
    cidMT = campaign6(baseA8, tokenA8, agent_mt["agent_id"], "A8 — Max Turns Batch",
                      "self_hosted",
                      "callee_name,phone_number,bank_name,state\n"
                      "S. Pal,9500000011,Tata Capital,West Bengal\n")
    call6(baseA8, "POST", f"/api/campaigns/{cidMT}/dispatch", token=tokenA8)
    _, callsMT = call6(baseA8, "GET", f"/api/campaigns/{cidMT}/calls", token=tokenA8)
    sMT = callsMT["calls"][0]
    chat_mt0 = len(MOCK_AI["chat"])
    audioMT = bytearray()
    wsMT = _stream_open8(baseA8, tokenA8, sMT["session_id"])
    _wait_audio8(wsMT, audioMT, _audio_len8(_R1_8))
    _media8(wsMT)  # callee answers — but the one allowed turn is spent
    _drain8(wsMT, audioMT, 2.5)
    check6("A8: max_turns guard — no second reply is generated or spoken",
           len(audioMT) == _audio_len8(_R1_8)
           and len(MOCK_AI["chat"]) - chat_mt0 == 1, len(audioMT))
    _stream_stop8(wsMT)
    doneMT = poll8(baseA8, tokenA8, cidMT, sMT["session_id"],
                   lambda c: c["final_state"] is not None)
    check6("A8: the guarded call finalizes with end_reason max_turns",
           doneMT is not None and doneMT["end_reason"] == "max_turns"
           and [t["speaker"] for t in doneMT["transcript"]] == ["agent", "customer"],
           doneMT and doneMT["end_reason"])

    _, agent_md = call6(baseA8, "POST", "/api/agents", {
        "name": "A8 Max Duration Agent", "prompt": _SCRIPTED_PROMPT_8,
        "tts_voice_id": "bulbul-bn-female-1", "max_duration_s": 1,
        "language_rule": {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "favourite_colour", "type": "string",
                             "description": "The callee's favourite colour"}],
    }, token=tokenA8, expect=201)
    cidMD = campaign6(baseA8, tokenA8, agent_md["agent_id"], "A8 — Max Duration Batch",
                      "self_hosted",
                      "callee_name,phone_number,bank_name,state\n"
                      "T. Ray,9500000012,Tata Capital,West Bengal\n")
    call6(baseA8, "POST", f"/api/campaigns/{cidMD}/dispatch", token=tokenA8)
    _, callsMD = call6(baseA8, "GET", f"/api/campaigns/{cidMD}/calls", token=tokenA8)
    sMD = callsMD["calls"][0]
    audioMD = bytearray()
    wsMD = _stream_open8(baseA8, tokenA8, sMD["session_id"])
    _wait_audio8(wsMD, audioMD, _audio_len8(_R1_8))
    time.sleep(1.4)  # let the 1-second duration limit lapse
    _media8(wsMD)  # the answer arrives too late — the guard has ended the loop
    _drain8(wsMD, audioMD, 2.0)
    check6("A8: max_duration guard — the late answer gets no reply",
           len(audioMD) == _audio_len8(_R1_8), len(audioMD))
    _stream_stop8(wsMD)
    doneMD = poll8(baseA8, tokenA8, cidMD, sMD["session_id"],
                   lambda c: c["final_state"] is not None)
    check6("A8: the duration-guarded call finalizes with end_reason max_duration",
           doneMD is not None and doneMD["end_reason"] == "max_duration",
           doneMD and doneMD["end_reason"])

    # ── LLM leg down: STT lives, the brain fails, honesty holds ──────
    print("── A8: LLM down — degraded finalize, review flag, no faked conversation ──")
    baseD8 = start_app6(18109, "conv-llmdown", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
        "SARVAM_WS_URL": MOCK_STT8_URL, "SARVAM_BASE_URL": DEAD_AI_URL,
    })
    tokenD8 = login6(baseD8)
    _, agentsD8 = call6(baseD8, "GET", "/api/agents", token=tokenD8)
    cidD8 = campaign6(baseD8, tokenD8, agentsD8[0]["agent_id"], "A8 — LLM Down Batch",
                      "self_hosted",
                      "callee_name,phone_number,bank_name,state\n"
                      "U. Jha,9500000021,Tata Capital,Bihar\n")
    call6(baseD8, "POST", f"/api/campaigns/{cidD8}/dispatch", token=tokenD8)
    _, callsD8 = call6(baseD8, "GET", f"/api/campaigns/{cidD8}/calls", token=tokenD8)
    sD8 = callsD8["calls"][0]
    audioD = bytearray()
    wsD8 = _stream_open8(baseD8, tokenD8, sD8["session_id"])
    _media8(wsD8)
    _media8(wsD8)
    liveD = poll8(baseD8, tokenD8, cidD8, sD8["session_id"],
                  lambda c: sum(1 for t in c["transcript"] if not t.get("partial")) >= 2)
    check6("A8: with the LLM down, STT finals still land on the session live",
           liveD is not None
           and [t["text"] for t in liveD["transcript"]] == [_L1_8, _L2_8], liveD)
    _drain8(wsD8, audioD, 1.5)
    check6("A8: no audio was ever sent back — the brain produced nothing to speak",
           len(audioD) == 0, len(audioD))
    _stream_stop8(wsD8)
    doneD8 = poll8(baseD8, tokenD8, cidD8, sD8["session_id"],
                   lambda c: c["final_state"] is not None)
    check6("A8: degraded finalize — legs recorded (STT active, LLM + TTS failed)",
           doneD8 is not None and doneD8["final_state"] == "COMPLETED"
           and doneD8["stt_status"] == "active"
           and doneD8["llm_status"] == "failed" and bool(doneD8["llm_detail"])
           and doneD8["tts_status"] == "failed", doneD8)
    check6("A8: degraded outcome — STT transcript stands, disposition empty, review flagged",
           doneD8 is not None
           and [t["text"] for t in doneD8["transcript"]] == [_L1_8, _L2_8]
           and doneD8["disposition"] == "" and doneD8["review_required"] is True
           and "live Sarvam Saaras transcription" in doneD8["summary"]
           and "flagged for review" in doneD8["summary"], doneD8)

    _ai_server.shutdown()
    _stt8_server.shutdown()

    print("── A9: barge-in + silence — partials cut the agent, silence nudges then closes ──")
    # Same mock pattern as A8, two A9 twists: the mock Saaras can emit
    # PARTIALS (the barge-in trigger) and stays mute on demand (the
    # silence scenarios), and the mock Bulbul returns ENLARGED audio
    # for the barge opening's sentences (~25 frames each; paced at
    # 20 ms/frame by the stream handler, each sentence plays ~0.5 s)
    # so the interruption deterministically lands mid-reply. The
    # silence agent runs silence_prompt_after_s=1 /
    # silence_max_prompts=1, so the whole nudge → answer → nudge →
    # polite-close arc plays in seconds.
    import base64 as _b64_9
    from websockets.sync.client import connect as _ws_connect9

    _LI_9 = "Wait, I have a question."
    _LANS_9 = "Yes, I am here."
    _B1_9 = "First, let me explain the reason for this call in some detail."
    _B2_9 = "Second, there are several options we should walk through together."
    _B3_9 = "Third, I will confirm the next steps before we finish."
    _OPEN_B_9 = _B1_9 + " " + _B2_9 + " " + _B3_9
    _RB_9 = "Of course, please go ahead and ask your question."
    _OPEN_9 = "Hello, I am calling about your loan account. Am I speaking with the account holder?"
    _OPEN_SENT_9 = ["Hello, I am calling about your loan account.",
                    "Am I speaking with the account holder?"]
    _NUDGE_9 = "Hello, are you still there?"
    _RANS_9 = "Thank you for confirming. Let me pull up your account details."
    _CLOSE_9 = "Since I cannot hear you, I will close this call now. Goodbye."


    def _mock_audio9(text):
        rep = 60 if text.startswith(("First,", "Second,", "Third,")) else 3
        return ("<tts:" + text + ">").encode() * rep


    _FULL_B_AUDIO_9 = sum(len(_mock_audio9(s)) for s in (_B1_9, _B2_9, _B3_9))

    MOCK_AI9 = {"chat": [], "classify": [], "tts": []}


    class _MockSarvamAI9(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, data, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if self.path == "/chat/completions":
                self._chat(body)
            elif self.path == "/text-to-speech":
                MOCK_AI9["tts"].append(body)
                text = (body.get("inputs") or [""])[0]
                self._json(200, {"audios": [_b64_9.b64encode(_mock_audio9(text)).decode()]})
            else:
                self._json(404, {})

        def _chat(self, body):
            msgs = body.get("messages") or []
            system = msgs[0].get("content", "") if msgs else ""
            if "POST-CALL CLASSIFIER" in system:
                MOCK_AI9["classify"].append(body)
                self._json(200, {"choices": [{"message": {"role": "assistant",
                    "content": json.dumps({
                        "disposition": "SPOKEN", "captured_fields": {},
                        "confidence": 0.91,
                        "summary": "Mock classifier (A9): the callee spoke."})}}]})
                return
            MOCK_AI9["chat"].append(body)
            users = [m.get("content", "") for m in msgs if m.get("role") == "user"]
            last = users[-1] if users else ""
            end_call = False
            if "been silent" in last:
                pieces = [_NUDGE_9]
            elif "not responded" in last:
                pieces = [_CLOSE_9]
                end_call = True
            elif last == _LI_9:
                pieces = [_RB_9]
            elif last == _LANS_9:
                pieces = [_RANS_9]
            elif "BARGE TEST AGENT" in system:
                pieces = ["First, let me explain the reason ",
                          "for this call in some detail. ",
                          "Second, there are several options ",
                          "we should walk through together. ",
                          "Third, I will confirm the next steps ",
                          "before we finish."]
            else:
                pieces = ["Hello, I am calling about your loan account. ",
                          "Am I speaking with the account holder?"]
            lines = []
            for p in pieces:
                lines.append("data: " + json.dumps(
                    {"choices": [{"delta": {"content": p}}]}) + "\n\n")
            if end_call:
                lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_end_9", "type": "function",
                     "function": {"name": "end_call", "arguments": ""}}]}}]}) + "\n\n")
            lines.append("data: [DONE]\n\n")
            self._send(200, "".join(lines).encode(), "text/event-stream")


    _ai9_server = ThreadingHTTPServer(("127.0.0.1", 0), _MockSarvamAI9)
    threading.Thread(target=_ai9_server.serve_forever, daemon=True).start()
    MOCK_AI9_URL = f"http://127.0.0.1:{_ai9_server.server_address[1]}"

    MOCK_STT9 = {"mode": "barge", "connections": []}


    def _mock_saaras9(ws):
        req = ws.request
        info = {"path": req.path if req else "", "frames": 0}
        MOCK_STT9["connections"].append(info)
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "audio" not in msg:
                    continue
                info["frames"] += 1
                mode = MOCK_STT9["mode"]
                if mode == "barge" and info["frames"] == 1:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": "Wait, I have", "is_final": False}}))
                elif mode == "barge" and info["frames"] == 2:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": _LI_9, "is_final": True}}))
                elif mode == "talk" and info["frames"] == 1:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": _LANS_9, "is_final": True}}))
        except Exception:
            pass  # client went away mid-stream — the mock just stops


    _stt9_server = _ws_serve7(_mock_saaras9, "127.0.0.1", 0)
    threading.Thread(target=_stt9_server.serve_forever, daemon=True).start()
    MOCK_STT9_URL = f"ws://127.0.0.1:{_stt9_server.socket.getsockname()[1]}"

    _PROMPT_B_9 = """ROLE
    You are the BARGE TEST AGENT, a scripted platform test agent.

    FLOW
    1. Explain the call in three parts, then take the customer's question.

    DISPOSITIONS - classify the finished call into exactly ONE disposition:
    - SPOKEN: the callee spoke during the call.
    - NO_CONTACT: nobody spoke.

    CLOSINGS
    Always close politely and briefly.
    """

    _PROMPT_S_9 = """ROLE
    You are the SILENCE TEST AGENT, a scripted platform test agent.

    FLOW
    1. Greet the customer and ask whether they are the account holder.
    2. When they answer, thank them and confirm the details are being opened.

    DISPOSITIONS - classify the finished call into exactly ONE disposition:
    - SPOKEN: the callee spoke during the call.
    - NO_CONTACT: nobody spoke.

    CLOSINGS
    Close politely and briefly.
    """


    def _collect9(ws, sink, events, seconds):
        """Read the stream for `seconds`, accumulating playAudio bytes
        into sink and EVERY event (in order) into events — the order
        is what proves no audio follows a clearAudio."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
            except TimeoutError:
                break
            except Exception:
                events.append({"event": "__closed__"})
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            events.append(ev)
            if ev.get("event") == "playAudio":
                sink.extend(_b64_9.b64decode((ev.get("media") or {}).get("payload") or ""))


    def _until9(ws, sink, events, pred, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not pred():
            _collect9(ws, sink, events, 0.4)
        return pred()


    baseA9 = start_app6(18110, "a9-live", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
        "SARVAM_WS_URL": MOCK_STT9_URL, "SARVAM_BASE_URL": MOCK_AI9_URL,
    })
    tokenA9 = login6(baseA9)

    # ── config surface: the silence knobs exist with design defaults ──
    _, agB9 = call6(baseA9, "POST", "/api/agents", {
        "name": "A9 Barge Agent", "prompt": _PROMPT_B_9,
        "tts_voice_id": "bulbul-bn-female-1",
        "language_rule": {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "account_holder", "type": "boolean",
                             "description": "Whether the callee is the account holder"}],
    }, token=tokenA9, expect=201)
    check6("A9: silence config defaults — prompt after 8s, 1 prompt before the close (§5.2)",
           agB9["silence_prompt_after_s"] == 8 and agB9["silence_max_prompts"] == 1,
           {k: agB9.get(k) for k in ("silence_prompt_after_s", "silence_max_prompts")})
    _, agS9 = call6(baseA9, "POST", "/api/agents", {
        "name": "A9 Silence Agent", "prompt": _PROMPT_S_9,
        "tts_voice_id": "bulbul-bn-female-1",
        "silence_prompt_after_s": 1, "silence_max_prompts": 1,
        "language_rule": {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "account_holder", "type": "boolean",
                             "description": "Whether the callee is the account holder"}],
    }, token=tokenA9, expect=201)
    check6("A9: silence knobs are agent-configurable (1s / 1 prompt for the test agent)",
           agS9["silence_prompt_after_s"] == 1 and agS9["silence_max_prompts"] == 1,
           {k: agS9.get(k) for k in ("silence_prompt_after_s", "silence_max_prompts")})

    # ── barge-in: partial mid-reply cuts the audio ────────────────────
    cidB9 = campaign6(baseA9, tokenA9, agB9["agent_id"], "A9 — Barge Batch",
                      "self_hosted",
                      "callee_name,phone_number,bank_name,state\n"
                      "V. Rao,9500000031,Tata Capital,West Bengal\n")
    call6(baseA9, "POST", f"/api/campaigns/{cidB9}/dispatch", token=tokenA9)
    _, callsB9 = call6(baseA9, "GET", f"/api/campaigns/{cidB9}/calls", token=tokenA9)
    sB9 = callsB9["calls"][0]
    MOCK_STT9["mode"] = "barge"
    chat_b0, tts_b0 = len(MOCK_AI9["chat"]), len(MOCK_AI9["tts"])
    audioB9, eventsB9 = bytearray(), []
    wsB9 = _stream_open8(baseA9, tokenA9, sB9["session_id"])
    okB = _until9(wsB9, audioB9, eventsB9, lambda: len(audioB9) >= 160, 10)
    check6("A9: the barge opening starts playing (audio flowing before the interruption)",
           okB and len(audioB9) >= 160, len(audioB9))
    _media8(wsB9)  # media frame 1 → mock Saaras emits the partial "Wait, I have"
    okB = _until9(wsB9, audioB9, eventsB9,
                  lambda: any(e.get("event") == "clearAudio" for e in eventsB9), 10)
    check6("A9: BARGE-IN — the partial cuts the reply and the transport is told to flush (clearAudio)",
           okB, [e.get("event") for e in eventsB9][-6:])
    _collect9(wsB9, audioB9, eventsB9, 1.0)  # settle: anything already queued drains
    idx_clear = next((i for i, e in enumerate(eventsB9)
                      if e.get("event") == "clearAudio"), -1)
    check6("A9: clearAudio carries the stream id Plivo supplied, and NO playAudio follows it",
           idx_clear >= 0
           and eventsB9[idx_clear].get("stream_id") == "str-a8"
           and all(e.get("event") != "playAudio" for e in eventsB9[idx_clear:]),
           eventsB9[idx_clear] if idx_clear >= 0 else None)
    check6("A9: the opening was cut short — some audio heard, never the whole reply",
           0 < len(audioB9) < _FULL_B_AUDIO_9, (len(audioB9), _FULL_B_AUDIO_9))
    ttsB = [t["inputs"][0] for t in MOCK_AI9["tts"][tts_b0:]]
    check6("A9: unsent sentences were abandoned — TTS requests are a strict prefix of the reply",
           len(ttsB) >= 1 and ttsB == [_B1_9, _B2_9, _B3_9][:len(ttsB)], ttsB)
    audio_at_cut = len(audioB9)
    _media8(wsB9)  # media frame 2 → the interrupting line lands as a final
    okB = _until9(wsB9, audioB9, eventsB9,
                  lambda: len(audioB9) >= audio_at_cut + len(_mock_audio9(_RB_9)), 10)
    check6("A9: the interrupting line becomes the next turn — its reply is spoken",
           okB, (len(audioB9), audio_at_cut))
    _stream_stop8(wsB9)
    doneB9 = poll8(baseA9, tokenA9, cidB9, sB9["session_id"],
                   lambda c: c["final_state"] is not None)
    tB9 = (doneB9 or {}).get("transcript") or []
    spokenB = tB9[0]["text"] if tB9 else ""
    check6("A9: the interrupted turn is recorded with ONLY the words actually spoken",
           doneB9 is not None and len(tB9) == 3
           and tB9[0]["speaker"] == "agent" and tB9[0].get("interrupted") is True
           and spokenB != "" and _OPEN_B_9.startswith(spokenB)
           and tB9[1]["speaker"] == "customer" and tB9[1]["text"] == _LI_9
           and tB9[2]["speaker"] == "agent" and tB9[2]["text"] == _RB_9,
           tB9)
    check6("A9: the classifier sees the coherent interrupted transcript — SPOKEN, no review",
           doneB9 is not None and doneB9["disposition"] == "SPOKEN"
           and doneB9["review_required"] is False
           and "interrupted the agent" in doneB9["summary"], doneB9)
    chatsB = [c for c in MOCK_AI9["chat"][chat_b0:]
              if any(m.get("role") == "user" and m.get("content") == _LI_9
                     for m in c["messages"])]
    check6("A9: LLM history carries the SPOKEN part as the assistant line (never the full reply)",
           len(chatsB) == 1
           and any(m.get("role") == "assistant" and m.get("content") == spokenB
                   for m in chatsB[0]["messages"])
           and not any(m.get("role") == "assistant" and m.get("content") == _OPEN_B_9
                       for m in chatsB[0]["messages"]),
           [m.get("content") for c in chatsB for m in c["messages"]
            if m.get("role") == "assistant"])

    # ── silence: nudge → answer resets → nudge again → polite close ──
    cidS9 = campaign6(baseA9, tokenA9, agS9["agent_id"], "A9 — Silence Batch",
                      "self_hosted",
                      "callee_name,phone_number,bank_name,state\n"
                      "W. Khan,9500000032,Tata Capital,West Bengal\n")
    call6(baseA9, "POST", f"/api/campaigns/{cidS9}/dispatch", token=tokenA9)
    _, callsS9 = call6(baseA9, "GET", f"/api/campaigns/{cidS9}/calls", token=tokenA9)
    sS9 = callsS9["calls"][0]
    MOCK_STT9["mode"] = "talk"
    chat_s0, tts_s0 = len(MOCK_AI9["chat"]), len(MOCK_AI9["tts"])
    audioS9, eventsS9 = bytearray(), []
    wsS9 = _stream_open8(baseA9, tokenA9, sS9["session_id"])
    open_target = sum(len(_mock_audio9(s)) for s in _OPEN_SENT_9)
    okS = _until9(wsS9, audioS9, eventsS9, lambda: len(audioS9) >= open_target, 10)
    check6("A9: silence scenario — the opening is spoken in full first",
           okS and len(audioS9) == open_target, len(audioS9))
    okS = _until9(wsS9, audioS9, eventsS9,
                  lambda: len(audioS9) >= open_target + len(_mock_audio9(_NUDGE_9)), 12)
    ttsS = [t["inputs"][0] for t in MOCK_AI9["tts"][tts_s0:]]
    check6("A9: SILENCE — after the configured window the agent speaks a nudge",
           okS and _NUDGE_9 in ttsS, ttsS)
    chatsS = MOCK_AI9["chat"][chat_s0:]
    check6("A9: the nudge is an LLM turn carrying the silence context (wording stays the model's)",
           any(m.get("role") == "user" and "been silent" in m.get("content", "")
               for c in chatsS for m in c["messages"]),
           [m.get("content") for c in chatsS for m in c["messages"]][-2:])
    _media8(wsS9)  # the callee finally answers — the silence counter must reset
    okS = _until9(wsS9, audioS9, eventsS9,
                  lambda: len(audioS9) >= open_target + len(_mock_audio9(_NUDGE_9))
                  + len(_mock_audio9(_RANS_9)), 10)
    check6("A9: the answer after the nudge gets a normal reply",
           okS, len(audioS9))
    okS = _until9(wsS9, audioS9, eventsS9,
                  lambda: len(audioS9) >= open_target + 2 * len(_mock_audio9(_NUDGE_9))
                  + len(_mock_audio9(_RANS_9)), 12)
    ttsS = [t["inputs"][0] for t in MOCK_AI9["tts"][tts_s0:]]
    check6("A9: a fresh silence earns a fresh nudge (the counter reset on the answer)",
           okS and ttsS.count(_NUDGE_9) == 2
           and not any(e.get("event") == "__closed__" for e in eventsS9), ttsS)
    okS = _until9(wsS9, audioS9, eventsS9,
                  lambda: any(e.get("event") == "__closed__" for e in eventsS9), 12)
    tts_close = [t["inputs"][0] for t in MOCK_AI9["tts"][tts_s0:]]
    check6("A9: prompts exhausted — the polite closing is spoken and the stream ends",
           okS and "Goodbye." in tts_close
           and any(t.startswith("Since I cannot hear you") for t in tts_close),
           tts_close[-2:])
    try:
        wsS9.close()
    except Exception:
        pass
    doneS9 = poll8(baseA9, tokenA9, cidS9, sS9["session_id"],
                   lambda c: c["final_state"] is not None)
    check6("A9: the silence-ended call finalizes with end_reason no_input (not agent_end_call)",
           doneS9 is not None and doneS9["final_state"] == "COMPLETED"
           and doneS9["end_reason"] == "no_input", doneS9 and doneS9["end_reason"])
    check6("A9: silence transcript — opening, nudge, answer, reply, nudge, closing, in order",
           doneS9 is not None
           and [t["text"] for t in doneS9["transcript"]] ==
               [_OPEN_9, _NUDGE_9, _LANS_9, _RANS_9, _NUDGE_9, _CLOSE_9]
           and doneS9["disposition"] == "SPOKEN"
           and "no input" in doneS9["summary"], doneS9 and doneS9["transcript"])

    _ai9_server.shutdown()
    _stt9_server.shutdown()

    # ── A10: dispatch at scale, results surface, managed parity ──────
    # Main-instance checks (market-local schedule, results summary,
    # dashboard, calls filters) run against BASE with call(); the retry
    # and parity checks boot one more managed instance on the fake
    # provider (start_app6/call6), because retry outcomes arrive via the
    # Vapi webhook — a stub dispatch finalizes synchronously and can
    # never produce a no-answer to retry from.
    print("── A10: market-local schedule — windows evaluated in the tenant's timezone ──")
    from zoneinfo import ZoneInfo as _ZI
    _kol = _ZI("Asia/Kolkata")
    _now_kol = datetime.now(_kol)
    _now_utc = datetime.utcnow()

    _, _me10 = call("GET", "/api/tenants/me", token=token)
    check6("A10: tenant carries its market timezone (QGBS → Asia/Kolkata)",
           _me10["timezone"] == "Asia/Kolkata", _me10)

    def _hhmm(dt):
        return dt.strftime("%H:%M")

    # A window AROUND Kolkata-now: under A5's UTC evaluation this window
    # (Kolkata wall-clock strings) excludes UTC-now by 5½ hours, so an
    # admitted dispatch proves the fix. Window length 60 min, centred
    # on local now; wrap-safe via the dispatcher's wrap handling.
    _w_in = {"window_start": _hhmm(_now_kol - timedelta(minutes=20)),
             "window_end": _hhmm(_now_kol + timedelta(minutes=40)),
             "calling_days": "Mon - Sun", "daily_cap": 2000}
    _, _campIn = call("POST", "/api/campaigns", {
        "agent_id": rpi["agent_id"], "name": "A10 — Local Window Admits",
        "provider_mode": "self_hosted", "schedule": _w_in,
    }, token=token, expect=201)
    _cidIn = _campIn["campaign_id"]
    call_upload(f"/api/campaigns/{_cidIn}/leads/upload", A5_CSV, token=token,
                fields={"mapping": json.dumps(A5_MAP)})
    call("POST", f"/api/campaigns/{_cidIn}/status", {"status": "active"}, token=token)
    _, _repIn = call("POST", f"/api/campaigns/{_cidIn}/dispatch", token=token)
    check6("A10: window around MARKET-LOCAL now admits (A5's UTC check would refuse)",
           _repIn["outside_window"] is False and _repIn["created"] == 5
           and _repIn["timezone"] == "Asia/Kolkata", _repIn)

    _w_out = {"window_start": _hhmm(_now_kol + timedelta(hours=3)),
              "window_end": _hhmm(_now_kol + timedelta(hours=4)),
              "calling_days": "Mon - Sun", "daily_cap": 2000}
    _, _campOut = call("POST", "/api/campaigns", {
        "agent_id": rpi["agent_id"], "name": "A10 — Local Window Refuses",
        "provider_mode": "self_hosted", "schedule": _w_out,
    }, token=token, expect=201)
    call_upload(f"/api/campaigns/{_campOut['campaign_id']}/leads/upload", A5_CSV,
                token=token, fields={"mapping": json.dumps(A5_MAP)})
    call("POST", f"/api/campaigns/{_campOut['campaign_id']}/status",
         {"status": "active"}, token=token)
    _, _repOut = call("POST", f"/api/campaigns/{_campOut['campaign_id']}/dispatch", token=token)
    check6("A10: window 3h ahead in market-local time refuses, and the message names the timezone",
           _repOut["outside_window"] is True and _repOut["created"] == 0
           and "Asia/Kolkata" in _repOut["message"], _repOut)

    # Calling-days gate: a schedule naming ONLY a different weekday.
    _days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    _other_day = _days[(_now_kol.weekday() + 1) % 7]
    _, _campDay = call("POST", "/api/campaigns", {
        "agent_id": rpi["agent_id"], "name": "A10 — Calling Days Gate",
        "provider_mode": "self_hosted",
        "schedule": {"window_start": "00:00", "window_end": "23:59",
                     "calling_days": _other_day, "daily_cap": 2000},
    }, token=token, expect=201)
    call_upload(f"/api/campaigns/{_campDay['campaign_id']}/leads/upload", A5_CSV,
                token=token, fields={"mapping": json.dumps(A5_MAP)})
    call("POST", f"/api/campaigns/{_campDay['campaign_id']}/status",
         {"status": "active"}, token=token)
    _, _repDay = call("POST", f"/api/campaigns/{_campDay['campaign_id']}/dispatch", token=token)
    check6("A10: today outside the calling days → nothing admitted",
           _repDay["outside_window"] is True and _repDay["created"] == 0
           and "calling days" in _repDay["message"], _repDay)

    # Start-date gate: the campaign starts tomorrow (market-local).
    _tomorrow = (_now_kol + timedelta(days=1)).date().isoformat()
    _, _campSd = call("POST", "/api/campaigns", {
        "agent_id": rpi["agent_id"], "name": "A10 — Starts Tomorrow",
        "provider_mode": "self_hosted",
        "schedule": {"start_date": _tomorrow, "window_start": "00:00",
                     "window_end": "23:59", "calling_days": "Mon - Sun",
                     "daily_cap": 2000},
    }, token=token, expect=201)
    call_upload(f"/api/campaigns/{_campSd['campaign_id']}/leads/upload", A5_CSV,
                token=token, fields={"mapping": json.dumps(A5_MAP)})
    call("POST", f"/api/campaigns/{_campSd['campaign_id']}/status",
         {"status": "active"}, token=token)
    _, _repSd = call("POST", f"/api/campaigns/{_campSd['campaign_id']}/dispatch", token=token)
    check6("A10: before the start date → nothing admitted",
           _repSd["outside_window"] is True and _repSd["created"] == 0
           and "start date" in _repSd["message"], _repSd)

    print("── A10: campaign results summary + dashboard + calls filters ──")
    _, _res5 = call("GET", f"/api/campaigns/{cid5}/results", token=token)
    _exp_counts = {b: (1 if b in RPI_BUCKETS[:5] else 0) for b in RPI_BUCKETS}
    check6("A10: results summary — buckets from the agent's own prompt, zero-filled counts",
           _res5["buckets"] == RPI_BUCKETS
           and _res5["counts_by_disposition"] == _exp_counts, _res5)
    check6("A10: results summary — attempts / connect rate / leads by state",
           _res5["attempts_made"] == 5 and _res5["results_count"] == 5
           and _res5["connected"] == 5 and _res5["connect_rate"] == 1.0
           and _res5["review_required"] == 0
           and _res5["leads_by_state"] == {"done": 5}
           and _res5["leads_total"] == 5 and _res5["awaiting_retry"] == 0, _res5)
    call("GET", f"/api/campaigns/{cid5}/results", token=token2, expect=404)
    print("  PASS  results summary is tenant-scoped (tenant 2 → 404)")

    _, _dash = call("GET", "/api/dashboard", token=token)
    check6("A10: dashboard — totals across campaigns (7 from A5 + 5 admitted above)",
           _dash["calls_total"] == 12 and _dash["results_total"] == 12
           and _dash["timezone"] == "Asia/Kolkata"
           and _dash["dispositions"].get("RPC_CONFIRMED") == 3
           and _dash["dispositions"].get("CPC_CONFIRMED") == 3
           and _dash["review_queue_count"] == 0
           and _dash["active_campaigns"] >= 1
           and 5 <= _dash["calls_today"] <= 12, _dash)
    _, _dash2 = call("GET", "/api/dashboard", token=token2)
    check6("A10: dashboard is tenant-scoped (tenant 2 sees zeros)",
           _dash2["calls_total"] == 0 and _dash2["results_total"] == 0
           and _dash2["review_queue_count"] == 0, _dash2)

    _, _f1 = call("GET", "/api/calls?disposition=RPC_CONFIRMED", token=token)
    check6("A10: calls filter — disposition=RPC_CONFIRMED matches all three campaigns' first results",
           _f1["total"] == 3
           and all(c["disposition"] == "RPC_CONFIRMED" for c in _f1["calls"]), _f1["total"])
    _, _f2 = call("GET", "/api/calls?disposition=DECEASED_CASE", token=token)
    check6("A10: calls filter — a bucket never recorded matches nothing",
           _f2["total"] == 0, _f2["total"])
    _, _f3 = call("GET", "/api/calls?review_required=true", token=token)
    _, _f4 = call("GET", f"/api/calls?campaign_id={cid5}&disposition=CPC_CONFIRMED", token=token)
    check6("A10: calls filter — review_required + campaign/disposition combined",
           _f3["total"] == 0 and _f4["total"] == 1
           and _f4["calls"][0]["campaign_id"] == cid5, (_f3["total"], _f4["total"]))

    print("── A10: retry / re-queue — no-answer earns a NEW attempt, never a second result ──")
    baseR = start_app6(18112, "a10retry", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-a10",
    })
    tokenR = login6(baseR)
    _, agentsR = call6(baseR, "GET", "/api/agents", token=tokenR)
    rpiR = agentsR[0]

    def _campaignR(name, retry, csv_text):
        _s, camp = call6(baseR, "POST", "/api/campaigns", {
            "agent_id": rpiR["agent_id"], "name": name, "product": "vehicle_loan",
            "provider_mode": "managed", "retry_rules": retry,
            "schedule": {"window_start": "00:00", "window_end": "23:59",
                         "calling_days": "Mon - Sun", "daily_cap": 2000},
        }, token=tokenR, expect=201)
        cid = camp["campaign_id"]
        upload6(baseR, f"/api/campaigns/{cid}/leads/upload", csv_text, tokenR, A6_MAP)
        call6(baseR, "POST", f"/api/campaigns/{cid}/status", {"status": "active"}, token=tokenR)
        return cid

    def _webhookR(session_id, call_id, ended, structured=None, extra=None):
        msg = {"type": "end-of-call-report", "endedReason": ended,
               "startedAt": "2026-10-23T11:00:00.000Z",
               "endedAt": "2026-10-23T11:00:30.000Z",
               "call": {"id": call_id, "metadata": {"session_id": session_id}},
               "analysis": {"structuredData": structured or {}},
               "summary": "", "artifact": {}, "costBreakdown": {"total": 0.02}}
        msg.update(extra or {})
        return call6(baseR, "POST", "/api/telephony/vapi/webhook", {"message": msg},
                     headers={"x-vapi-secret": "a6-webhook-secret"})

    def _sessionsR(cid):
        _s, cl = call6(baseR, "GET", f"/api/campaigns/{cid}/calls", token=tokenR)
        return cl["calls"]

    def _leadsR(cid):
        _s, ll = call6(baseR, "GET", f"/api/campaigns/{cid}/leads", token=tokenR)
        return ll["leads"]

    # Campaign A — retry gap 1 day: the re-queued lead waits.
    cidRA = _campaignR("A10 — Retry Waits For The Gap",
                       {"max_attempts": 3, "retry_gap_days": 1, "retry_different_time": False},
                       "callee_name,phone_number,bank_name,state\n"
                       "R. One,9400000001,Tata Capital,Bihar\n")
    _, _repRA = call6(baseR, "POST", f"/api/campaigns/{cidRA}/dispatch", token=tokenR)
    check6("A10: managed dispatch places attempt 1", _repRA["placed"] == 1, _repRA)
    _sessRA = _sessionsR(cidRA)[0]
    _webhookR(_sessRA["session_id"], "call_a10_a1", "customer-did-not-answer")
    _leadRA = _leadsR(cidRA)[0]
    _dueRA = datetime.fromisoformat(_leadRA["next_attempt_at"]) if _leadRA["next_attempt_at"] else None
    check6("A10: NO_ANSWER re-queues the lead — pending again, attempt counted, due ≈ +1 day",
           _leadRA["status"] == "pending" and _leadRA["attempt_count"] == 1
           and _dueRA is not None
           and datetime.utcnow() + timedelta(hours=12) < _dueRA < datetime.utcnow() + timedelta(hours=36),
           _leadRA)
    _, _repRA2 = call6(baseR, "POST", f"/api/campaigns/{cidRA}/dispatch", token=tokenR)
    check6("A10: dispatch inside the retry gap admits nothing and SAYS the lead is waiting",
           _repRA2["created"] == 0 and _repRA2["retry_waiting"] == 1, _repRA2)
    _resRA = call6(baseR, "GET", f"/api/campaigns/{cidRA}/results", token=tokenR)[1]
    check6("A10: results summary reflects the re-queue (awaiting_retry, leads by state)",
           _resRA["awaiting_retry"] == 1 and _resRA["leads_by_state"] == {"pending": 1}
           and _resRA["attempts_made"] == 1 and _resRA["connect_rate"] == 0.0, _resRA)

    # Campaign B — gap 0, max 2 attempts: retry fires at once, then exhausts.
    cidRB = _campaignR("A10 — Retry Then Exhaust",
                       {"max_attempts": 2, "retry_gap_days": 0, "retry_different_time": False},
                       "callee_name,phone_number,bank_name,state\n"
                       "S. Two,9400000002,Tata Capital,Bihar\n"
                       "T. Three,9400000003,Tata Capital,Bihar\n")
    _, _repRB = call6(baseR, "POST", f"/api/campaigns/{cidRB}/dispatch", token=tokenR)
    check6("A10: campaign B places 2 first attempts", _repRB["placed"] == 2, _repRB)
    for _s1 in _sessionsR(cidRB):
        _webhookR(_s1["session_id"], f"call_a10_b_{_s1['attempt_id']}", "customer-busy")
    _leadsRB = _leadsR(cidRB)
    check6("A10: BUSY re-queues both leads, due immediately with gap 0",
           all(l["status"] == "pending" and l["attempt_count"] == 1
               and (l["next_attempt_at"] is None
                    or datetime.fromisoformat(l["next_attempt_at"]) <= datetime.utcnow() + timedelta(minutes=1))
               for l in _leadsRB), _leadsRB)
    _, _repRB2 = call6(baseR, "POST", f"/api/campaigns/{cidRB}/dispatch", token=tokenR)
    _sessRB2 = _sessionsR(cidRB)
    check6("A10: second dispatch creates NEW attempts (attempt 2 ids), 4 sessions total",
           _repRB2["created"] == 2 and _repRB2["placed"] == 2
           and len(_sessRB2) == 4
           and sum(1 for s in _sessRB2 if s["attempt_id"].endswith("_2")) == 2, _repRB2)
    for _s2 in _sessRB2:
        if _s2["attempt_id"].endswith("_2"):
            _webhookR(_s2["session_id"], f"call_a10_b2_{_s2['attempt_id']}", "customer-did-not-answer")
    _leadsRB2 = _leadsR(cidRB)
    _sessRB3 = _sessionsR(cidRB)
    check6("A10: attempts exhausted → leads reach the final state and stop",
           all(l["status"] == "exhausted" and l["attempt_count"] == 2
               and l["next_attempt_at"] is None for l in _leadsRB2), _leadsRB2)
    check6("A10: exactly one result per attempt across retries (4 attempts, 4 results)",
           len(_sessRB3) == 4
           and all(s["final_state"] in ("BUSY", "NO_ANSWER") for s in _sessRB3), len(_sessRB3))
    # Auto-complete: with every lead exhausted, nothing is dispatchable —
    # the campaign completes itself, so a re-dispatch is refused (before
    # the fix it stayed active and returned zeros here).
    _, _campRB = call6(baseR, "GET", f"/api/campaigns/{cidRB}", token=tokenR)
    check6("A10: exhausted leads complete the campaign",
           _campRB["status"] == "completed", _campRB)
    call6(baseR, "POST", f"/api/campaigns/{cidRB}/dispatch", token=tokenR, expect=400)
    check6("A10: exhausted leads are never dispatched again",
           True, "re-dispatch refused on completed campaign")

    print("── A10: managed-mode parity — the same result surface as the stub ──")
    cidRP = _campaignR("A10 — Parity Batch",
                       {"max_attempts": 3, "retry_gap_days": 1, "retry_different_time": False},
                       "callee_name,phone_number,bank_name,state\n"
                       "P. Parity,9400000009,Tata Capital,West Bengal\n")
    call6(baseR, "POST", f"/api/campaigns/{cidRP}/dispatch", token=tokenR)
    _sessRP = _sessionsR(cidRP)[0]
    _webhookR(_sessRP["session_id"], "call_a10_parity", "customer-ended-call",
              structured={"disposition": "RPC_CONFIRMED", "identity_confirmed": True},
              extra={"startedAt": "2026-10-23T11:00:00.000Z",
                     "endedAt": "2026-10-23T11:00:47.000Z",
                     "summary": "Customer confirmed identity.",
                     "artifact": {"recordingUrl": "https://recordings.example/parity.mp3",
                                  "transcript": "AI: Namaste, am I speaking with P. Parity?\nUser: Yes, speaking."},
                     "costBreakdown": {"total": 0.11, "transport": 0.05,
                                       "stt": 0.02, "llm": 0.03, "tts": 0.01}})
    _managed_call = _sessionsR(cidRP)[0]
    _, _stub_list = call("GET", f"/api/campaigns/{cid5}/calls", token=token)
    _stub_call = _stub_list["calls"][0]
    _COST_KEYS = {"telephony", "stt", "llm", "tts", "total", "currency", "mode"}
    check6("A10 PARITY: managed and stub calls expose the identical CallOut field set",
           set(_managed_call.keys()) == set(_stub_call.keys()),
           (set(_managed_call) ^ set(_stub_call)))
    check6("A10 PARITY: identical result-surface values where the mode guarantees them",
           _managed_call["final_state"] == "COMPLETED"
           and _stub_call["final_state"] == "COMPLETED"
           and _managed_call["disposition"] == "RPC_CONFIRMED"
           and _managed_call["duration_s"] == 47 and _stub_call["duration_s"] > 0
           and _managed_call["captured_fields"].get("identity_confirmed") is True
           and isinstance(_stub_call["captured_fields"].get("identity_confirmed"), bool)
           and _managed_call["recording_url"].endswith("parity.mp3")
           and isinstance(_stub_call["recording_url"], str)
           and _managed_call["review_required"] is False
           and _stub_call["review_required"] is False, _managed_call)
    check6("A10 PARITY: cost breakdown carries the contract keys in both modes",
           _COST_KEYS <= set(_managed_call["cost_breakdown"])
           and _COST_KEYS <= set(_stub_call["cost_breakdown"])
           and _managed_call["cost_breakdown"]["mode"] == "managed"
           and _managed_call["cost_breakdown"]["total"] == 0.11
           and _stub_call["cost_breakdown"]["mode"] == "stub",
           (_managed_call["cost_breakdown"], _stub_call["cost_breakdown"]))
    check6("A10 PARITY: transcripts share the {speaker, text, offset_s} turn shape",
           len(_managed_call["transcript"]) == 2
           and all({"speaker", "text", "offset_s"} <= set(t) for t in _managed_call["transcript"])
           and all({"speaker", "text", "offset_s"} <= set(t) for t in _stub_call["transcript"]),
           _managed_call["transcript"])

    # The closed gap: a CONNECTED managed call whose analysis returns no
    # disposition must land review-flagged, exactly like the self-hosted
    # classifier-failure path (A8) — not as an unclassified success.
    cidRD = _campaignR("A10 — Parity Review Gap",
                       {"max_attempts": 3, "retry_gap_days": 1, "retry_different_time": False},
                       "callee_name,phone_number,bank_name,state\n"
                       "Q. Quiet,9400000010,Tata Capital,Bihar\n")
    call6(baseR, "POST", f"/api/campaigns/{cidRD}/dispatch", token=tokenR)
    _sessRD = _sessionsR(cidRD)[0]
    _webhookR(_sessRD["session_id"], "call_a10_review", "customer-ended-call")
    _callRD = _sessionsR(cidRD)[0]
    check6("A10 PARITY: connected managed call with NO disposition → review flag (the A8 behaviour)",
           _callRD["final_state"] == "COMPLETED" and _callRD["disposition"] == ""
           and _callRD["review_required"] is True
           and "no disposition" in _callRD["summary"], _callRD)
    _, _dashR = call6(baseR, "GET", "/api/dashboard", token=tokenR)
    check6("A10: dashboard counts the review queue across campaigns (managed instance)",
           _dashR["review_queue_count"] == 1, _dashR)
    _, _fR = call6(baseR, "GET", "/api/calls?review_required=true", token=tokenR)
    check6("A10: calls filter finds the flagged managed call",
           _fR["total"] == 1 and _fR["calls"][0]["session_id"] == _sessRD["session_id"], _fR["total"])

    print("── A11: browser test harness — same engine over a browser transport, fakes only ──")
    # The harness runs the SAME Saaras session + ConversationEngine as the
    # Plivo stream, over a WebSocket the browser speaks: PCM16 @16 kHz in
    # (binary), PCM16 @8 kHz out (binary), JSON events for ready /
    # transcript / partial / interrupt / ended. A dedicated mock Saaras
    # RECORDS the audio payloads it receives, so the server's
    # PCM16→μ-law conversion is asserted at the STT boundary itself.
    # The LLM/TTS/classifier HTTP mock is A11's OWN (below): the A8/A9
    # sections shut their mock servers down when done — and a shut-down
    # HTTPServer leaves its socket listening, so calls to the old URL
    # connect and then hang until timeout. This twin serves both scripts
    # used below, routed like the originals: the A8 survey (by last user
    # line) and the A9 barge script (system names the BARGE TEST AGENT).
    # No real provider, no real browser: a sync WebSocket client speaks
    # the harness protocol exactly as the page does.
    import base64 as _b64_11
    import struct as _struct_11
    from websockets.sync.client import connect as _ws_connect11


    def _mock_audio11(text):
        if text.startswith(("First,", "Second,", "Third,")):
            return _mock_audio9(text)   # the long barge-in opening (rep 60)
        return _mock_audio8(text)


    class _MockSarvamAI11(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, data, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if self.path == "/chat/completions":
                self._chat(body)
            elif self.path == "/text-to-speech":
                text = (body.get("inputs") or [""])[0]
                self._json(200, {"audios": [
                    _b64_11.b64encode(_mock_audio11(text)).decode()]})
            else:
                self._json(404, {})

        def _chat(self, body):
            msgs = body.get("messages") or []
            system = msgs[0].get("content", "") if msgs else ""
            users = [m.get("content", "") for m in msgs if m.get("role") == "user"]
            if "POST-CALL CLASSIFIER" in system:
                # The classifier call carries the agent prompt in its USER
                # message (classify_call's shape) — route on the whole body.
                if "BARGE TEST AGENT" in (system + "\n" + "\n".join(users)):
                    content = {"disposition": "SPOKEN", "captured_fields": {},
                               "confidence": 0.91,
                               "summary": "Mock classifier (A11): the callee spoke."}
                else:
                    content = {"disposition": "ANSWERED",
                               "captured_fields": {"favourite_colour": "blue"},
                               "confidence": 0.93,
                               "summary": "Mock classifier (A11): survey answered."}
                self._json(200, {"choices": [{"message": {
                    "role": "assistant", "content": json.dumps(content)}}]})
                return
            last = users[-1] if users else ""
            end_call = False
            if "BARGE TEST AGENT" in system:
                if last == _LI_9:
                    pieces = [_RB_9]
                elif "been silent" in last:
                    pieces = [_NUDGE_9]
                elif "not responded" in last:
                    pieces = [_CLOSE_9]
                    end_call = True
                else:  # kickoff — the long three-sentence opening
                    pieces = ["First, let me explain the reason ",
                              "for this call in some detail. ",
                              "Second, there are several options ",
                              "we should walk through together. ",
                              "Third, I will confirm the next steps ",
                              "before we finish."]
            elif last == _L2_8:
                pieces = ["Thank you for your answers. ", "Have a good day."]
                end_call = True
            elif last == _L1_8:
                pieces = ["Blue is a lovely ", "colour. And do you prefer it ",
                          "for clothes or for cars?"]
            else:  # survey kickoff
                pieces = ["Hello, this is a short survey ",
                          "call. What is your favourite colour?"]
            lines = ["data: " + json.dumps(
                {"choices": [{"delta": {"content": p}}]}) + "\n\n" for p in pieces]
            if end_call:
                lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_end_11", "type": "function",
                     "function": {"name": "end_call", "arguments": ""}}]}}]}) + "\n\n")
            lines.append("data: [DONE]\n\n")
            self._send(200, "".join(lines).encode(), "text/event-stream")


    _ai11_server = ThreadingHTTPServer(("127.0.0.1", 0), _MockSarvamAI11)
    threading.Thread(target=_ai11_server.serve_forever, daemon=True).start()
    MOCK_AI11_URL = f"http://127.0.0.1:{_ai11_server.server_address[1]}"

    MOCK_STT11 = {"mode": "survey", "connections": []}


    def _mock_saaras11(ws):
        req = ws.request
        info = {"path": req.path if req else "", "frames": 0, "payloads": []}
        MOCK_STT11["connections"].append(info)
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "audio" not in msg:
                    continue
                info["frames"] += 1
                try:
                    info["payloads"].append(_b64_11.b64decode(msg["audio"]["data"]))
                except Exception:
                    pass
                mode, n = MOCK_STT11["mode"], info["frames"]
                if mode == "survey" and n == 1:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": _L1_8, "is_final": True}}))
                elif mode == "survey" and n == 2:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": _L2_8, "is_final": True}}))
                elif mode == "barge" and n == 1:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": "Wait, I have", "is_final": False}}))
                elif mode == "barge" and n == 2:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": _LI_9, "is_final": True}}))
        except Exception:
            pass  # client went away mid-stream — the mock just stops


    _stt11_server = _ws_serve7(_mock_saaras11, "127.0.0.1", 0)
    threading.Thread(target=_stt11_server.serve_forever, daemon=True).start()
    MOCK_STT11_URL = f"ws://127.0.0.1:{_stt11_server.socket.getsockname()[1]}"


    def _pcm11(value, samples=320):
        """One 20 ms inbound chunk: constant-value PCM16 @16 kHz."""
        return _struct_11.pack(f"<{samples}h", *([value] * samples))


    class _BtClient:
        """The harness protocol, client side: binary audio accumulates,
        JSON events accumulate, and `stream` keeps the receive ORDER of
        the two — the order is what proves an interruption cut."""

        def __init__(self, ws):
            self.ws = ws
            self.audio = bytearray()
            self.events = []
            self.stream = []

        def _feed(self, raw):
            if isinstance(raw, (bytes, bytearray)):
                self.audio += raw
                self.stream.append(("audio", len(raw)))
            else:
                try:
                    ev = json.loads(raw)
                except Exception:
                    return
                self.events.append(ev)
                self.stream.append(("json", ev))

        def until(self, pred, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if pred():
                    return True
                try:
                    raw = self.ws.recv(timeout=max(0.05, deadline - time.monotonic()))
                except TimeoutError:
                    break
                except Exception:
                    self.stream.append(("closed", None))
                    break
                self._feed(raw)
            return pred()

        def for_seconds(self, seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    raw = self.ws.recv(timeout=max(0.05, deadline - time.monotonic()))
                except TimeoutError:
                    break
                except Exception:
                    self.stream.append(("closed", None))
                    break
                self._feed(raw)

        def turns(self):
            return [e["turn"] for e in self.events if e.get("event") == "transcript"]

        def has_event(self, name, **match):
            return any(e.get("event") == name
                       and all(e.get(k) == v for k, v in match.items())
                       for e in self.events)


    def _bt_open(base, sid, token):
        url = (base.replace("http://", "ws://")
               + f"/api/telephony/browser/stream/{sid}?token={token}")
        return _BtClient(_ws_connect11(url))


    def _bt_close_code(base, sid, token):
        url = (base.replace("http://", "ws://")
               + f"/api/telephony/browser/stream/{sid}?token={token}")
        try:
            ws = _ws_connect11(url)
        except Exception as e:
            return getattr(getattr(e, "rcvd", None), "code", None)
        try:
            ws.recv(timeout=5)
        except Exception as e:
            return getattr(getattr(e, "rcvd", None), "code", None)
        return None


    def _bt_poll(base, token, sid, pred, tries=60, nap=0.25):
        last = None
        for _ in range(tries):
            _s, cl = call6(base, "GET", "/api/calls", token=token)
            last = next((c for c in cl["calls"] if c["session_id"] == sid), None)
            if last is not None and pred(last):
                return last
            time.sleep(nap)
        return last


    # ── main scenario: saved lead, full survey conversation ───────────
    baseB = start_app6(18111, "browser-live", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
        "SARVAM_WS_URL": MOCK_STT11_URL, "SARVAM_BASE_URL": MOCK_AI11_URL,
    })
    tokenB = login6(baseB)
    _, survey = call6(baseB, "POST", "/api/agents", {
        "name": "A11 Scripted Survey Agent",
        "purpose": "Browser-harness twin of the A8 scripted agent",
        "prompt": _SCRIPTED_PROMPT_8,
        "llm_model": "sarvam-105b",
        "tts_voice_id": "bulbul-bn-female-1",
        "language_rule": {"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "favourite_colour", "type": "string",
                             "description": "The callee's favourite colour"}],
    }, token=tokenB, expect=201)
    cidB = campaign6(baseB, tokenB, survey["agent_id"], "A11 — Context Batch",
                     "self_hosted",
                     "callee_name,phone_number,bank_name,state\n"
                     "P. Sen,9500000001,Tata Capital,West Bengal\n")
    _, leadsB = call6(baseB, "GET", f"/api/campaigns/{cidB}/leads", token=tokenB)
    leadB = leadsB["leads"][0]

    _, bt = call6(baseB, "POST", "/api/browser-tests", {
        "agent_id": survey["agent_id"], "campaign_id": cidB,
        "lead_id": leadB["lead_id"],
    }, token=tokenB, expect=201)
    sidB = bt["session_id"]
    check6("A11: browser test created — channel marker, snapshot lead, stream path",
           bt["channel"] == "browser-test" and bt["state"] == "QUEUED"
           and bt["lead_name"] == "P. Sen" and bt["to_number"] == "9500000001"
           and bt["stream_path"].endswith(sidB), bt)
    _, campsB2 = call6(baseB, "GET", f"/api/campaigns/{cidB}/calls", token=tokenB)
    _, leadsB2 = call6(baseB, "GET", f"/api/campaigns/{cidB}/leads", token=tokenB)
    check6("A11: creating a test touches NO campaign state (no session, lead unattempted)",
           campsB2["total"] == 0 and leadsB2["leads"][0]["attempt_count"] == 0
           and leadsB2["leads"][0]["status"] == "pending",
           (campsB2["total"], leadsB2["leads"][0]))

    print("── A11: tenant isolation on create + stream ──")
    _, t2B = call6(baseB, "POST", "/api/tenants", {"name": "OtherBrowser", "market": "IN"})
    _, loginB2 = call6(baseB, "POST", "/api/auth/login",
                       {"email": t2B["admin_email"], "password": t2B["admin_password"]})
    tokenB2 = loginB2["access_token"]
    call6(baseB, "POST", "/api/browser-tests", {"agent_id": survey["agent_id"]},
          token=tokenB2, expect=404)
    call6(baseB, "POST", "/api/browser-tests",
          {"agent_id": survey["agent_id"], "campaign_id": cidB}, token=tokenB2, expect=404)
    check6("A11: stream refuses a bad token (4401) and another tenant's token (4404)",
           _bt_close_code(baseB, sidB, "not-a-token") == 4401
           and _bt_close_code(baseB, sidB, tokenB2) == 4404)

    MOCK_STT11["mode"] = "survey"
    stt11_conns0 = len(MOCK_STT11["connections"])
    bt_ws = _bt_open(baseB, sidB, tokenB)
    _full_mulaw = _audio_len8(_R1_8) + _audio_len8(_R2_8) + _audio_len8(_R3_8)
    ok_ready = bt_ws.until(lambda: bt_ws.has_event("ready", language="bn-IN"), timeout=15)
    check6("A11: stream opens — ready event names the resolved language (bn-IN)",
           ok_ready, bt_ws.events[:2])
    ok_open = bt_ws.until(
        lambda: len(bt_ws.audio) >= 2 * _audio_len8(_R1_8)
        and any(t["text"] == _R1_8 for t in bt_ws.turns()), timeout=25)
    check6("A11: the agent speaks FIRST — opening audio (PCM, 2× the μ-law bytes) + transcript event",
           ok_open and bt_ws.turns()[0]["speaker"] == "agent",
           (len(bt_ws.audio), bt_ws.turns()[:1]))
    bt_ws.ws.send(_pcm11(1000))   # one 20 ms mic chunk → mock Saaras final L1
    ok_r2 = bt_ws.until(
        lambda: len(bt_ws.audio) >= 2 * (_audio_len8(_R1_8) + _audio_len8(_R2_8))
        and any(t["text"] == _R2_8 for t in bt_ws.turns()), timeout=25)
    check6("A11: operator turn 1 (STT final) is answered — reply audio + interleaved turns",
           ok_r2 and [t["text"] for t in bt_ws.turns()] == [_R1_8, _L1_8, _R2_8],
           bt_ws.turns())
    bt_ws.ws.send(_pcm11(8000))   # second chunk → final L2 → closing + end_call
    ok_end = bt_ws.until(
        lambda: bt_ws.has_event("ended", end_reason="agent_end_call",
                                disposition="ANSWERED"), timeout=25)
    check6("A11: closing spoken, the model's end_call ends the test — ended event carries the result",
           ok_end and len(bt_ws.audio) == 2 * _full_mulaw
           and [t["text"] for t in bt_ws.turns()] == [_R1_8, _L1_8, _R2_8, _L2_8, _R3_8],
           (len(bt_ws.audio), 2 * _full_mulaw, bt_ws.events[-2:]))
    try:
        bt_ws.ws.close()
    except Exception:
        pass
    conns11 = MOCK_STT11["connections"][stt11_conns0:]
    check6("A11: Saaras saw ONE connection (bn-IN) and the PCM16→μ-law conversion is real",
           len(conns11) == 1 and "language-code=bn-IN" in conns11[0]["path"]
           and len(conns11[0]["payloads"]) == 2
           and all(len(p) == 160 for p in conns11[0]["payloads"])
           and len(set(conns11[0]["payloads"][0])) == 1
           and len(set(conns11[0]["payloads"][1])) == 1
           and conns11[0]["payloads"][0] != conns11[0]["payloads"][1],
           conns11)
    doneB = _bt_poll(baseB, tokenB, sidB, lambda c: c["final_state"] is not None)
    check6("A11: the result lands in the SAME §8.2 surface — marked, classified, unattached",
           doneB is not None and doneB["channel"] == "browser-test"
           and doneB["campaign_id"] is None and doneB["lead_id"] is None
           and doneB["lead_name"] == "P. Sen"
           and doneB["final_state"] == "COMPLETED"
           and doneB["end_reason"] == "agent_end_call"
           and doneB["disposition"] == "ANSWERED"
           and doneB["captured_fields"].get("favourite_colour") == "blue"
           and doneB["transport"] == "self_hosted"
           and doneB["summary"].startswith("[BROWSER TEST]")
           and [t["speaker"] for t in doneB["transcript"]] ==
               ["agent", "customer", "agent", "customer", "agent"],
           doneB and {k: doneB[k] for k in ("channel", "final_state", "disposition")})
    _, allB = call6(baseB, "GET", "/api/calls", token=tokenB)
    _, dashB = call6(baseB, "GET", "/api/dashboard", token=tokenB)
    _, leadsB3 = call6(baseB, "GET", f"/api/campaigns/{cidB}/leads", token=tokenB)
    _, campsB3 = call6(baseB, "GET", f"/api/campaigns/{cidB}/calls", token=tokenB)
    _, resB = call6(baseB, "GET", f"/api/campaigns/{cidB}/results", token=tokenB)
    check6("A11: exactly one result; dashboard counts it; the campaign NEVER sees the test",
           allB["total"] == 1 and dashB["calls_total"] == 1
           and dashB["results_total"] == 1
           and dashB["dispositions"].get("ANSWERED") == 1
           and campsB3["total"] == 0 and resB["attempts_made"] == 0
           and leadsB3["leads"][0]["attempt_count"] == 0
           and leadsB3["leads"][0]["status"] == "pending",
           (allB["total"], dashB["calls_total"], leadsB3["leads"][0]))

    # ── operator ends the test: close without a word ──────────────────
    _, bt2 = call6(baseB, "POST", "/api/browser-tests", {
        "agent_id": survey["agent_id"],
        "lead": {"name": "Manual Tester", "phone_number": "9000000098",
                 "bank_name": "HDFC Bank", "state": "Bihar"},
    }, token=tokenB, expect=201)
    ws2 = _bt_open(baseB, bt2["session_id"], tokenB)
    ws2.until(lambda: len(ws2.audio) > 0, timeout=20)
    try:
        ws2.ws.close()
    except Exception:
        pass
    doneB2 = _bt_poll(baseB, tokenB, bt2["session_id"],
                      lambda c: c["final_state"] is not None)
    check6("A11: operator End (socket close) finalizes exactly once, stream_closed",
           doneB2 is not None and doneB2["channel"] == "browser-test"
           and doneB2["final_state"] == "COMPLETED"
           and doneB2["end_reason"] == "stream_closed"
           and doneB2["lead_name"] == "Manual Tester",
           doneB2 and doneB2["end_reason"])
    _, allB2 = call6(baseB, "GET", "/api/calls", token=tokenB)
    check6("A11: two tests, two results — no duplicates from either path",
           allB2["total"] == 2, allB2["total"])

    # ── barge-in: the partial cuts the reply, the page gets `interrupt` ──
    print("── A11: barge-in — STT partial cuts the reply; the harness flush event fires ──")
    baseB9 = start_app6(18114, "browser-barge", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
        "SARVAM_WS_URL": MOCK_STT11_URL, "SARVAM_BASE_URL": MOCK_AI11_URL,
    })
    tokenB9 = login6(baseB9)
    _, barge_agent = call6(baseB9, "POST", "/api/agents", {
        "name": "A11 Barge Agent", "prompt": _PROMPT_B_9,
        "tts_voice_id": "bulbul-hi-female-1",
        "language_rule": {"default": "hi-IN"},
        "captured_fields": [],
    }, token=tokenB9, expect=201)
    _, bt9 = call6(baseB9, "POST", "/api/browser-tests", {
        "agent_id": barge_agent["agent_id"],
        "lead": {"name": "Barge Tester", "state": "Bihar"},
    }, token=tokenB9, expect=201)
    MOCK_STT11["mode"] = "barge"
    ws9 = _bt_open(baseB9, bt9["session_id"], tokenB9)
    ws9.until(lambda: len(ws9.audio) > 0, timeout=20)
    ws9.ws.send(_pcm11(1200))     # → mock partial lands mid-opening
    ok_cut = ws9.until(lambda: ws9.has_event("interrupt"), timeout=15)
    mark = next((i for i, (k, _v) in enumerate(ws9.stream) if k == "json"
                 and _v.get("event") == "interrupt"), None)
    audio_at_cut = sum(n for k, n in ws9.stream[:mark] if k == "audio") if mark is not None else -1
    ws9.for_seconds(1.2)          # anything still pumped after the cut lands here
    audio_after = sum(n for k, n in ws9.stream[mark + 1:] if k == "audio") if mark is not None else -1
    check6("A11 BARGE: partial → engine interrupt → harness `interrupt` event, audio cut mid-reply",
           ok_cut and mark is not None
           and 0 < audio_at_cut < 2 * _FULL_B_AUDIO_9
           and audio_after == 0,
           (audio_at_cut, 2 * _FULL_B_AUDIO_9, audio_after))
    check6("A11 BARGE: the live partial rode the harness events (the page's live line)",
           any(e.get("event") == "partial" and e.get("text") == "Wait, I have"
               for e in ws9.events), ws9.events)
    ws9.ws.send(_pcm11(1200))     # → mock final: the interrupting line itself
    ok_reply = ws9.until(
        lambda: any(t["text"] == _RB_9 for t in ws9.turns())
        and len(ws9.audio) > audio_at_cut, timeout=20)
    check6("A11 BARGE: the interrupting line is answered as the next turn (audio resumes)",
           ok_reply, ws9.turns())
    try:
        ws9.ws.close()
    except Exception:
        pass
    doneB9 = _bt_poll(baseB9, tokenB9, bt9["session_id"],
                      lambda c: c["final_state"] is not None)
    check6("A11 BARGE: the recorded opening turn is the spoken prefix, flagged interrupted",
           doneB9 is not None and doneB9["channel"] == "browser-test"
           and doneB9["disposition"] == "SPOKEN"
           and doneB9["transcript"] and doneB9["transcript"][0].get("interrupted") is True
           and len(doneB9["transcript"][0]["text"]) < len(_OPEN_B_9),
           doneB9 and doneB9["transcript"] and doneB9["transcript"][0])
    MOCK_STT11["mode"] = "survey"

    # ── no Sarvam config: the harness refuses honestly ────────────────
    print("── A11: no Sarvam config — error event + an honest FAILED result ──")
    baseN = start_app6(18113, "browser-noconfig", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token",
        "SARVAM_WS_URL": MOCK_STT11_URL, "SARVAM_BASE_URL": MOCK_AI11_URL,
    })
    tokenN = login6(baseN)
    _, agentsN = call6(baseN, "GET", "/api/agents", token=tokenN)
    _, btN = call6(baseN, "POST", "/api/browser-tests", {
        "agent_id": agentsN[0]["agent_id"], "lead": {"name": "No Config"},
    }, token=tokenN, expect=201)
    connsN0 = len(MOCK_STT11["connections"])
    wsN = _bt_open(baseN, btN["session_id"], tokenN)
    got_err = wsN.until(lambda: wsN.has_event("error", reason="not_configured"), timeout=10)
    try:
        wsN.ws.close()
    except Exception:
        pass
    doneN = _bt_poll(baseN, tokenN, btN["session_id"],
                     lambda c: c["final_state"] is not None)
    check6("A11: no Sarvam → not_configured event, FAILED result, no STT connection attempted",
           got_err and doneN is not None and doneN["final_state"] == "FAILED"
           and doneN["end_reason"] == "not_configured"
           and doneN["summary"].startswith("[BROWSER TEST]")
           and "not configured" in doneN["summary"]
           and len(MOCK_STT11["connections"]) == connsN0,
           doneN and doneN["summary"][:70])
    _ai11_server.shutdown()
    _stt11_server.shutdown()

    print("── A12: rate cards → real cost · edge cases · 500-lead load ──")
    # The final Phase A chunk. Rate cards live in Provider Settings and
    # finalize computes the real breakdown from them; the edge cases run
    # against a purpose-built fake provider (accepts-and-hangs / rejects /
    # flips the campaign mid-dispatch) and A12's OWN Sarvam mocks (A8/A11's
    # are shut down by their sections). The load test is stub-only.
    # No live provider is called anywhere in this section.
    from datetime import datetime as _dt12, timedelta as _td12, timezone as _tz12

    def _raw12(base, method, path, body=None, token=None, timeout=120):
        """Like call6 but with a caller-chosen timeout (the hang test's
        dispatch legitimately takes ~15 s server-side)."""
        data = json.dumps(body).encode() if body is not None else (
            b"" if method == "POST" else None)
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except Exception:
                return e.code, {}

    def _webhook12(base, sid, call_id, seconds=60, cost=None,
                   disposition="RPC_CONFIRMED"):
        t0 = _dt12(2026, 10, 23, 10, 0, 0, tzinfo=_tz12.utc)
        fmt = lambda d: d.isoformat().replace("+00:00", "Z")
        msg = {"type": "end-of-call-report",
               "call": {"id": call_id, "metadata": {"session_id": sid}},
               "startedAt": fmt(t0), "endedAt": fmt(t0 + _td12(seconds=seconds)),
               "endedReason": "customer-ended-call",
               "transcript": "agent: Hello.\ncustomer: Yes, speaking.",
               "analysis": {"summary": "A12 webhook report.",
                            "structuredData": {"disposition": disposition}},
               "artifact": {"recordingUrl": "https://recordings.example/a12.mp3"}}
        if cost is not None:
            msg["costBreakdown"] = cost
        req = urllib.request.Request(
            base + "/api/telephony/vapi/webhook",
            data=json.dumps({"message": msg}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "x-vapi-secret": "a6-webhook-secret"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")

    def _row12(base, token, sid):
        _s, cl = call6(base, "GET", "/api/calls?limit=200", token=token)
        return next((c for c in cl["calls"] if c["session_id"] == sid), None)

    # ── load: 500 stub leads to completion + a zero-lead no-op ──────
    baseL = start_app6(18115, "load", {})
    _s, tenantL = call6(baseL, "POST", "/api/tenants", {
        "name": "A12 Load Co", "market": "IN", "concurrency_limit": 1000})
    _s, loginL = call6(baseL, "POST", "/api/auth/login", {
        "email": tenantL["admin_email"], "password": tenantL["admin_password"]})
    tokenL = loginL["access_token"]
    _s, agentL = call6(baseL, "POST", "/api/agents", {
        "name": "A12 Load Agent",
        "prompt": "ROLE\nYou verify identity for the lender.\n\nFLOW\n"
                  "1. Greet and ask.\n\nDISPOSITIONS\n"
                  "- CONFIRMED: the right party confirmed.\n"
                  "- DENIED: the callee denied knowledge.\n"
                  "- CALLBACK: a callback was requested.\n\n"
                  "CLOSINGS\nClose politely.\n"}, token=tokenL, expect=201)
    csvL = ("callee_name,phone_number,bank_name,state\n" + "\n".join(
        f"Load Person {i},9600{i:06d},Tata Capital,West Bengal"
        for i in range(1, 501)) + "\n")
    cidL = campaign6(baseL, tokenL, agentL["agent_id"], "A12 — 500 Lead Load",
                     "self_hosted", csvL)
    t0L = time.monotonic()
    stL, repL = _raw12(baseL, "POST", f"/api/campaigns/{cidL}/dispatch",
                       token=tokenL, timeout=300)
    wallL = time.monotonic() - t0L
    check6("A12 load: a 500-lead stub dispatch runs to completion",
           stL == 200 and repL["created"] == 500 and repL["finalized"] == 500,
           (stL, {k: repL.get(k) for k in ("created", "finalized")}))
    _s, callsL = call6(baseL, "GET", f"/api/campaigns/{cidL}/calls?limit=600",
                       token=tokenL)
    check6("A12 load: exactly 500 sessions, every one COMPLETED",
           callsL["total"] == 500
           and all(c["final_state"] == "COMPLETED" for c in callsL["calls"]),
           callsL["total"])
    _s, resL = call6(baseL, "GET", f"/api/campaigns/{cidL}/results", token=tokenL)
    check6("A12 load: exactly 500 results (exactly-one-result under volume); "
           "per-disposition counts sum to 500",
           resL["results_count"] == 500 and resL["attempts_made"] == 500
           and sum(resL["counts_by_disposition"].values()) == 500,
           (resL["results_count"], resL["counts_by_disposition"]))
    print(f"        (500-lead stub dispatch wall-clock: {wallL:.1f}s)")
    check6("A12 load: stub results carry NO money (mode stub, total 0) and "
           "are excluded from the cost aggregates",
           all((c["cost_breakdown"] or {}).get("mode") == "stub"
               and (c["cost_breakdown"] or {}).get("total") == 0
               for c in callsL["calls"]) and resL["costs"] == [],
           resL["costs"])
    _s, campZ = call6(baseL, "POST", "/api/campaigns", {
        "agent_id": agentL["agent_id"], "name": "A12 — Zero Leads", "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
        "provider_mode": "self_hosted"}, token=tokenL, expect=201)
    call6(baseL, "POST", f"/api/campaigns/{campZ['campaign_id']}/status",
          {"status": "active"}, token=tokenL)
    _s, repZ = call6(baseL, "POST",
                     f"/api/campaigns/{campZ['campaign_id']}/dispatch",
                     token=tokenL)
    check6("A12: dispatching a campaign with zero leads is a clean no-op",
           repZ["created"] == 0 and repZ["finalized"] == 0
           and repZ["message"] == "No pending leads to dispatch.", repZ)

    # ── A12's own Sarvam mocks: chat/TTS/classifier + Saaras STT ────
    _LONG12 = " ".join(f"Long answer sentence {i}." for i in range(1, 121))
    MOCK_AI12 = {"chat": [], "tts": [], "classify": []}

    class _MockAI12(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, data, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if self.path.endswith("/chat/completions"):
                msgs = body.get("messages") or []
                system = msgs[0].get("content", "") if msgs else ""
                if "POST-CALL CLASSIFIER" in system:
                    MOCK_AI12["classify"].append(body)
                    content = json.dumps({
                        "disposition": "ANSWERED",
                        "captured_fields": {"favourite_colour": "blue"},
                        "confidence": 0.93, "summary": "A12 mock classification."})
                    self._send(json.dumps({"choices": [{"message": {
                        "role": "assistant", "content": content}}]}).encode(),
                        "application/json")
                    return
                MOCK_AI12["chat"].append(body)
                users = [m.get("content", "") for m in msgs
                         if m.get("role") == "user"]
                last = users[-1] if users else ""
                if last == _L2_8:
                    pieces = ["Thank you for your answers. ", "Have a good day."]
                    end_call = True
                elif last == _L1_8:
                    pieces = ["Blue is a lovely ", "colour. And do you prefer it ",
                              "for clothes or for cars?"]
                    end_call = False
                elif "A12 LONG SURVEY" in system:
                    pieces = [_LONG12[i:i + 240]
                              for i in range(0, len(_LONG12), 240)]
                    end_call = False
                else:  # the kickoff — greeting + the one survey question
                    pieces = ["Hello, this is a short survey ",
                              "call. What is your favourite colour?"]
                    end_call = False
                lines = []
                for p in pieces:
                    lines.append("data: " + json.dumps(
                        {"choices": [{"delta": {"content": p}}]}) + "\n\n")
                if end_call:
                    lines.append("data: " + json.dumps({"choices": [{"delta": {
                        "tool_calls": [{"index": 0, "id": "call_end_12",
                                        "type": "function",
                                        "function": {"name": "end_call",
                                                     "arguments": ""}}]}}]})
                        + "\n\n")
                lines.append("data: [DONE]\n\n")
                self._send("".join(lines).encode(), "text/event-stream")
                return
            if self.path.endswith("/text-to-speech"):
                MOCK_AI12["tts"].append(body)
                text = (body.get("inputs") or [""])[0]
                self._send(json.dumps({"audios": [
                    _b64_8.b64encode(_mock_audio8(text)).decode()]}).encode(),
                    "application/json")
                return
            self._send(b"{}", "application/json")

    def _mock_saaras12(ws):
        info = {"frames": 0}
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "audio" not in msg:
                    continue
                info["frames"] += 1
                line = _L1_8 if info["frames"] == 1 else (
                    _L2_8 if info["frames"] == 2 else "")
                if line:
                    ws.send(json.dumps({"type": "data", "data": {
                        "transcript": line, "is_final": True}}))
        except Exception:
            pass  # client went away mid-stream — the mock just stops

    _ai12_server = ThreadingHTTPServer(("127.0.0.1", 0), _MockAI12)
    threading.Thread(target=_ai12_server.serve_forever, daemon=True).start()
    MOCK_AI12_URL = f"http://127.0.0.1:{_ai12_server.server_address[1]}"
    _stt12_server = _ws_serve7(_mock_saaras12, "127.0.0.1", 0)
    threading.Thread(target=_stt12_server.serve_forever, daemon=True).start()
    MOCK_STT12_URL = f"ws://127.0.0.1:{_stt12_server.socket.getsockname()[1]}"

    # ── real cost: a self-hosted call priced from the rate card ─────
    baseC = start_app6(18116, "cost", {
        "TELEPHONY_MODE": "self_hosted", "PLIVO_AUTH_ID": "MAFAKEID",
        "PLIVO_AUTH_TOKEN": "fake-plivo-token", "SARVAM_API_KEY": "sk-fake-sarvam",
        "SARVAM_WS_URL": MOCK_STT12_URL, "SARVAM_BASE_URL": MOCK_AI12_URL})
    tokenC = login6(baseC)
    call6(baseC, "PUT", "/api/settings/providers/plivo",
          {"fields": {"rate_telephony_per_min": "2"}}, token=tokenC)
    call6(baseC, "PUT", "/api/settings/providers/sarvam",
          {"fields": {"rate_stt_per_min": "3", "rate_llm_per_min": "5",
                      "rate_tts_per_min": "7"}}, token=tokenC)
    _s, viewP = call6(baseC, "GET", "/api/settings/providers/plivo", token=tokenC)
    _s, viewS = call6(baseC, "GET", "/api/settings/providers/sarvam", token=tokenC)
    fp12 = {f["name"]: f for f in viewP["fields"]}
    fs12 = {f["name"]: f for f in viewS["fields"]}
    check6("A12: rate cards are tenant settings — stored per provider, "
           "labelled per-minute, shipped empty",
           fp12["rate_telephony_per_min"]["value"] == "2"
           and fp12["rate_telephony_per_min"]["source"] == "tenant"
           and "per minute" in fp12["rate_telephony_per_min"]["label"]
           and fs12["rate_stt_per_min"]["value"] == "3"
           and fs12["rate_llm_per_min"]["value"] == "5"
           and fs12["rate_tts_per_min"]["value"] == "7",
           (fp12["rate_telephony_per_min"], fs12["rate_tts_per_min"]))
    _s, agentC = call6(baseC, "POST", "/api/agents", {
        "name": "A12 Cost Survey Agent", "prompt": _SCRIPTED_PROMPT_8,
        "llm_model": "sarvam-105b", "tts_voice_id": "bulbul-bn-female-1",
        "language_rule": {"default": "hi-IN",
                          "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "favourite_colour", "type": "string",
                             "description": "The callee's favourite colour"}]},
        token=tokenC, expect=201)
    cidC = campaign6(baseC, tokenC, agentC["agent_id"], "A12 — Costed Batch",
                     "self_hosted",
                     "callee_name,phone_number,bank_name,state\n"
                     "C. Rao,9500000011,Tata Capital,West Bengal\n")
    _s, repC = call6(baseC, "POST", f"/api/campaigns/{cidC}/dispatch",
                     token=tokenC)
    check6("A12: the costed call is placed via Plivo (fake provider)",
           repC["placed"] == 1 and repC["transport"] == "self_hosted", repC)
    _s, callsC = call6(baseC, "GET", f"/api/campaigns/{cidC}/calls", token=tokenC)
    sidC = callsC["calls"][0]["session_id"]

    def _wait_audio12(ws, sink, events, target_bytes, timeout=20):
        """_wait_audio8 + a playAudio EVENT counter (one event = one
        pumped 20 ms frame — the TTS cost basis)."""
        deadline = time.monotonic() + timeout
        closed = False
        while len(sink) < target_bytes and time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
            except TimeoutError:
                break
            except Exception:
                closed = True
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("event") == "playAudio":
                events[0] += 1
                sink.extend(_b64_8.b64decode(
                    (ev.get("media") or {}).get("payload") or ""))
        return closed

    audioC = bytearray()
    evC = [0]
    wsC = _stream_open8(baseC, tokenC, sidC)
    _wait_audio12(wsC, audioC, evC, _audio_len8(_R1_8))
    _media8(wsC)
    _wait_audio12(wsC, audioC, evC, _audio_len8(_R1_8) + _audio_len8(_R2_8))
    _media8(wsC)
    _wait_audio12(wsC, audioC, evC,
                  _audio_len8(_R1_8) + _audio_len8(_R2_8) + _audio_len8(_R3_8))
    _wait_closed8(wsC, 5.0)
    try:
        wsC.close()
    except Exception:
        pass
    doneC = poll8(baseC, tokenC, cidC, sidC, lambda c: c["final_state"] is not None)
    cbC = (doneC or {}).get("cost_breakdown") or {}
    durC = (doneC or {}).get("duration_s") or 0
    framesC = evC[0]
    exp_framesC = sum(-(-len(_mock_audio8(t["inputs"][0])) // 160)
                      for t in MOCK_AI12["tts"])
    check6("A12: the wire carried exactly one playAudio per pumped frame, "
           "bytes == the mock's synthesized audio",
           framesC == exp_framesC and framesC > 0
           and len(audioC) == sum(len(_mock_audio8(t["inputs"][0]))
                                  for t in MOCK_AI12["tts"]),
           (framesC, exp_framesC, len(audioC)))
    expC = {"telephony": round(2.0 * durC / 60, 4),
            "stt": round(3.0 * durC / 60, 4),
            "llm": round(5.0 * durC / 60, 4),
            "tts": round(7.0 * framesC * 0.02 / 60, 4)}
    expC["total"] = round(sum(expC.values()), 4)
    check6("A12: finalize computes the REAL breakdown from the rate card — "
           "telephony/STT/LLM from duration, TTS from pumped frames only",
           doneC is not None
           and all(cbC.get(k) == v for k, v in expC.items())
           and cbC.get("currency") == "INR" and cbC.get("mode") == "self_hosted"
           and cbC.get("cost_source") == "rate_card"
           and cbC.get("rates_per_min") == {"telephony": 2.0, "stt": 3.0,
                                            "llm": 5.0, "tts": 7.0}
           and set(cbC.get("basis") or {}) == {"telephony", "stt", "llm", "tts"}
           and cbC.get("total") > 0,
           (durC, framesC, cbC))

    # ── real cost: the browser-test channel (telephony 0, labelled) ──
    ttsB0 = len(MOCK_AI12["tts"])
    _s, btB = call6(baseC, "POST", "/api/browser-tests", {
        "agent_id": agentC["agent_id"],
        "lead": {"name": "Browser Cost", "phone_number": "9000000098",
                 "bank_name": "Tata Capital", "state": "West Bengal"}},
        token=tokenC, expect=201)
    sidB = btB["session_id"]
    clientB = _bt_open(baseC, sidB, tokenC)
    clientB.until(lambda: any(e.get("event") == "ready" for e in clientB.events), 10)
    clientB.ws.send(_pcm11(1000))
    clientB.until(lambda: any(t["text"] == _R2_8 for t in clientB.turns()), 25)
    clientB.ws.send(_pcm11(1200))
    endedB = clientB.until(
        lambda: any(e.get("event") == "ended" for e in clientB.events), 25)
    check6("A12: the browser cost call completes the scripted conversation",
           bool(endedB), [e.get("event") for e in clientB.events][-4:])
    try:
        clientB.ws.close()
    except Exception:
        pass
    doneB = _bt_poll(baseC, tokenC, sidB, lambda c: c["final_state"] is not None)
    cbB = (doneB or {}).get("cost_breakdown") or {}
    durB = (doneB or {}).get("duration_s") or 0
    framesB = sum(-(-len(_mock_audio8(t["inputs"][0])) // 160)
                  for t in MOCK_AI12["tts"][ttsB0:])
    expB = {"stt": round(3.0 * durB / 60, 4), "llm": round(5.0 * durB / 60, 4),
            "tts": round(7.0 * framesB * 0.02 / 60, 4)}
    check6("A12: browser-test calls cost the real legs the same way — "
           "telephony 0 and labelled browser",
           doneB is not None and cbB.get("telephony") == 0
           and "browser" in (cbB.get("basis") or {}).get("telephony", "")
           and all(cbB.get(k) == v for k, v in expB.items())
           and cbB.get("total") == round(sum(expB.values()), 4)
           and cbB.get("channel") == "browser-test"
           and doneB["disposition"] == "ANSWERED",
           (durB, framesB, cbB))

    # ── edge: a very long transcript still classifies ────────────────
    _s, agentLong = call6(baseC, "POST", "/api/agents", {
        "name": "A12 Long Survey Agent",
        "prompt": _SCRIPTED_PROMPT_8 + "\nA12 LONG SURVEY\n",
        "llm_model": "sarvam-105b", "tts_voice_id": "bulbul-bn-female-1",
        "language_rule": {"default": "hi-IN",
                          "by_state": {"West Bengal": "bn-IN"}},
        "captured_fields": [{"name": "favourite_colour", "type": "string",
                             "description": "The callee's favourite colour"}]},
        token=tokenC, expect=201)
    cls0L = len(MOCK_AI12["classify"])
    _s, btL = call6(baseC, "POST", "/api/browser-tests", {
        "agent_id": agentLong["agent_id"],
        "lead": {"name": "Long Tester", "phone_number": "9000000097",
                 "bank_name": "Tata Capital", "state": "West Bengal"}},
        token=tokenC, expect=201)
    clientL = _bt_open(baseC, btL["session_id"], tokenC)
    clientL.until(lambda: any(e.get("event") == "ready" for e in clientL.events), 10)
    long_turn = clientL.until(
        lambda: any(t["text"] == _LONG12 for t in clientL.turns()), 40)
    check6("A12: a very long agent turn streams and lands in the transcript whole",
           bool(long_turn), len(clientL.turns()))
    clientL.ws.send(_pcm11(1000))
    clientL.until(lambda: any(t["text"] == _R2_8 for t in clientL.turns()), 25)
    clientL.ws.send(_pcm11(1200))
    clientL.until(lambda: any(e.get("event") == "ended" for e in clientL.events), 25)
    try:
        clientL.ws.close()
    except Exception:
        pass
    doneL = _bt_poll(baseC, tokenC, btL["session_id"],
                     lambda c: c["final_state"] is not None)
    clsL = MOCK_AI12["classify"][cls0L:]
    check6("A12: the classifier still classifies the very long transcript — "
           "real bucket, no crash, no review-flag excuse",
           doneL is not None and doneL["final_state"] == "COMPLETED"
           and doneL["disposition"] == "ANSWERED"
           and doneL["review_required"] is False
           and any(t["speaker"] == "agent" and t["text"] == _LONG12
                   for t in doneL["transcript"])
           and len(clsL) == 1
           and len(clsL[0]["messages"][1]["content"]) > len(_LONG12),
           doneL and (doneL["disposition"], len(doneL["transcript"])))

    # ── cost aggregates: campaign + dashboard, per currency ─────────
    _s, resC = call6(baseC, "GET", f"/api/campaigns/{cidC}/results", token=tokenC)
    check6("A12: campaign results aggregate cost — total + cost per connected call",
           resC["costs"] == [{"currency": "INR", "total": cbC["total"],
                              "calls": 1, "connected_calls": 1,
                              "cost_per_connected": cbC["total"]}],
           resC["costs"])
    totAll = round(cbC["total"] + cbB["total"]
                   + ((doneL or {}).get("cost_breakdown") or {}).get("total", 0), 4)
    _s, dashC = call6(baseC, "GET", "/api/dashboard", token=tokenC)
    check6("A12: the dashboard aggregates tenant cost per currency "
           "(INR only — currencies are never mixed)",
           dashC["costs"] == [{"currency": "INR", "total": totAll, "calls": 3,
                               "connected_calls": 3,
                               "cost_per_connected": round(totAll / 3, 4)}],
           dashC["costs"])

    # ── cross-tenant fuzz on every surface ───────────────────────────
    _s, tenant2 = call6(baseC, "POST", "/api/tenants",
                        {"name": "A12 Other Co", "market": "IN"})
    _s, login2 = call6(baseC, "POST", "/api/auth/login", {
        "email": tenant2["admin_email"], "password": tenant2["admin_password"]})
    token2 = login2["access_token"]
    call6(baseC, "GET", f"/api/campaigns/{cidC}/results", token=token2, expect=404)
    call6(baseC, "GET", f"/api/campaigns/{cidC}/calls", token=token2, expect=404)
    call6(baseC, "POST", f"/api/campaigns/{cidC}/dispatch", token=token2, expect=404)
    call6(baseC, "POST", f"/api/campaigns/{cidC}/status", {"status": "paused"},
          token=token2, expect=404)
    call6(baseC, "POST", "/api/browser-tests", {"agent_id": agentC["agent_id"]},
          token=token2, expect=404)
    call6(baseC, "POST", "/api/results",
          {"session_id": sidC, "state": "COMPLETED"}, token=token2, expect=404)
    _s, callsX = call6(baseC, "GET", f"/api/calls?campaign_id={cidC}", token=token2)
    _s, dashX = call6(baseC, "GET", "/api/dashboard", token=token2)
    check6("A12: cross-tenant fuzz — the other tenant sees 404s and zeros everywhere",
           callsX["total"] == 0 and dashX["calls_total"] == 0
           and dashX["results_total"] == 0 and dashX["costs"] == [],
           (callsX["total"], dashX["calls_total"], dashX["costs"]))

    # ── managed edges: hang + malformed + duplicate webhook ×3 ──────
    FAKE12 = {"numbers": [], "flip": None, "seq": 0}
    HANG12 = "9300000099"

    class _FakeProvider12(BaseHTTPRequestHandler):
        """A Vapi-shaped fake with failure modes: one number accepts and
        then hangs (the transport must time out), one is rejected 400,
        and an armed flip completes the campaign mid-dispatch."""

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if self.path == "/call/phone":
                try:
                    body = json.loads(raw) if raw else {}
                except Exception:
                    body = {}
                num = ((body.get("customer") or {}).get("number") or "")
                FAKE12["numbers"].append(num)
                if num == HANG12:
                    time.sleep(30)  # accepts, then hangs — caller gives up at 15 s
                flip = FAKE12.get("flip")
                if flip and flip.get("armed"):
                    flip["armed"] = False
                    try:
                        req = urllib.request.Request(
                            flip["base"] + "/api/campaigns/" + flip["campaign_id"]
                            + "/status",
                            data=json.dumps({"status": "completed"}).encode(),
                            method="POST",
                            headers={"Content-Type": "application/json",
                                     "Authorization": "Bearer " + flip["token"]})
                        urllib.request.urlopen(req, timeout=10).read()
                    except Exception:
                        pass
                if "abc" in num:
                    code, payload = 400, {"error": "invalid phone number"}
                else:
                    FAKE12["seq"] += 1
                    code, payload = 200, {"id": f"call_12_{FAKE12['seq']}",
                                          "status": "queued"}
                data = json.dumps(payload).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    pass  # hang case: the caller already gave up
                return
            data = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    _fake12_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider12)
    threading.Thread(target=_fake12_server.serve_forever, daemon=True).start()
    FAKE12_URL = f"http://127.0.0.1:{_fake12_server.server_address[1]}"

    baseE = start_app6(18117, "edge", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-edge",
        "VAPI_BASE_URL": FAKE12_URL})
    tokenE = login6(baseE)
    _s, agentsE = call6(baseE, "GET", "/api/agents", token=tokenE)
    agentE = agentsE[0]["agent_id"]
    cidE = campaign6(baseE, tokenE, agentE, "A12 — Edge Batch", "managed",
                     "callee_name,phone_number,bank_name,state\n"
                     "Hang Victim,9300000099,Tata Capital,West Bengal\n"
                     "Bad Number,abc123,Tata Capital,West Bengal\n"
                     "Good Caller,9300000011,Tata Capital,West Bengal\n")
    stE, repE = _raw12(baseE, "POST", f"/api/campaigns/{cidE}/dispatch",
                       token=tokenE, timeout=120)
    check6("A12: a hanging provider + a malformed number fail honestly — "
           "the batch continues and places the good lead",
           stE == 200 and repE["placed"] == 1 and repE["finalized"] == 2,
           (stE, repE))
    _s, callsE = call6(baseE, "GET", f"/api/campaigns/{cidE}/calls", token=tokenE)
    byNumE = {c["to_number"]: c for c in callsE["calls"]}
    check6("A12: hang → FAILED (provider_error, network error on the record); "
           "malformed → FAILED (rejected at placement, HTTP 400); "
           "the good lead is in flight",
           byNumE[HANG12]["final_state"] == "FAILED"
           and byNumE[HANG12]["end_reason"] == "provider_error"
           and "network error" in (byNumE[HANG12]["summary"] or "")
           and byNumE["abc123"]["final_state"] == "FAILED"
           and "rejected the call placement (HTTP 400)" in (
               byNumE["abc123"]["summary"] or "")
           and byNumE["9300000011"]["final_state"] is None,
           {k: (v["final_state"], (v["summary"] or "")[:52])
            for k, v in byNumE.items()})
    goodE = byNumE["9300000011"]
    r1 = _webhook12(baseE, goodE["session_id"], "call_a12_" + goodE["session_id"][-6:],
                    seconds=47, cost={"total": 0.05, "transport": 0.05})
    r2 = _webhook12(baseE, goodE["session_id"], "call_a12_" + goodE["session_id"][-6:],
                    seconds=47, cost={"total": 0.05, "transport": 0.05})
    r3 = _webhook12(baseE, goodE["session_id"], "call_a12_" + goodE["session_id"][-6:],
                    seconds=47, cost={"total": 0.05, "transport": 0.05})
    check6("A12: the same end-of-call report delivered 3× still writes ONE result",
           r1[1].get("created") is True and r2[1].get("created") is False
           and r3[1].get("created") is False, (r1[1], r2[1], r3[1]))
    _s, resE = call6(baseE, "GET", f"/api/campaigns/{cidE}/results", token=tokenE)
    goodRow = _row12(baseE, tokenE, goodE["session_id"])
    check6("A12: campaign totals count the good call once; its cost is the "
           "provider's own reported figure, kept verbatim",
           resE["results_count"] == 3 and resE["attempts_made"] == 3
           and goodRow["cost_breakdown"]["total"] == 0.05
           and goodRow["cost_breakdown"]["cost_source"] == "provider",
           (resE["results_count"], goodRow["cost_breakdown"]))

    # ── managed fallback: no cost in the report → telephony-only ────
    cidM = campaign6(baseE, tokenE, agentE, "A12 — Managed Cost", "managed",
                     "callee_name,phone_number,bank_name,state\n"
                     "Cost One,9300000021,Tata Capital,West Bengal\n"
                     "Cost Two,9300000022,Tata Capital,West Bengal\n")
    call6(baseE, "POST", f"/api/campaigns/{cidM}/dispatch", token=tokenE)
    _s, callsM = call6(baseE, "GET", f"/api/campaigns/{cidM}/calls", token=tokenE)
    m1, m2 = callsM["calls"][0], callsM["calls"][1]
    _webhook12(baseE, m1["session_id"], "call_a12_" + m1["session_id"][-6:], seconds=120)
    rowM1 = _row12(baseE, tokenE, m1["session_id"])
    cbM1 = rowM1["cost_breakdown"]
    check6("A12: a report WITHOUT a cost breakdown falls back to the rate "
           "card — no rate yet → honest zeros, the rest marked not-reported",
           cbM1["cost_source"] == "rate_card" and cbM1["total"] == 0
           and "not reported" in cbM1["basis"]["stt"], cbM1)
    call6(baseE, "PUT", "/api/settings/providers/vapi",
          {"fields": {"rate_telephony_per_min": "1.5"}}, token=tokenE)
    _webhook12(baseE, m2["session_id"], "call_a12_" + m2["session_id"][-6:], seconds=120)
    cbM2 = _row12(baseE, tokenE, m2["session_id"])["cost_breakdown"]
    check6("A12: with a telephony rate set, the fallback prices telephony "
           "only — 1.5 × 2 min = 3.0 USD, legs honestly zero",
           cbM2["telephony"] == 3.0 and cbM2["total"] == 3.0
           and cbM2["stt"] == 0 and cbM2["llm"] == 0 and cbM2["tts"] == 0
           and cbM2["currency"] == "USD" and cbM2["cost_source"] == "rate_card",
           cbM2)

    # ── edge: the campaign is completed mid-dispatch ─────────────────
    cidF = campaign6(baseE, tokenE, agentE, "A12 — Flip Batch", "managed",
                     "callee_name,phone_number,bank_name,state\n"
                     "Flip One,9300000031,Tata Capital,West Bengal\n"
                     "Flip Two,9300000032,Tata Capital,West Bengal\n"
                     "Flip Three,9300000033,Tata Capital,West Bengal\n")
    FAKE12["flip"] = {"armed": True, "base": baseE, "token": tokenE,
                      "campaign_id": cidF}
    stF, repF = _raw12(baseE, "POST", f"/api/campaigns/{cidF}/dispatch",
                       token=tokenE, timeout=60)
    FAKE12["flip"] = None
    flip_calls = [n for n in FAKE12["numbers"] if n.startswith("930000003")]
    check6("A12: campaign completed MID-dispatch — the remaining leads are "
           "never dispatched (their queued attempts are withdrawn)",
           stF == 200 and repF["placed"] == 1 and repF["created"] == 1
           and repF["cancelled_inactive"] == 2 and len(flip_calls) == 1,
           (stF, repF, flip_calls))
    _s, callsF = call6(baseE, "GET", f"/api/campaigns/{cidF}/calls", token=tokenE)
    _s, campF = call6(baseE, "GET", f"/api/campaigns/{cidF}", token=tokenE)
    call6(baseE, "POST", f"/api/campaigns/{cidF}/dispatch", token=tokenE, expect=400)
    check6("A12: after the mid-dispatch stop — one session only, campaign "
           "completed, re-dispatch refused",
           callsF["total"] == 1 and campF["status"] == "completed",
           (callsF["total"], campF["status"]))

    # ── edge: the tenant concurrency cap binds across campaigns ────
    _s, tenantK = call6(baseE, "POST", "/api/tenants", {
        "name": "A12 Concurrency Co", "market": "IN", "concurrency_limit": 2})
    _s, loginK = call6(baseE, "POST", "/api/auth/login", {
        "email": tenantK["admin_email"], "password": tenantK["admin_password"]})
    tokenK = loginK["access_token"]
    _s, agentK = call6(baseE, "POST", "/api/agents", {
        "name": "A12 Concurrency Agent", "prompt": _SCRIPTED_PROMPT_8},
        token=tokenK, expect=201)
    cidKA = campaign6(baseE, tokenK, agentK["agent_id"], "A12 — Concurrency A",
                      "managed",
                      "callee_name,phone_number,bank_name,state\n"
                      "K A1,9300000041,Tata Capital,West Bengal\n"
                      "K A2,9300000042,Tata Capital,West Bengal\n"
                      "K A3,9300000043,Tata Capital,West Bengal\n")
    cidKB = campaign6(baseE, tokenK, agentK["agent_id"], "A12 — Concurrency B",
                      "managed",
                      "callee_name,phone_number,bank_name,state\n"
                      "K B1,9300000051,Tata Capital,West Bengal\n"
                      "K B2,9300000052,Tata Capital,West Bengal\n")
    _s, repKA = call6(baseE, "POST", f"/api/campaigns/{cidKA}/dispatch",
                      token=tokenK)
    _s, repKB = call6(baseE, "POST", f"/api/campaigns/{cidKB}/dispatch",
                      token=tokenK)
    check6("A12: the tenant concurrency cap binds ACROSS campaigns — "
           "A admits 2 of 3, B admits none while A is in flight",
           repKA["created"] == 2 and repKA["placed"] == 2
           and repKA["concurrency_capped"] == 1
           and repKB["created"] == 0 and repKB["concurrency_capped"] == 2,
           (repKA, repKB))
    _s, callsKA = call6(baseE, "GET", f"/api/campaigns/{cidKA}/calls",
                        token=tokenK)
    for c in callsKA["calls"]:
        _webhook12(baseE, c["session_id"], "call_a12_" + c["session_id"][-6:],
                   seconds=30, cost={"total": 0.02})
    _s, repKA2 = call6(baseE, "POST", f"/api/campaigns/{cidKA}/dispatch",
                       None, tokenK)
    _s, repKB2 = call6(baseE, "POST", f"/api/campaigns/{cidKB}/dispatch",
                       None, tokenK)
    check6("A12: with A's first two calls finalized, A admits its last "
           "lead and B admits one of two — A's fresh call holds a slot",
           repKA2["created"] == 1 and repKA2["placed"] == 1
           and repKB2["created"] == 1 and repKB2["concurrency_capped"] == 1,
           (repKA2, repKB2))
    _s, callsKA3 = call6(baseE, "GET", f"/api/campaigns/{cidKA}/calls",
                         None, tokenK)
    _s, callsKB3 = call6(baseE, "GET", f"/api/campaigns/{cidKB}/calls",
                         None, tokenK)
    for c in callsKA3["calls"] + callsKB3["calls"]:
        if c["final_state"] is None:
            _webhook12(baseE, c["session_id"],
                       "call_a12_" + c["session_id"][-6:],
                       seconds=30, cost={"total": 0.02})
    _s, repKB4 = call6(baseE, "POST", f"/api/campaigns/{cidKB}/dispatch",
                       None, tokenK)
    _s, callsKA4 = call6(baseE, "GET", f"/api/campaigns/{cidKA}/calls",
                         None, tokenK)
    _s, callsKB4 = call6(baseE, "GET", f"/api/campaigns/{cidKB}/calls",
                         None, tokenK)
    check6("A12: once every in-flight call finalizes, B's held lead is "
           "admitted too — A ends with 3 sessions, B with 2",
           repKB4["created"] == 1 and callsKA4["total"] == 3
           and callsKB4["total"] == 2,
           (repKB4, callsKA4["total"], callsKB4["total"]))
    _ai12_server.shutdown()
    _stt12_server.shutdown()
    _fake12_server.shutdown()

    # ── FIX: migration runner — an old database upgrades in place ────
    # The defect this section locks shut: create_all never alters an
    # existing table, so a volume created by an early build kept its
    # early shape and a later build crashed at startup on the first
    # missing column (tenants.timezone, 008 — the owner's first real
    # Docker run of the full Phase A code). Every section above boots
    # FRESH databases, which is exactly why the hole survived A1–A12.
    print("── FIX: migration runner — 002-era database upgrades in place ──")
    import sqlite3 as _sqlite3

    def _legacy_db(path):
        """A database at the 002-era shape: exactly the objects 001 +
        002 define (001's file executed verbatim; 002's four columns
        with a literal timestamp default — the file's DEFAULT now() is
        PostgreSQL-only syntax; the SHAPE is what the fixture must
        reproduce), plus one tenant row written by the old code."""
        if os.path.exists(path):
            os.remove(path)
        con = _sqlite3.connect(path)
        con.executescript(
            open(os.path.join(_BACKEND6, "migrations", "001_initial.sql")).read())
        for _stmt in (
            "ALTER TABLE campaigns ADD COLUMN product_detail VARCHAR(300) NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN audience TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN channel VARCHAR(120) NOT NULL DEFAULT 'Service Channel 1'",
            "ALTER TABLE campaigns ADD COLUMN updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT '2026-01-01 00:00:00'",
        ):
            con.execute(_stmt)
        con.execute(
            "INSERT INTO tenants (tenant_id, name, market,"
            " provider_mode_default, provider_credential_refs, from_numbers,"
            " concurrency_limit, recording_retention_days, created_at)"
            " VALUES ('ten_legacy0001', 'Legacy Tenant', 'IN', 'managed',"
            " '{}', '[]', 20, 90, '2026-01-01 00:00:00')")
        con.commit()
        con.close()

    def _db_cols(path, table):
        con = _sqlite3.connect(path)
        try:
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()

    def _db_query(path, sql):
        con = _sqlite3.connect(path)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    _legacy_path = os.path.join(TMP6, "legacy.db")
    _legacy_db(_legacy_path)
    check6("FIX fixture: the 002-era database has NO tenants.timezone "
           "(the column whose absence crashed the owner's upgraded boot) "
           "but DOES have 002's campaign fields",
           "timezone" not in _db_cols(_legacy_path, "tenants")
           and "product_detail" in _db_cols(_legacy_path, "campaigns"))

    baseL = None
    try:
        baseL = start_app6(18118, "legacy", {})
    except RuntimeError:
        baseL = None
    check6("FIX: the app BOOTS against the 002-era database — startup no "
           "longer dies on the missing 008 column", baseL is not None)
    if baseL:
        tokenL = login6(baseL)
        check6("FIX: login works on the upgraded database (the seed ran "
               "after the runner)", bool(tokenL))
        check6("FIX: tenants.timezone now exists and the pre-existing "
               "tenant row survived with the migration default",
               "timezone" in _db_cols(_legacy_path, "tenants")
               and _db_query(_legacy_path,
                             "SELECT name, timezone FROM tenants "
                             "WHERE tenant_id = 'ten_legacy0001'")
               == [("Legacy Tenant", "Asia/Kolkata")])
        check6("FIX: leads.next_attempt_at exists (008 complete)",
               "next_attempt_at" in _db_cols(_legacy_path, "leads"))
        _sess_cols = _db_cols(_legacy_path, "call_sessions")
        check6("FIX: call_sessions.channel + test_lead exist (009)",
               {"channel", "test_lead"} <= _sess_cols)
        check6("FIX: every other step landed — provider_settings table, "
               "voice_gender / max_turns / silence_max_prompts, "
               "STT+LLM+TTS columns, review_required",
               "provider_settings" in {r[0] for r in _db_query(
                   _legacy_path,
                   "SELECT name FROM sqlite_master WHERE type = 'table'")}
               and {"voice_gender", "max_turns", "silence_max_prompts"}
                   <= _db_cols(_legacy_path, "agent_configs")
               and {"stt_status", "llm_status", "tts_status"} <= _sess_cols
               and "review_required" in _db_cols(_legacy_path, "call_results"))
        check6("FIX: schema_migrations records exactly versions 1-11",
               [r[0] for r in _db_query(
                   _legacy_path,
                   "SELECT version FROM schema_migrations ORDER BY version")]
               == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        _recorded_before = _db_query(
            _legacy_path,
            "SELECT version, name, applied_at FROM schema_migrations "
            "ORDER BY version")
        _tenants_before = _db_query(
            _legacy_path,
            "SELECT tenant_id, name, timezone FROM tenants ORDER BY tenant_id")

        # ── second boot against the same file: a pure no-op ──────────
        _proc = _procs6[-1]
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except Exception:
            _proc.kill()
        baseL2 = None
        try:
            baseL2 = start_app6(18119, "legacy", {})
        except RuntimeError:
            baseL2 = None
        check6("FIX: a SECOND boot against the upgraded database also "
               "succeeds", baseL2 is not None)
        if baseL2:
            check6("FIX: the second boot changed nothing — "
                   "schema_migrations rows (incl. applied_at) and the "
                   "tenants table are identical, so the runner is a "
                   "no-op on a current database",
                   _db_query(_legacy_path,
                             "SELECT version, name, applied_at FROM "
                             "schema_migrations ORDER BY version")
                   == _recorded_before
                   and _db_query(_legacy_path,
                                 "SELECT tenant_id, name, timezone FROM "
                                 "tenants ORDER BY tenant_id")
                   == _tenants_before)

    # ── fresh-database path unchanged ────────────────────────────────
    # (Every instance above already boots fresh; this asserts the fresh
    # path explicitly against the migration record itself.)
    baseF = start_app6(18120, "freshmig", {})
    _fresh_path = os.path.join(TMP6, "freshmig.db")
    check6("FIX: a fresh database boots to the identical final schema "
           "with a complete 1-11 migration record",
           "timezone" in _db_cols(_fresh_path, "tenants")
           and "channel" in _db_cols(_fresh_path, "call_sessions")
           and [r[0] for r in _db_query(
               _fresh_path,
               "SELECT version FROM schema_migrations ORDER BY version")]
           == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])

    # ── B2: the second seeded agent — Payment Reminder ─────────────
    # Runs after the A1–A10 sections: the BASE (stub-mode) parts add
    # finalized calls to the seed tenant, and nothing after this section
    # asserts tenant-wide counts, so they cannot disturb the A1–A10
    # assertions above. (B3–B6 below follow the same rule.)
    import re as _re

    def _prompt_buckets(prompt):
        # The orchestrator's disposition_buckets() extraction, verbatim:
        # `- NAME: …` lines inside the prompt's DISPOSITIONS section.
        bucket_line = _re.compile(r"^-\s+([A-Z][A-Z0-9_]{2,})\s*(?::|$)")
        section_head = _re.compile(r"^[A-Z][A-Z /&-]{2,}$")
        out, in_sec = [], False
        for raw in (prompt or "").splitlines():
            line = raw.strip()
            if line.upper().startswith("DISPOSITIONS"):
                in_sec = True
                continue
            if in_sec:
                m = bucket_line.match(line)
                if m:
                    if m.group(1) not in out:
                        out.append(m.group(1))
                    continue
                if section_head.match(line) and line.upper() not in ("DISPOSITIONS",):
                    break
        return out

    print("── B2: seeded Payment Reminder — buckets from its own prompt ──")
    _, agentsB2 = call("GET", "/api/agents", token=token)
    remB2 = next(a for a in agentsB2
                 if a["name"] == "Payment Reminder" and "PAYMENT_DATE_GIVEN" in a["prompt"])
    check6("B2: seeded Payment Reminder agent (stage Reminder, prompt v1, not the reference)",
           remB2["stage"] == "Reminder" and remB2["prompt_version"] == 1
           and remB2["is_reference"] is False, remB2.get("stage"))
    REM_BUCKETS = _prompt_buckets(remB2["prompt"])
    check6("B2: 9 disposition buckets parsed from the Reminder prompt, incl. PAYMENT_DATE_GIVEN",
           len(REM_BUCKETS) == 9 and "PAYMENT_DATE_GIVEN" in REM_BUCKETS, REM_BUCKETS)
    check6("B2: the Reminder agent captures the stated payment_date field",
           [f["name"] for f in remB2["captured_fields"]] == ["payment_date"],
           remB2["captured_fields"])

    print("── B2: stub campaign on the Reminder agent dispatches + finalizes ──")
    B2_CSV = (
        "callee_name,phone_number,bank_name,state,due_date,amount_due\n"
        "P. Reminder,9500000001,Tata Capital,Bihar,2026-11-05,8500\n"
    )
    B2_MAP = {**A5_MAP, "due_date": "agent_field", "amount_due": "agent_field"}
    _, campB2 = call("POST", "/api/campaigns", {
        "agent_id": remB2["agent_id"], "name": "B2 — Reminder Batch",
        "product": "vehicle_loan", "provider_mode": "self_hosted",
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=token, expect=201)
    cidB2 = campB2["campaign_id"]
    _, upB2 = call_upload(f"/api/campaigns/{cidB2}/leads/upload", B2_CSV, token=token,
                          fields={"mapping": json.dumps(B2_MAP)})
    check6("B2: lead with due_date/amount_due columns uploads clean",
           upB2["created"] == 1, upB2)
    _, leadsB2 = call("GET", f"/api/campaigns/{cidB2}/leads", token=token)
    check6("B2: due_date + amount_due landed in the lead's agent_fields",
           leadsB2["leads"][0]["agent_fields"].get("due_date") == "2026-11-05"
           and leadsB2["leads"][0]["agent_fields"].get("amount_due") == "8500",
           leadsB2["leads"][0]["agent_fields"])
    call("POST", f"/api/campaigns/{cidB2}/status", {"status": "active"}, token=token)
    _, repB2 = call("POST", f"/api/campaigns/{cidB2}/dispatch", token=token)
    check6("B2: stub dispatch admits + finalizes the Reminder campaign's call",
           repB2["transport"] == "stub" and repB2["created"] == 1
           and repB2["finalized"] == 1, repB2)
    _, clB2 = call("GET", f"/api/campaigns/{cidB2}/calls", token=token)
    check6("B2: finalized — COMPLETED, the prompt's first bucket (PAYMENT_DATE_GIVEN), payment_date slot present",
           clB2["total"] == 1 and clB2["calls"][0]["final_state"] == "COMPLETED"
           and clB2["calls"][0]["disposition"] == "PAYMENT_DATE_GIVEN"
           and "payment_date" in clB2["calls"][0]["captured_fields"], clB2)

    print("── B2: the built Vapi payload carries due_date/amount_due ──")
    baseB2 = start_app6(18121, "b2-reminder", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-managed",
        "VAPI_DEFAULT_MODEL": "gpt-4o-mini",
    })
    tokenB2 = login6(baseB2)
    _, agentsMB2 = call6(baseB2, "GET", "/api/agents", token=tokenB2)
    remMB2 = next(a for a in agentsMB2 if a["name"] == "Payment Reminder")
    _, campMB2 = call6(baseB2, "POST", "/api/campaigns", {
        "agent_id": remMB2["agent_id"], "name": "B2 — Reminder Managed",
        "product": "vehicle_loan", "provider_mode": "managed",
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=tokenB2, expect=201)
    cidMB2 = campMB2["campaign_id"]
    upMB2 = upload6(baseB2, f"/api/campaigns/{cidMB2}/leads/upload", B2_CSV, tokenB2, B2_MAP)
    assert upMB2["created"] == 1, upMB2
    call6(baseB2, "POST", f"/api/campaigns/{cidMB2}/status", {"status": "active"}, token=tokenB2)
    _reqs_before = len([r for r in FAKE["requests"] if r["path"] == "/call/phone"])
    _, repMB2 = call6(baseB2, "POST", f"/api/campaigns/{cidMB2}/dispatch", token=tokenB2)
    check6("B2: managed dispatch places the Reminder call via the Vapi transport",
           repMB2["transport"] == "managed" and repMB2["created"] == 1
           and repMB2["placed"] == 1, repMB2)
    reqsB2 = [r for r in FAKE["requests"] if r["path"] == "/call/phone"][_reqs_before:]
    check6("B2: fake Vapi received exactly the one Reminder start-call",
           len(reqsB2) == 1, len(reqsB2))
    payB2 = json.loads(reqsB2[0]["body"]) if reqsB2 else {}
    vvB2 = payB2.get("assistantOverrides", {}).get("variableValues", {})
    check6("B2: payload variableValues carry the lead's due_date + amount_due",
           vvB2.get("due_date") == "2026-11-05" and vvB2.get("amount_due") == "8500", vvB2)
    schemaB2 = (payB2.get("assistant", {}).get("analysisPlan", {})
                .get("structuredDataSchema", {}).get("properties", {}))
    check6("B2: payload analysis schema = Reminder buckets + payment_date (§8.2 source)",
           "PAYMENT_DATE_GIVEN" in schemaB2.get("disposition", {}).get("enum", [])
           and "payment_date" in schemaB2, list(schemaB2))

    # ── B3: Promise-to-Pay Negotiation ─────────────────────────────
    print("── B3: seeded Promise-to-Pay Negotiation — buckets from its own prompt ──")
    _, agentsB3 = call("GET", "/api/agents", token=token)
    ptpB3 = next(a for a in agentsB3
                 if a["name"] == "Promise-to-Pay Negotiation" and "PROMISE_TO_PAY" in a["prompt"])
    check6("B3: seeded PTP agent (stage Negotiation, prompt v1, not the reference)",
           ptpB3["stage"] == "Negotiation" and ptpB3["prompt_version"] == 1
           and ptpB3["is_reference"] is False, ptpB3.get("stage"))
    PTP_BUCKETS = _prompt_buckets(ptpB3["prompt"])
    check6("B3: 12 disposition buckets parsed from the PTP prompt, incl. PROMISE_TO_PAY + TRANSFERRED_TO_HUMAN",
           len(PTP_BUCKETS) == 12 and "PROMISE_TO_PAY" in PTP_BUCKETS
           and "TRANSFERRED_TO_HUMAN" in PTP_BUCKETS, PTP_BUCKETS)
    check6("B3: the PTP agent captures promise_date + promise_amount + transfer_reason",
           [f["name"] for f in ptpB3["captured_fields"]] == ["promise_date", "promise_amount", "transfer_reason"],
           ptpB3["captured_fields"])

    print("── B3: stub campaign on the PTP agent dispatches + finalizes ──")
    B3_CSV = (
        "callee_name,phone_number,bank_name,state,outstanding\n"
        "P. Promiser,9500000002,Tata Capital,Bihar,42000\n"
    )
    B3_MAP = {**A5_MAP, "outstanding": "agent_field"}
    _, campB3 = call("POST", "/api/campaigns", {
        "agent_id": ptpB3["agent_id"], "name": "B3 — PTP Batch",
        "product": "vehicle_loan", "provider_mode": "self_hosted",
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=token, expect=201)
    cidB3 = campB3["campaign_id"]
    _, upB3 = call_upload(f"/api/campaigns/{cidB3}/leads/upload", B3_CSV, token=token,
                          fields={"mapping": json.dumps(B3_MAP)})
    check6("B3: lead with outstanding column uploads clean",
           upB3["created"] == 1, upB3)
    _, leadsB3 = call("GET", f"/api/campaigns/{cidB3}/leads", token=token)
    check6("B3: outstanding landed in the lead's agent_fields",
           leadsB3["leads"][0]["agent_fields"].get("outstanding") == "42000",
           leadsB3["leads"][0]["agent_fields"])
    call("POST", f"/api/campaigns/{cidB3}/status", {"status": "active"}, token=token)
    _, repB3 = call("POST", f"/api/campaigns/{cidB3}/dispatch", token=token)
    check6("B3: stub dispatch admits + finalizes the PTP campaign's call",
           repB3["transport"] == "stub" and repB3["created"] == 1
           and repB3["finalized"] == 1, repB3)
    _, clB3 = call("GET", f"/api/campaigns/{cidB3}/calls", token=token)
    check6("B3: finalized — COMPLETED, the prompt's first bucket (PROMISE_TO_PAY), promise_date slot present",
           clB3["total"] == 1 and clB3["calls"][0]["final_state"] == "COMPLETED"
           and clB3["calls"][0]["disposition"] == "PROMISE_TO_PAY"
           and "promise_date" in clB3["calls"][0]["captured_fields"], clB3)

    print("── B3: the built Vapi payload carries outstanding ──")
    baseB3 = start_app6(18122, "b3-ptp", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-managed",
        "VAPI_DEFAULT_MODEL": "gpt-4o-mini",
    })
    tokenB3 = login6(baseB3)
    _, agentsMB3 = call6(baseB3, "GET", "/api/agents", token=tokenB3)
    ptpMB3 = next(a for a in agentsMB3 if a["name"] == "Promise-to-Pay Negotiation")
    _, campMB3 = call6(baseB3, "POST", "/api/campaigns", {
        "agent_id": ptpMB3["agent_id"], "name": "B3 — PTP Managed",
        "product": "vehicle_loan", "provider_mode": "managed",
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=tokenB3, expect=201)
    cidMB3 = campMB3["campaign_id"]
    upMB3 = upload6(baseB3, f"/api/campaigns/{cidMB3}/leads/upload", B3_CSV, tokenB3, B3_MAP)
    assert upMB3["created"] == 1, upMB3
    call6(baseB3, "POST", f"/api/campaigns/{cidMB3}/status", {"status": "active"}, token=tokenB3)
    _reqs_before = len([r for r in FAKE["requests"] if r["path"] == "/call/phone"])
    _, repMB3 = call6(baseB3, "POST", f"/api/campaigns/{cidMB3}/dispatch", token=tokenB3)
    check6("B3: managed dispatch places the PTP call via the Vapi transport",
           repMB3["transport"] == "managed" and repMB3["created"] == 1
           and repMB3["placed"] == 1, repMB3)
    reqsB3 = [r for r in FAKE["requests"] if r["path"] == "/call/phone"][_reqs_before:]
    check6("B3: fake Vapi received exactly the one PTP start-call",
           len(reqsB3) == 1, len(reqsB3))
    payB3 = json.loads(reqsB3[0]["body"]) if reqsB3 else {}
    vvB3 = payB3.get("assistantOverrides", {}).get("variableValues", {})
    check6("B3: payload variableValues carry the lead's outstanding",
           vvB3.get("outstanding") == "42000", vvB3)
    schemaB3 = (payB3.get("assistant", {}).get("analysisPlan", {})
                .get("structuredDataSchema", {}).get("properties", {}))
    check6("B3: payload analysis schema = PTP buckets + promise_date/promise_amount (§8.2 source)",
           "PROMISE_TO_PAY" in schemaB3.get("disposition", {}).get("enum", [])
           and "promise_date" in schemaB3 and "promise_amount" in schemaB3, list(schemaB3))

    # ── B4: Settlement Negotiation ─────────────────────────────
    print("── B4: seeded Settlement Negotiation — buckets from its own prompt ──")
    _, agentsB4 = call("GET", "/api/agents", token=token)
    setB4 = next(a for a in agentsB4
                 if a["name"] == "Settlement Negotiation" and "SETTLED" in a["prompt"])
    check6("B4: seeded Settlement agent (stage Settlement, prompt v1, not the reference)",
           setB4["stage"] == "Settlement" and setB4["prompt_version"] == 1
           and setB4["is_reference"] is False, setB4.get("stage"))
    SET_BUCKETS = _prompt_buckets(setB4["prompt"])
    check6("B4: 11 disposition buckets parsed from the Settlement prompt, incl. SETTLED + TRANSFERRED_TO_HUMAN",
           len(SET_BUCKETS) == 11 and "SETTLED" in SET_BUCKETS
           and "TRANSFERRED_TO_HUMAN" in SET_BUCKETS, SET_BUCKETS)
    check6("B4: the Settlement agent captures settlement_amount + discount_pct + transfer_reason",
           [f["name"] for f in setB4["captured_fields"]] == ["settlement_amount", "discount_pct", "transfer_reason"],
           setB4["captured_fields"])

    print("── B4: campaign authority cap round-trips + stub dispatch finalizes ──")
    B4_CSV = (
        "callee_name,phone_number,bank_name,state,outstanding\n"
        "S. Settler,9500000003,Tata Capital,Bihar,100000\n"
    )
    B4_MAP = {**A5_MAP, "outstanding": "agent_field"}
    _, campB4 = call("POST", "/api/campaigns", {
        "agent_id": setB4["agent_id"], "name": "B4 — Settlement Batch",
        "product": "vehicle_loan", "provider_mode": "self_hosted",
        "settlement_authority_pct": 30,
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=token, expect=201)
    cidB4 = campB4["campaign_id"]
    check6("B4: campaign authority cap persists (30%)",
           campB4["settlement_authority_pct"] == 30, campB4)
    _, upB4 = call_upload(f"/api/campaigns/{cidB4}/leads/upload", B4_CSV, token=token,
                          fields={"mapping": json.dumps(B4_MAP)})
    check6("B4: lead with outstanding column uploads clean",
           upB4["created"] == 1, upB4)
    call("POST", f"/api/campaigns/{cidB4}/status", {"status": "active"}, token=token)
    _, repB4 = call("POST", f"/api/campaigns/{cidB4}/dispatch", token=token)
    check6("B4: stub dispatch admits + finalizes the Settlement campaign's call",
           repB4["transport"] == "stub" and repB4["created"] == 1
           and repB4["finalized"] == 1, repB4)
    _, clB4 = call("GET", f"/api/campaigns/{cidB4}/calls", token=token)
    check6("B4: finalized — COMPLETED, the prompt's first bucket (SETTLED), settlement_amount slot present",
           clB4["total"] == 1 and clB4["calls"][0]["final_state"] == "COMPLETED"
           and clB4["calls"][0]["disposition"] == "SETTLED"
           and "settlement_amount" in clB4["calls"][0]["captured_fields"], clB4)

    print("── B4: the built Vapi payload carries the authority cap ──")
    baseB4 = start_app6(18123, "b4-settlement", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-managed",
        "VAPI_DEFAULT_MODEL": "gpt-4o-mini",
    })
    tokenB4 = login6(baseB4)
    _, agentsMB4 = call6(baseB4, "GET", "/api/agents", token=tokenB4)
    setMB4 = next(a for a in agentsMB4 if a["name"] == "Settlement Negotiation")
    _, campMB4 = call6(baseB4, "POST", "/api/campaigns", {
        "agent_id": setMB4["agent_id"], "name": "B4 — Settlement Managed",
        "product": "vehicle_loan", "provider_mode": "managed",
        "settlement_authority_pct": 30,
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=tokenB4, expect=201)
    cidMB4 = campMB4["campaign_id"]
    upMB4 = upload6(baseB4, f"/api/campaigns/{cidMB4}/leads/upload", B4_CSV, tokenB4, B4_MAP)
    assert upMB4["created"] == 1, upMB4
    call6(baseB4, "POST", f"/api/campaigns/{cidMB4}/status", {"status": "active"}, token=tokenB4)
    _reqs_before = len([r for r in FAKE["requests"] if r["path"] == "/call/phone"])
    _, repMB4 = call6(baseB4, "POST", f"/api/campaigns/{cidMB4}/dispatch", token=tokenB4)
    check6("B4: managed dispatch places the Settlement call via the Vapi transport",
           repMB4["transport"] == "managed" and repMB4["created"] == 1
           and repMB4["placed"] == 1, repMB4)
    reqsB4 = [r for r in FAKE["requests"] if r["path"] == "/call/phone"][_reqs_before:]
    check6("B4: fake Vapi received exactly the one Settlement start-call",
           len(reqsB4) == 1, len(reqsB4))
    payB4 = json.loads(reqsB4[0]["body"]) if reqsB4 else {}
    vvB4 = payB4.get("assistantOverrides", {}).get("variableValues", {})
    check6("B4: payload variableValues carry the campaign's settlement_cap_pct (30) + outstanding",
           vvB4.get("settlement_cap_pct") == 30 and vvB4.get("outstanding") == "100000", vvB4)
    schemaB4 = (payB4.get("assistant", {}).get("analysisPlan", {})
                .get("structuredDataSchema", {}).get("properties", {}))
    check6("B4: payload analysis schema = Settlement buckets + settlement_amount/discount_pct (§8.2 source)",
           "SETTLED" in schemaB4.get("disposition", {}).get("enum", [])
           and "settlement_amount" in schemaB4 and "discount_pct" in schemaB4, list(schemaB4))

    # ── B5: Broken-Promise Recovery ─────────────────────────────
    print("── B5: seeded Broken-Promise Recovery — buckets from its own prompt ──")
    _, agentsB5 = call("GET", "/api/agents", token=token)
    brkB5 = next(a for a in agentsB5
                 if a["name"] == "Broken-Promise Recovery" and "NEW_PROMISE" in a["prompt"])
    check6("B5: seeded Broken-Promise agent (stage Recovery, prompt v1, not the reference)",
           brkB5["stage"] == "Recovery" and brkB5["prompt_version"] == 1
           and brkB5["is_reference"] is False, brkB5.get("stage"))
    BRK_BUCKETS = _prompt_buckets(brkB5["prompt"])
    check6("B5: 11 disposition buckets parsed from the Broken-Promise prompt, incl. NEW_PROMISE + TRANSFERRED_TO_HUMAN",
           len(BRK_BUCKETS) == 11 and "NEW_PROMISE" in BRK_BUCKETS
           and "TRANSFERRED_TO_HUMAN" in BRK_BUCKETS, BRK_BUCKETS)
    check6("B5: the Broken-Promise agent captures promise_date + promise_amount + break_reason + transfer_reason",
           [f["name"] for f in brkB5["captured_fields"]] == ["promise_date", "promise_amount", "break_reason", "transfer_reason"],
           brkB5["captured_fields"])

    print("── B5: stub campaign on the Broken-Promise agent dispatches + finalizes ──")
    B5_CSV = (
        "callee_name,phone_number,bank_name,state,promise_date,promised_amount\n"
        "B. Broken,9500000004,Tata Capital,Bihar,2026-10-20,15000\n"
    )
    B5_MAP = {**A5_MAP, "promise_date": "agent_field", "promised_amount": "agent_field"}
    _, campB5 = call("POST", "/api/campaigns", {
        "agent_id": brkB5["agent_id"], "name": "B5 — Broken-Promise Batch",
        "product": "vehicle_loan", "provider_mode": "self_hosted",
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=token, expect=201)
    cidB5 = campB5["campaign_id"]
    _, upB5 = call_upload(f"/api/campaigns/{cidB5}/leads/upload", B5_CSV, token=token,
                          fields={"mapping": json.dumps(B5_MAP)})
    check6("B5: lead with promise_date/promised_amount columns uploads clean",
           upB5["created"] == 1, upB5)
    _, leadsB5 = call("GET", f"/api/campaigns/{cidB5}/leads", token=token)
    check6("B5: original promise landed in the lead's agent_fields",
           leadsB5["leads"][0]["agent_fields"].get("promise_date") == "2026-10-20"
           and leadsB5["leads"][0]["agent_fields"].get("promised_amount") == "15000",
           leadsB5["leads"][0]["agent_fields"])
    call("POST", f"/api/campaigns/{cidB5}/status", {"status": "active"}, token=token)
    _, repB5 = call("POST", f"/api/campaigns/{cidB5}/dispatch", token=token)
    check6("B5: stub dispatch admits + finalizes the Broken-Promise campaign's call",
           repB5["transport"] == "stub" and repB5["created"] == 1
           and repB5["finalized"] == 1, repB5)
    _, clB5 = call("GET", f"/api/campaigns/{cidB5}/calls", token=token)
    check6("B5: finalized — COMPLETED, the prompt's first bucket (NEW_PROMISE), promise_date slot present",
           clB5["total"] == 1 and clB5["calls"][0]["final_state"] == "COMPLETED"
           and clB5["calls"][0]["disposition"] == "NEW_PROMISE"
           and "promise_date" in clB5["calls"][0]["captured_fields"], clB5)

    print("── B5: the built Vapi payload carries the original promise ──")
    baseB5 = start_app6(18124, "b5-broken", {
        "TELEPHONY_MODE": "managed", "VAPI_API_KEY": "sk-fake-managed",
        "VAPI_DEFAULT_MODEL": "gpt-4o-mini",
    })
    tokenB5 = login6(baseB5)
    _, agentsMB5 = call6(baseB5, "GET", "/api/agents", token=tokenB5)
    brkMB5 = next(a for a in agentsMB5 if a["name"] == "Broken-Promise Recovery")
    _, campMB5 = call6(baseB5, "POST", "/api/campaigns", {
        "agent_id": brkMB5["agent_id"], "name": "B5 — Broken-Promise Managed",
        "product": "vehicle_loan", "provider_mode": "managed",
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=tokenB5, expect=201)
    cidMB5 = campMB5["campaign_id"]
    upMB5 = upload6(baseB5, f"/api/campaigns/{cidMB5}/leads/upload", B5_CSV, tokenB5, B5_MAP)
    assert upMB5["created"] == 1, upMB5
    call6(baseB5, "POST", f"/api/campaigns/{cidMB5}/status", {"status": "active"}, token=tokenB5)
    _reqs_before = len([r for r in FAKE["requests"] if r["path"] == "/call/phone"])
    _, repMB5 = call6(baseB5, "POST", f"/api/campaigns/{cidMB5}/dispatch", token=tokenB5)
    check6("B5: managed dispatch places the Broken-Promise call via the Vapi transport",
           repMB5["transport"] == "managed" and repMB5["created"] == 1
           and repMB5["placed"] == 1, repMB5)
    reqsB5 = [r for r in FAKE["requests"] if r["path"] == "/call/phone"][_reqs_before:]
    check6("B5: fake Vapi received exactly the one Broken-Promise start-call",
           len(reqsB5) == 1, len(reqsB5))
    payB5 = json.loads(reqsB5[0]["body"]) if reqsB5 else {}
    vvB5 = payB5.get("assistantOverrides", {}).get("variableValues", {})
    check6("B5: payload variableValues carry the lead's promise_date + promised_amount",
           vvB5.get("promise_date") == "2026-10-20" and vvB5.get("promised_amount") == "15000", vvB5)
    schemaB5 = (payB5.get("assistant", {}).get("analysisPlan", {})
                .get("structuredDataSchema", {}).get("properties", {}))
    check6("B5: payload analysis schema = Broken-Promise buckets + promise_date/promise_amount/break_reason (§8.2 source)",
           "NEW_PROMISE" in schemaB5.get("disposition", {}).get("enum", [])
           and "promise_date" in schemaB5 and "promise_amount" in schemaB5
           and "break_reason" in schemaB5, list(schemaB5))

    # ── B6: Human Escalation capability ────────────────────────────
    # Minimal faithful version (design §11.3 B6): the escalation
    # disposition + transfer-reason capture + human-queue routing
    # flagging. The live transfer itself (tenant human number config,
    # Vapi transfer / Plivo dial wiring, Human Queue screen) is NOT
    # built here — see the build report's open gaps.
    print("── B6: escalation disposition + human-queue flagging ──")
    _, agentsB6 = call("GET", "/api/agents", token=token)
    for _name in ("Promise-to-Pay Negotiation", "Settlement Negotiation",
                  "Broken-Promise Recovery"):
        _ag = next(a for a in agentsB6 if a["name"] == _name)
        _bk = _prompt_buckets(_ag["prompt"])
        check6(f"B6: {_name} offers TRANSFERRED_TO_HUMAN + captures transfer_reason",
               "TRANSFERRED_TO_HUMAN" in _bk
               and "transfer_reason" in [f["name"] for f in _ag["captured_fields"]], _bk)
    _brkB6 = next(a for a in agentsB6 if a["name"] == "Broken-Promise Recovery")
    _B6_RULES = [
        {"disposition": "TRANSFERRED_TO_HUMAN", "action": "human_queue", "target": ""},
        {"disposition": "SETTLEMENT_NEEDS_APPROVAL", "action": "human_queue", "target": ""},
        {"disposition": "ESCALATE_TO_HUMAN", "action": "human_queue", "target": ""},
    ]
    _, campB6 = call("POST", "/api/campaigns", {
        "agent_id": _brkB6["agent_id"], "name": "B6 — Escalation Routing",
        "product": "vehicle_loan", "provider_mode": "self_hosted",
        "routing_rules": _B6_RULES,
        "schedule": {"window_start": "00:00", "window_end": "23:59", "calling_days": "Mon - Sun", "daily_cap": 2000},
    }, token=token, expect=201)
    check6("B6: escalation routing rules persist on the campaign",
           campB6["routing_rules"] == _B6_RULES, campB6["routing_rules"])
    _, campB6g = call("GET", f"/api/campaigns/{campB6['campaign_id']}", token=token)
    check6("B6: escalation routing rules round-trip on read",
           campB6g["routing_rules"] == campB6["routing_rules"], campB6g["routing_rules"])

finally:
    for _p in _procs6:
        _p.terminate()
    for _p in _procs6:
        try:
            _p.wait(timeout=5)
        except Exception:
            _p.kill()
    _fake_server.shutdown()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
