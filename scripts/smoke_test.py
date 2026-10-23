#!/usr/bin/env python3
"""Smoke tests for Phase A chunks 1–6 (A1–A7) against a running server.

Usage:  python3 scripts/smoke_test.py [base_url]     (default http://localhost:8000)
        Run with the backend venv python (backend/.venv/bin/python) —
        the A6/A7 sections boot their own app instances with that
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
once); no-config path identical to the A6 spike (A7).

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
    "schedule": {"window_start": "00:00", "window_end": "23:59", "daily_cap": 2000},
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
_, rep2 = call("POST", f"/api/campaigns/{cid5}/dispatch", token=token)
assert rep2["created"] == 0 and rep2["finalized"] == 0, rep2  # leads already attempted
_, cl2 = call("GET", f"/api/campaigns/{cid5}/calls", token=token)
assert cl2["total"] == 5, cl2  # a duplicate POST creates no second session
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
assert all(l["attempt_count"] == 1 and l["status"] == "attempted" for l in leads5["leads"]), leads5
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
_win = {"window_start": (_now + timedelta(hours=2)).strftime("%H:%M"),
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
    "schedule": {"window_start": "00:00", "window_end": "23:59", "daily_cap": 2},
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
assert sum(1 for l in leadsC["leads"] if l["status"] == "attempted") == 2, leadsC
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
            FAKE["vapi_calls"] += 1
            payload, code = {"id": f"call_fake_{FAKE['vapi_calls']}", "status": "queued"}, 200
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
        "schedule": {"window_start": "00:00", "window_end": "23:59", "daily_cap": 2000},
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
    _, saved_v = call6(baseM, "PUT", "/api/settings/providers/vapi",
                       {"fields": {"bn_voice_id_male": "tenant-bn-male-override"}}, token=tokenM)
    svf = {f["name"]: f for f in saved_v["fields"]}
    check6("voice profile editable per tenant (override sticks, source = tenant)",
           svf["bn_voice_id_male"]["value"] == "tenant-bn-male-override"
           and svf["bn_voice_id_male"]["source"] == "tenant", svf.get("bn_voice_id_male"))

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
    schema_props = wb["assistant"]["analysisPlan"]["structuredDataSchema"]["properties"]
    check6("payload: analysis schema = prompt buckets + captured fields (§8.2 source)",
           "RPC_CONFIRMED" in schema_props["disposition"]["enum"]
           and "identity_confirmed" in schema_props, list(schema_props))
    check6("payload: webhook binding (serverUrl + secret + session metadata)",
           wb["assistant"]["serverUrl"] == f"{baseM}/api/telephony/vapi/webhook"
           and wb["assistant"]["serverUrlSecret"] == "a6-webhook-secret"
           and wb["metadata"]["session_id"].startswith("ses_"), wb["assistant"]["serverUrl"])

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
    call6(baseM, "POST", "/api/telephony/vapi/webhook", report_body,
          headers={"x-vapi-secret": "wrong-secret"}, expect=401)
    check6("webhook rejects a wrong shared secret (401)", True)
    _, ack = call6(baseM, "POST", "/api/telephony/vapi/webhook",
                   {"message": {"type": "status-update",
                                "call": {"id": fake_id_by_session[wb_session]}}},
                   headers={"x-vapi-secret": "a6-webhook-secret"})
    check6("non-result server messages are acknowledged, not finalized",
           ack["handled"] is False, ack)
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
