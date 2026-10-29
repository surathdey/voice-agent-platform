#!/usr/bin/env python3
"""Smoke tests for the Twilio self-hosted transport (Plivo alternative).

Mirrors the A6 smoke style (scripts/smoke_test.py): pure checks first,
then a live-server section that boots uvicorn on a FRESH sqlite DB and
drives the real routes. No real provider is ever called: the transport
placement test points at a local fake Twilio server, and the live-server
section exercises only provider-facing routes (answer TwiML, status
callbacks, media-stream WebSocket) with seeded session rows.

Covers:
  - TwiML answer generation (Connect/Stream, session-bound URL + Parameter)
  - Twilio media-event parsing (connected/start/media/mark/stop/unknown)
  - terminal status mapping + machine-answer (AnsweredBy) handling
  - Twilio request-signature verification (HMAC-SHA1, not Plivo's SHA256)
  - PSTN selection (plivo default | twilio | invalid | disabled row)
  - Twilio credential readiness (account_sid and legacy auth_id spellings)
  - TwilioTransport.run placement against a fake Twilio API (201 + sid,
    and the 400 honest-failure shape)
  - resolve_transport routing (twilio selected+configured -> Twilio
    transport; otherwise Plivo, unchanged)
  - live-server: answer route, stream finalize through the shared
    helper (Twilio-labelled spike shape), status finalize + idempotency,
    signature accept/reject, stream-status ack

Usage: backend/.venv/bin/python scripts/smoke_twilio_transport.py
Run from the repo root. Exits 0 only when every check passes.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
VENV_PY = os.path.join(BACKEND, ".venv", "bin", "python")
APP_PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable

passed = failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {extra}")


# ── Part A: pure checks (no server) ──────────────────────────────────────
print("── Twilio transport: pure checks ──")
from app import providers as prov
from app import transports
from app.config import settings

# 1. TwiML answer generation
xml_text = transports.twilio_answer_xml("ses_twtest")
root = ET.fromstring(xml_text)
stream_el = root.find("Connect/Stream")
param = stream_el.find("Parameter") if stream_el is not None else None
check("answer TwiML: Response/Connect/Stream with session-bound ws URL + Parameter",
      root.tag == "Response" and stream_el is not None
      and stream_el.text is None
      and stream_el.get("url") == "ws://localhost:8000/api/telephony/twilio/stream/ses_twtest"
      and param is not None and param.get("name") == "session_id"
      and param.get("value") == "ses_twtest"
      and (stream_el.get("statusCallback") or "").endswith("/stream-status/ses_twtest"),
      xml_text[:200])

# 2. Media-event parsing
check("parse: connected keeps streamSid",
      transports.parse_twilio_stream_event(
          {"event": "connected", "streamSid": "MZ1"}) == ("connected", "MZ1", b""))
check("parse: start reads nested streamSid",
      transports.parse_twilio_stream_event(
          {"event": "start", "start": {"streamSid": "MZ2"}}) == ("start", "MZ2", b""))
check("parse: media decodes base64 mulaw payload",
      transports.parse_twilio_stream_event(
          {"event": "media", "streamSid": "MZ3",
           "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}})
      == ("media", "MZ3", b"\xff" * 160))
check("parse: undecodable media payload yields empty audio",
      transports.parse_twilio_stream_event(
          {"event": "media", "streamSid": "MZ3", "media": {"payload": "!!!"}})
      == ("media", "MZ3", b""))
check("parse: mark / stop / unknown",
      transports.parse_twilio_stream_event({"event": "mark", "streamSid": "MZ4"})[0] == "mark"
      and transports.parse_twilio_stream_event({"event": "stop", "streamSid": "MZ4"})[0] == "stop"
      and transports.parse_twilio_stream_event({"event": "dtmf"})[0] == "unknown"
      and transports.parse_twilio_stream_event("junk")[0] == "unknown")

# 3. Terminal status mapping
check("terminal: completed/busy/failed/no-answer/canceled map",
      transports.twilio_terminal_outcome("completed", {"CallDuration": "31"})["final_state"] == "COMPLETED"
      and transports.twilio_terminal_outcome("completed", {"CallDuration": "31"})["duration_s"] == 31
      and transports.twilio_terminal_outcome("busy", {})["final_state"] == "BUSY"
      and transports.twilio_terminal_outcome("failed", {})["end_reason"] == "provider_error"
      and transports.twilio_terminal_outcome("no-answer", {})["final_state"] == "NO_ANSWER"
      and transports.twilio_terminal_outcome("canceled", {})["final_state"] == "FAILED")
check("terminal: machine AnsweredBy maps to VOICEMAIL (Plivo machine parity)",
      transports.twilio_terminal_outcome(
          "completed", {"AnsweredBy": "machine_start"})["final_state"] == "VOICEMAIL")
check("terminal: non-terminal statuses return None",
      transports.twilio_terminal_outcome("ringing", {}) is None
      and transports.twilio_terminal_outcome("in-progress", {}) is None
      and transports.twilio_terminal_outcome("initiated", {}) is None)
check("terminal: RecordingUrl carried through",
      transports.twilio_terminal_outcome(
          "completed", {"RecordingUrl": "https://example.invalid/rec.mp3"})["recording_url"]
      == "https://example.invalid/rec.mp3")

# 4. Signature verification (SHA1 — must differ from Plivo's SHA256)
_sig_url = "https://example.invalid/api/telephony/twilio/status/ses_x"
_sig_params = {"CallSid": "CA1", "CallStatus": "completed"}
_sig_base = _sig_url + "".join(f"{k}{_sig_params[k]}" for k in sorted(_sig_params))
_sig_good = base64.b64encode(hmac.new(b"tok-secret", _sig_base.encode(),
                                      hashlib.sha1).digest()).decode()
check("signature: correct HMAC-SHA1 verifies",
      transports.verify_twilio_signature(_sig_url, _sig_params, "tok-secret", _sig_good))
check("signature: wrong value / empty token / empty signature rejected",
      not transports.verify_twilio_signature(_sig_url, _sig_params, "tok-secret", "bogus")
      and not transports.verify_twilio_signature(_sig_url, _sig_params, "", _sig_good)
      and not transports.verify_twilio_signature(_sig_url, _sig_params, "tok-secret", ""))

# 5. Stream outcomes: Plivo wrapper unchanged, Twilio label differs only
po = transports.plivo_stream_outcome(10, stt_status="", transcript=[])
to = transports.twilio_stream_outcome(10, stt_status="", transcript=[])
check("stream outcomes: Plivo shape unchanged, Twilio label only",
      po["final_state"] == to["final_state"] == "COMPLETED"
      and "Plivo call connected" in po["summary"]
      and "Twilio call connected" in to["summary"]
      and po["duration_s"] == to["duration_s"] == 10)

# 6. Selection + readiness on a temp DB
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base  # noqa: E402  (models' Base via app.db)
from app.models import CallSession, ProviderSetting, Tenant  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="twilio-smoke-")
_eng = create_engine(f"sqlite:///{_tmp}/sel.db")
Base.metadata.create_all(_eng)
_Session = sessionmaker(bind=_eng)
_db = _Session()
_db.add(Tenant(tenant_id="ten_sel", name="Sel"))
_db.commit()


def _set_provider(provider, fields, enabled=True):
    row = _db.query(ProviderSetting).filter(
        ProviderSetting.tenant_id == "ten_sel",
        ProviderSetting.provider == provider).first()
    if row is None:
        row = ProviderSetting(tenant_id="ten_sel", provider=provider,
                              fields=fields, enabled=enabled)
        _db.add(row)
    else:
        row.fields = fields
        row.enabled = enabled
    _db.commit()


check("selection: default is plivo with no pstn row",
      prov.self_hosted_pstn(_db, "ten_sel") == "plivo")
_set_provider("pstn", {"provider": "twilio"})
check("selection: tenant pstn row twilio wins",
      prov.self_hosted_pstn(_db, "ten_sel") == "twilio")
_set_provider("pstn", {"provider": "bogus"})
check("selection: unknown value falls back to plivo",
      prov.self_hosted_pstn(_db, "ten_sel") == "plivo")
_set_provider("pstn", {"provider": "twilio"}, enabled=False)
check("selection: disabled pstn row falls back to plivo",
      prov.self_hosted_pstn(_db, "ten_sel") == "plivo")
_set_provider("pstn", {"provider": "twilio"})
check("readiness: account_sid + token counts as configured",
      (lambda: (_set_provider("twilio", {"account_sid": "ACx", "auth_token": "tok"}),
                prov.twilio_configured(_db, "ten_sel"))[1])())
check("readiness: legacy auth_id spelling counts as configured",
      (lambda: (_set_provider("twilio", {"auth_id": "ACy", "auth_token": "tok"}),
                prov.twilio_configured(_db, "ten_sel"))[1])())
check("readiness: missing token / disabled row do not count",
      (lambda: (_set_provider("twilio", {"account_sid": "ACx", "auth_token": ""}),
                not prov.twilio_configured(_db, "ten_sel"))[1])()
      and (lambda: (_set_provider("twilio", {"account_sid": "ACx", "auth_token": "tok"},
                                  enabled=False),
                not prov.twilio_configured(_db, "ten_sel"))[1])())

# 7. TwilioTransport.run against a fake Twilio API
FAKE = {"requests": []}


class _FakeTwilio(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        FAKE["requests"].append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        if FAKE.get("fail"):
            payload, code = {"message": "Bad number", "code": 21211}, 400
        else:
            payload, code = {"sid": "CAfake123", "status": "queued"}, 201
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


from http.server import ThreadingHTTPServer as _THS
_fake_srv = _THS(("127.0.0.1", 0), _FakeTwilio)
import threading as _th
_th.Thread(target=_fake_srv.serve_forever, daemon=True).start()
FAKE_BASE = f"http://127.0.0.1:{_fake_srv.server_address[1]}"
_old_twilio_base = settings.TWILIO_BASE_URL
settings.TWILIO_BASE_URL = FAKE_BASE
try:
    _set_provider("twilio", {"account_sid": "ACfake", "auth_token": "tok-fake"})
    _db.add(CallSession(session_id="ses_run1", tenant_id="ten_sel",
                        attempt_id="att_run1", provider_mode="self_hosted",
                        state="QUEUED", to_number="+15550002222",
                        from_number="+15550001111"))
    _db.commit()
    _sess = _db.get(CallSession, "ses_run1")
    _out = transports.TwilioTransport().run(
        _db, _sess, None, None, 0, datetime.utcnow())
    _req = FAKE["requests"][-1]
    _form = {k: v[0] for k, v in urllib.parse.parse_qs(_req["body"]).items()}
    check("transport.run: fake Twilio got Calls.json with From/To/Url/StatusCallback/Record + Basic auth",
          _req["path"] == "/2010-04-01/Accounts/ACfake/Calls.json"
          and _req["headers"].get("Authorization") == "Basic " + base64.b64encode(
              b"ACfake:tok-fake").decode()
          and _form.get("From") == "+15550001111"
          and _form.get("To") == "+15550002222"
          and _form.get("Url") == "http://localhost:8000/api/telephony/twilio/answer/ses_run1"
          and _form.get("StatusCallback") == "http://localhost:8000/api/telephony/twilio/status/ses_run1"
          and _form.get("Record") == "true", _form)
    _raw_form = urllib.parse.parse_qs(_req["body"])
    check("transport.run: StatusCallbackEvent sent as repeated params (not one space-separated value)",
          _raw_form.get("StatusCallbackEvent") == ["initiated", "ringing", "answered", "completed"],
          _raw_form.get("StatusCallbackEvent"))
    check("transport.run: 201 + sid places the call (None, DIALING, provider_call_id)",
          _out is None and _sess.state == "DIALING"
          and _sess.provider_call_id == "CAfake123", str(_out))
    FAKE["fail"] = True
    _db.add(CallSession(session_id="ses_run2", tenant_id="ten_sel",
                        attempt_id="att_run2", provider_mode="self_hosted",
                        state="QUEUED", to_number="+15550002223",
                        from_number="+15550001111"))
    _db.commit()
    _sess2 = _db.get(CallSession, "ses_run2")
    _out2 = transports.TwilioTransport().run(
        _db, _sess2, None, None, 0, datetime.utcnow())
    check("transport.run: provider rejection returns honest FAILED (Twilio-labelled)",
          _out2 is not None and _out2["final_state"] == "FAILED"
          and "Twilio" in _out2["summary"], str(_out2)[:200])
    FAKE["fail"] = False
    _set_provider("twilio", {"account_sid": "", "auth_token": ""})
    _db.add(CallSession(session_id="ses_run3", tenant_id="ten_sel",
                        attempt_id="att_run3", provider_mode="self_hosted",
                        state="QUEUED", to_number="+15550002224",
                        from_number="+15550001111"))
    _db.commit()
    _sess3 = _db.get(CallSession, "ses_run3")
    _out3 = transports.TwilioTransport().run(
        _db, _sess3, None, None, 0, datetime.utcnow())
    check("transport.run: no Twilio credentials fails honestly without placing",
          _out3 is not None and _out3["final_state"] == "FAILED"
          and "no Twilio credentials" in _out3["summary"], str(_out3)[:200])
finally:
    settings.TWILIO_BASE_URL = _old_twilio_base
    _fake_srv.shutdown()

# 8. resolve_transport routing
from app import orchestrator as orch
_old_mode = settings.TELEPHONY_MODE
settings.TELEPHONY_MODE = "self_hosted"
try:
    _set_provider("sarvam", {"api_key": "sk-fake-sarvam"})
    _set_provider("twilio", {"account_sid": "ACx", "auth_token": "tok"})
    _set_provider("pstn", {"provider": "twilio"})
    _t, _n, _r = orch.resolve_transport(_db, "ten_sel", "self_hosted")
    check("resolve: twilio selected + configured routes to TwilioTransport",
          isinstance(_t, transports.TwilioTransport) and _r is True, type(_t).__name__)
    _set_provider("plivo", {"auth_id": "MAx", "auth_token": "tok"})
    _set_provider("pstn", {"provider": "plivo"})
    _t2, _n2, _r2 = orch.resolve_transport(_db, "ten_sel", "self_hosted")
    check("resolve: plivo selected routes to PlivoTransport (unchanged)",
          isinstance(_t2, transports.PlivoTransport) and _r2 is True, type(_t2).__name__)
    _set_provider("twilio", {"account_sid": "", "auth_token": ""})
    _set_provider("pstn", {"provider": "twilio"})
    _t3, _n3, _r3 = orch.resolve_transport(_db, "ten_sel", "self_hosted")
    check("resolve: twilio selected but unconfigured falls back to PlivoTransport",
          isinstance(_t3, transports.PlivoTransport) and _r3 is True, type(_t3).__name__)
    check("resolve: transport name stays the mode (no API surface change)",
          _n == "self_hosted" and _n2 == "self_hosted" and _n3 == "self_hosted")
finally:
    settings.TELEPHONY_MODE = _old_mode
_db.close()

# ── Part B: live server (fresh DB, real routes, no real provider) ────────
print("── Twilio transport: live-server checks ──")
_srv_db = os.path.join(_tmp, "live.db")
_port = 18211
_base = f"http://127.0.0.1:{_port}"
_env = dict(os.environ)
_env.update({
    "DATABASE_URL": f"sqlite:///{_srv_db}",
    "PUBLIC_BASE_URL": _base,
    "WEBHOOK_SECRET": "twilio-smoke-secret",
    "VAPI_API_KEY": "", "PLIVO_AUTH_ID": "", "PLIVO_AUTH_TOKEN": "",
    "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "SARVAM_API_KEY": "",
    "TELEPHONY_MODE": "stub",
    "SECRET_KEY": "twilio-smoke-secret",
})
_log = open(os.path.join(_tmp, "live.log"), "wb")
_proc = subprocess.Popen(
    [APP_PY, "-m", "uvicorn", "app.main:app", "--port", str(_port)],
    cwd=BACKEND, env=_env, stdout=_log, stderr=_log)
try:
    for _ in range(80):
        try:
            with urllib.request.urlopen(_base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    else:
        raise RuntimeError("live app did not start")

    def _db_rows(sql, args=()):
        con = sqlite3.connect(_srv_db, timeout=5)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def _db_exec(sql, args=()):
        con = sqlite3.connect(_srv_db, timeout=5)
        try:
            con.execute(sql, args)
            con.commit()
        finally:
            con.close()

    _tenants = _db_rows("SELECT tenant_id FROM tenants LIMIT 1")
    check("live: seeded tenant exists", bool(_tenants), str(_tenants))
    _tid = _tenants[0][0]

    def _http(method, path, body=None, headers=None, form=None):
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            req = urllib.request.Request(_base + path, method=method, data=data)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(_base + path, method=method, data=data)
            if body is not None:
                req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    # Seed session rows directly (provider-facing routes need no auth).
    _cols = ("session_id, tenant_id, attempt_id, provider_mode, state, to_number, from_number, "
             "channel, test_lead, provider_call_id, recording_url, recording_s3_key, recording_s3_bucket, "
             "end_reason, transcript, stt_status, stt_language, stt_detail, llm_status, llm_detail, "
             "tts_status, tts_detail")
    _vals = ("ses_tw1", _tid, "att_tw1", "self_hosted", "QUEUED", "+15550003333", "+15550001111",
             "", "{}", "", "", "", "", "", "[]", "", "", "", "", "", "", "")
    _db_exec(f"INSERT INTO call_sessions ({_cols}) VALUES ({','.join('?' * len(_vals))})", _vals)
    _vals = ("ses_tw2", _tid, "att_tw2", "self_hosted", "QUEUED", "+15550003334", "+15550001111",
             "", "{}", "", "", "", "", "", "[]", "", "", "", "", "", "", "")
    _db_exec(f"INSERT INTO call_sessions ({_cols}) VALUES ({','.join('?' * len(_vals))})", _vals)
    _vals = ("ses_tw3", _tid, "att_tw3", "self_hosted", "QUEUED", "+15550003335", "+15550001111",
             "", "{}", "", "", "", "", "", "[]", "", "", "", "", "", "", "")
    _db_exec(f"INSERT INTO call_sessions ({_cols}) VALUES ({','.join('?' * len(_vals))})", _vals)
    _now = datetime.utcnow().isoformat()
    _db_exec("INSERT INTO provider_settings (setting_id, tenant_id, provider, enabled, fields, last_test_status, last_test_message, last_test_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
             ("pset_tw_sig", _tid, "twilio", 1, json.dumps({"account_sid": "ACsig", "auth_token": "tok-sig"}), "", "", None, _now, _now))

    _st, _xml = _http("GET", "/api/telephony/twilio/answer/ses_tw1")
    _root = ET.fromstring(_xml.decode()) if _st == 200 else None
    _sel = _root.find("Connect/Stream") if _root is not None else None
    check("live: answer route returns TwiML with the live stream URL",
          _st == 200 and _sel is not None
          and _sel.get("url") == f"ws://127.0.0.1:{_port}/api/telephony/twilio/stream/ses_tw1",
          f"status={_st}")
    _st404, _ = _http("GET", "/api/telephony/twilio/answer/ses_missing")
    check("live: answer route 404s for an unknown session", _st404 == 404)

    # WebSocket: connected/start/media/stop -> finalize (Twilio-labelled).
    from websockets.sync.client import connect as _ws_connect
    with _ws_connect(f"ws://127.0.0.1:{_port}/api/telephony/twilio/stream/ses_tw1") as ws:
        ws.send(json.dumps({"event": "connected", "streamSid": "MZlive"}))
        ws.send(json.dumps({"event": "start", "streamSid": "MZlive",
                            "start": {"streamSid": "MZlive", "callSid": "CAlive",
                                      "mediaFormat": {"encoding": "audio/x-mulaw",
                                                      "sampleRate": 8000}}}))
        ws.send(json.dumps({"event": "media", "streamSid": "MZlive",
                            "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}}))
        ws.send(json.dumps({"event": "mark", "streamSid": "MZlive",
                            "mark": {"name": "m1"}}))
        ws.send(json.dumps({"event": "stop", "streamSid": "MZlive"}))
    _res = None
    for _ in range(20):
        _rows = _db_rows("SELECT cs.state, cr.summary, cr.disposition FROM call_sessions cs "
                         "LEFT JOIN call_results cr ON cr.session_id = cs.session_id "
                         "WHERE cs.session_id=?", ("ses_tw1",))
        if _rows and _rows[0][1] is not None:
            _res = _rows[0]
            break
        time.sleep(0.25)
    check("live: Twilio stream close finalizes exactly once (Twilio-labelled spike shape)",
          _res is not None and _res[0] == "COMPLETED"
          and "Twilio call connected" in (_res[1] or "")
          and _res[2] == "", str(_res))

    # Status callbacks: finalize + idempotency + signature gate.
    _st, _ = _http("POST", "/api/telephony/twilio/status/ses_tw2",
                   form={"CallStatus": "no-answer", "CallSid": "CAtest2", "CallDuration": "0"})
    _rows = _db_rows("SELECT state, end_reason FROM call_sessions WHERE session_id=?",
                     ("ses_tw2",))
    check("live: no-answer status callback finalizes NO_ANSWER",
          _st == 200 and _rows and _rows[0] == ("NO_ANSWER", "no_answer"), str(_rows))
    _http("POST", "/api/telephony/twilio/status/ses_tw2",
          form={"CallStatus": "completed", "CallSid": "CAtest2", "CallDuration": "31"})
    _rows2 = _db_rows("SELECT state FROM call_sessions WHERE session_id=?",
                      ("ses_tw2",))
    check("live: second terminal callback cannot rewrite the stored result",
          _rows2 and _rows2[0][0] == "NO_ANSWER", str(_rows2))
    _sig_form = {"CallStatus": "completed", "CallSid": "CAtest3", "CallDuration": "5"}
    _sig_url = f"{_base}/api/telephony/twilio/status/ses_tw3"
    _sig_base = _sig_url + "".join(f"{k}{_sig_form[k]}" for k in sorted(_sig_form))
    _sig_ok = base64.b64encode(hmac.new(b"tok-sig", _sig_base.encode(),
                                        hashlib.sha1).digest()).decode()
    _st, _ = _http("POST", "/api/telephony/twilio/status/ses_tw3", form=_sig_form,
                   headers={"X-Twilio-Signature": _sig_ok})
    check("live: correct Twilio signature accepted", _st == 200, f"status={_st}")
    _st, _ = _http("POST", "/api/telephony/twilio/status/ses_tw3", form=_sig_form,
                   headers={"X-Twilio-Signature": "bogus"})
    check("live: bad Twilio signature rejected (403)", _st == 403, f"status={_st}")
    _st, _ = _http("POST", "/api/telephony/twilio/stream-status/ses_tw1", form={})
    check("live: stream-status callback acknowledged", _st == 200, f"status={_st}")
finally:
    _proc.terminate()
    try:
        _proc.wait(timeout=10)
    except Exception:
        _proc.kill()

print(f"\nTwilio smoke: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
