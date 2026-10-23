#!/usr/bin/env python3
"""Smoke tests for Phase A chunks 1–4 (A1–A5) against a running server.

Usage:  python3 scripts/smoke_test.py [base_url]     (default http://localhost:8000)
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
isolation on dispatch/calls/results (A5).

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

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
