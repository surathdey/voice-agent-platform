#!/usr/bin/env python3
"""C1 isolation tests: two Secure Boxes must not see each other.

Boots TWO backend processes (uvicorn) on two fresh SQLite databases
with different BOX_TENANT_SLUG values — the local equivalent of two
boxes on one host, minus Docker (this dev VM has none; the Docker
form is proven on the AWS test box). Then proves, over HTTP:

1. Health reports each box's own slug and a different database
   fingerprint (box -> database mapping is observable).
2. A campaign + lead created in box A is invisible in box B:
   B's campaign list has no A campaign, A's campaign/leads/agent
   URLs 404 in B, and A's tenant record 404s in B.
3. Statically, exactly one SQLAlchemy engine construction site
   exists in backend/app — no code path can open a second tenant's
   database inside one request (design §13.1).
4. The generated reverse-proxy config routes each subdomain to
   exactly its own box's port and drops unknown hosts (444).
5. Restarting box A (same database file) keeps A's data and does
   not touch box B's state — the restart/rebuild isolation rule.

Processes are killed by exact PID in a finally block; temp databases
are removed. Exit 0 only if every check passes.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
PYTHON = os.path.join(BACKEND, ".venv", "bin", "python")
TMP = "/tmp/c1-isolation"

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))


def http(base, method, path, token=None, body=None, expect=200):
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def http_multipart(base, path, token, fields, filename, filebytes):
    boundary = "----c1test" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="file"; filename="{filename}"\r\n'
                 "Content-Type: text/csv\r\n\r\n".encode() + filebytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(base + path, method="POST", data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def boot(slug, port, dbfile, tenant_name, email, password):
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{dbfile}",
        "SECRET_KEY": "c1-isolation-test",
        "TELEPHONY_MODE": "stub",
        "BOX_TENANT_SLUG": slug,
        "SEED_TENANT_NAME": tenant_name,
        "SEED_USER_EMAIL": email,
        "SEED_USER_PASSWORD": password,
    })
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            s, h = http(base, "GET", "/api/health")
            if s == 200 and h.get("status") == "ok":
                return proc, base
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"box {slug} did not come up on :{port}")


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main():
    os.makedirs(TMP, exist_ok=True)
    db_a = os.path.join(TMP, "tenant-a.db")
    db_b = os.path.join(TMP, "tenant-b.db")
    for f in (db_a, db_b):
        if os.path.exists(f):
            os.remove(f)
    proc_a = proc_b = None
    try:
        print("── boot two boxes ─────────────────────────────")
        proc_a, base_a = boot("tenant-a", 8111, db_a, "Tenant A",
                              "admin@tenant-a.example", "pw-a-123")
        proc_b, base_b = boot("tenant-b", 8112, db_b, "Tenant B",
                              "admin@tenant-b.example", "pw-b-123")

        print("── 1. health reports own box ──────────────────")
        _, ha = http(base_a, "GET", "/api/health")
        _, hb = http(base_b, "GET", "/api/health")
        check("box A health slug is tenant-a",
              ha.get("box", {}).get("tenant_slug") == "tenant-a", str(ha.get("box")))
        check("box B health slug is tenant-b",
              hb.get("box", {}).get("tenant_slug") == "tenant-b", str(hb.get("box")))
        check("database fingerprints differ",
              ha.get("box", {}).get("database") != hb.get("box", {}).get("database"),
              f"{ha.get('box')} vs {hb.get('box')}")
        check("health leaks no secret value",
              "pw-a-123" not in json.dumps(ha) and "pw-b-123" not in json.dumps(hb))

        print("── 2. cross-box invisibility ─────────────────")
        _, la = http(base_a, "POST", "/api/auth/login",
                     body={"email": "admin@tenant-a.example", "password": "pw-a-123"})
        _, lb = http(base_b, "POST", "/api/auth/login",
                     body={"email": "admin@tenant-b.example", "password": "pw-b-123"})
        tok_a, tok_b = la["access_token"], lb["access_token"]
        _, me_a = http(base_a, "GET", "/api/tenants/me", token=tok_a)
        _, me_b = http(base_b, "GET", "/api/tenants/me", token=tok_b)
        check("tenant ids differ", me_a["tenant_id"] != me_b["tenant_id"])

        _, agent = http(base_a, "POST", "/api/agents", token=tok_a,
                        body={"name": "A-only agent", "prompt": "ROLE\nTest."})
        agent_id = agent["agent_id"]
        _, camp = http(base_a, "POST", "/api/campaigns", token=tok_a,
                       body={"agent_id": agent_id, "name": "A-only campaign",
                             "product": "vehicle_loan", "provider_mode": "managed"})
        camp_id = camp["campaign_id"]
        csv = b"name,phone_number,bank_name,state\nAsha,9876543210,Test Bank,West Bengal\n"
        s, up = http_multipart(base_a, f"/api/campaigns/{camp_id}/leads/upload",
                               tok_a,
                               {"mapping": json.dumps({"name": "name", "phone_number": "phone_number", "bank_name": "bank_name", "state": "state"})},
                               "leads.csv", csv)
        check("lead upload in A succeeds", s == 200, str(up))
        _, leads_a = http(base_a, "GET", f"/api/campaigns/{camp_id}/leads", token=tok_a)
        n_leads = len(leads_a.get("leads", []))
        check("A sees its own lead", n_leads == 1, str(leads_a))

        _, camps_b = http(base_b, "GET", "/api/campaigns", token=tok_b)
        check("B campaign list has no A campaign",
              all(c["campaign_id"] != camp_id for c in camps_b))
        s, _ = http(base_b, "GET", f"/api/campaigns/{camp_id}", token=tok_b)
        check("B GET A campaign -> 404", s == 404, f"got {s}")
        s, _ = http(base_b, "GET", f"/api/campaigns/{camp_id}/leads", token=tok_b)
        check("B GET A leads -> 404", s == 404, f"got {s}")
        _, agents_b = http(base_b, "GET", "/api/agents", token=tok_b)
        check("B agent list has no A agent",
              all(a["agent_id"] != agent_id for a in agents_b))
        s, _ = http(base_b, "GET", f"/api/tenants/{me_a['tenant_id']}", token=tok_b)
        check("B GET A tenant -> 404", s == 404, f"got {s}")

        print("── 3. one engine per process ──────────────────")
        hits = []
        for dirpath, _, files in os.walk(os.path.join(ROOT, "backend", "app")):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    with open(p) as f:
                        if "create_engine(" in f.read():
                            hits.append(os.path.relpath(p, ROOT))
        check("exactly one create_engine site (app.db)",
              hits == ["backend/app/db.py"], str(hits))

        print("── 4. proxy config routes by subdomain ───────")
        map_path = os.path.join(TMP, "tenants.json")
        with open(map_path, "w") as f:
            json.dump({"a.example.com": 5174, "b.example.com": 5175}, f)
        out_path = os.path.join(TMP, "proxy.conf")
        r = subprocess.run([sys.executable,
                            os.path.join(ROOT, "scripts", "generate_proxy_config.py"),
                            map_path, "--out", out_path],
                           capture_output=True, text=True)
        conf = open(out_path).read() if r.returncode == 0 else ""
        block_a = re.search(r"server_name a\.example\.com;.*?proxy_pass http://([^;]+);",
                            conf, re.S)
        block_b = re.search(r"server_name b\.example\.com;.*?proxy_pass http://([^;]+);",
                            conf, re.S)
        check("proxy generator runs", r.returncode == 0, r.stderr[:200])
        check("a.example.com routes to 5174 only",
              bool(block_a) and block_a.group(1) == "127.0.0.1:5174")
        check("b.example.com routes to 5175 only",
              bool(block_b) and block_b.group(1) == "127.0.0.1:5175")
        check("unknown hosts dropped (444 default server)",
              "default_server" in conf and "return 444;" in conf)
        r2 = subprocess.run([sys.executable,
                             os.path.join(ROOT, "scripts", "generate_proxy_config.py"),
                             map_path, "--tls", "--out", os.path.join(TMP, "proxy-tls.conf")],
                            capture_output=True, text=True)
        tls_conf = open(os.path.join(TMP, "proxy-tls.conf")).read() if r2.returncode == 0 else ""
        check("TLS mode emits cert paths + HTTP redirect",
              "/etc/letsencrypt/live/a.example.com/fullchain.pem" in tls_conf
              and "return 301 https://$host$request_uri;" in tls_conf)

        print("── 5. restart A, B untouched ──────────────────")
        stop(proc_a)
        proc_a, base_a = boot("tenant-a", 8111, db_a, "Tenant A",
                              "admin@tenant-a.example", "pw-a-123")
        _, la2 = http(base_a, "POST", "/api/auth/login",
                      body={"email": "admin@tenant-a.example", "password": "pw-a-123"})
        _, leads_a2 = http(base_a, "GET", f"/api/campaigns/{camp_id}/leads",
                           token=la2["access_token"])
        check("A data survives its own restart",
              len(leads_a2.get("leads", [])) == 1, str(leads_a2))
        _, camps_b2 = http(base_b, "GET", "/api/campaigns", token=tok_b)
        check("B still has no A campaign after A restart",
              all(c["campaign_id"] != camp_id for c in camps_b2))
    finally:
        if proc_a:
            stop(proc_a)
        if proc_b:
            stop(proc_b)
        for f in (db_a, db_b):
            if os.path.exists(f):
                os.remove(f)
    print(f"\nC1 isolation: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED:", FAILED)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
