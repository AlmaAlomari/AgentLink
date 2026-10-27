import os, json, time, sqlite3, uuid, math, hashlib, hmac, secrets, contextvars, threading, re
import httpx
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "database", "agentlink.db"); os.makedirs(os.path.dirname(DB), exist_ok=True)
def q(sql, a=()):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(x) for x in c.execute(sql, a).fetchall()]
q("create table if not exists logs(id integer primary key autoincrement, ts text, src text, dst text, event text, detail text)")
q("create table if not exists msgs(id text primary key, ok int, a_ms real, b_ms real, enc_ms real, dec_ms real, ratio real, acc real)")
q("create table if not exists agents(id integer primary key autoincrement, name text, company text, description text, capabilities text, languages text, status text, protocol text, security text)")
if not q("select 1 from agents"):
    for a in [("Customer Support Agent","DemoCorp","Handles tickets and complaints","Customer Support, Complaint Analysis, Sentiment Analysis","English, Arabic","Online","AgentLink","Verified"),
              ("Data Analyst Agent","Insight Labs","Analyses datasets and reports","Data Analysis, Forecasting","English","Online","AgentLink","Verified"),
              ("Legacy Billing Agent","FinCo","Invoice processing","Billing, Reconciliation","English","Offline","Custom REST","Unverified")]:
        q("insert into agents(name,company,description,capabilities,languages,status,protocol,security) values(?,?,?,?,?,?,?,?)", a)
for t in ("logs", "msgs", "agents"):
    try: q(f"alter table {t} add column owner text")
    except Exception: pass
q("create table if not exists users(email text primary key, salt text, pw text, role text, created text)")
q("create table if not exists sessions(token text primary key, email text, role text)")
q("create table if not exists live_use(email text, day text, n int, primary key(email, day))")
q("create table if not exists docs(id integer primary key autoincrement, owner text, name text, content text, created text)")
q("create table if not exists tasks(id integer primary key autoincrement, owner text, title text, prompt text, every_min int, next_run real, last_run text, last_result text, enabled int)")
q("create table if not exists tg_links(email text primary key, chat_id text)")
q("create table if not exists tg_codes(code text primary key, email text, exp real)")
CUR = contextvars.ContextVar("cur", default="")
def log(src, dst, ev, detail=""): q("insert into logs(ts,src,dst,event,detail,owner) values(?,?,?,?,?,?)", (time.strftime("%H:%M:%S"), src, dst, ev, detail, CUR.get()))

PERMS = {"AGENT_A": {"SEND_REQUEST", "REQUEST_ANALYSIS"}, "AGENT_B": {"RECEIVE_REQUEST", "SEND_RESPONSE"}}
INTENTS = {"ANALYZE", "SUMMARIZE", "CLASSIFY", "FORECAST", "DEBUG", "RETRIEVE"}
def perm(agent, action):
    if action not in PERMS.get(agent, ()):
        log(agent, "AGENTLINK", "ACCESS DENIED", action)
        raise HTTPException(403, f"ACCESS DENIED: Agent does not have permission for this operation ({agent}:{action}).")

def hpw(pw, salt): return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120000).hex()
async def me(authorization: str = Header("")):
    s = q("select email, role from sessions where token=?", (authorization.replace("Bearer ", ""),))
    if not s: raise HTTPException(401, "Please log in")
    CUR.set(s[0]["email"]); return s[0]
def adm(u): return u["role"] == "admin"
def live_ok(u):
    if adm(u): return
    d, lim = time.strftime("%Y-%m-%d"), int(os.getenv("LIVE_LIMIT", "20"))
    n = (q("select n from live_use where email=? and day=?", (u["email"], d)) or [{"n": 0}])[0]["n"]
    if n >= lim: raise HTTPException(429, f"Daily Live AI limit reached ({lim} calls). Use Demo Mode or try tomorrow.")
    q("insert or replace into live_use values(?,?,?)", (u["email"], d, n + 1))

def llm(system, user, js=False):
    key, prov = os.getenv("LLM_API_KEY"), os.getenv("LLM_PROVIDER", "openai").lower()
    if not key or key == "YOUR_API_KEY": raise RuntimeError("LLM_API_KEY is not set (use Demo Mode or edit .env)")
    if prov == "anthropic":
        r = httpx.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=45,
                       json={"model": os.getenv("LLM_MODEL") or "claude-sonnet-5-5", "max_tokens": 700, "system": system, "messages": [{"role": "user", "content": user}]})
        r.raise_for_status(); return r.json()["content"][0]["text"]
    body = {"model": os.getenv("LLM_MODEL") or "gpt-4o-mini", "max_tokens": 2000, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if js: body["response_format"] = {"type": "json_object"}
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if prov == "groq":
       base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    r = httpx.post(base_url + "/chat/completions", headers={"Authorization": "Bearer " + key}, json=body, timeout=45)
    r.raise_for_status(); return r.json()["choices"][0]["message"]["content"]
def jl(t): return json.loads(t.strip().strip("`").removeprefix("json").strip())
def frame(**kw): return "AL/1|" + "|".join(f"{k}:{str(v).replace('|', '/').replace(chr(10), ' ')}" for k, v in kw.items())

SYS_A = ('You are Agent A, a request planner. Return ONLY JSON: {"intent": ANALYZE|SUMMARIZE|CLASSIFY|FORECAST|DEBUG|RETRIEVE, '
         '"priority": LOW|MEDIUM|HIGH, "task": one clear sentence in the same language as the request, "target": UPPER_SNAKE noun, "output": UPPER_SNAKE expected output}')
def demo_a(m):
    t = m.lower(); i = next((n for n, k in [("SUMMARIZE", "summar"), ("CLASSIFY", "classif"), ("FORECAST", "forecast"), ("DEBUG", "debug")] if k in t), "ANALYZE")
    return {"intent": i, "priority": "HIGH" if any(w in t for w in ("urgent", "asap", "critical", "most", "top")) else "MEDIUM", "task": m,
            "target": "CUSTOMER_COMPLAINTS" if "complaint" in t else "DATA", "output": "TOP_3_ISSUES" if ("three" in t or "3" in t) else "SUMMARY"}
DEMO_B = {"en": "Demo answer — the three most common issues: 1) Delivery delays 2) Product quality 3) Slow support response.",
          "ar": "إجابة تجريبية — أكثر ثلاث مشاكل شيوعاً: ١) تأخر التسليم ٢) جودة المنتج ٣) بطء استجابة الدعم."}

# ---------- data context (simple keyword retrieval over the owner's own documents) ----------
STOP = set("the and for are with what which who how tell about all most common top three analyze analyse summarize list find give show this that from your my our please".split())
def words(t): return {w for w in re.findall(r"\w+", t.lower()) if len(w) > 2 and w not in STOP}
def mask(t):
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[EMAIL]", t)
    return re.sub(r"\+?\d[\d\s\-]{7,}\d", "[NUMBER]", t)
def context(owner, query, limit=6000):
    qw, scored = words(query), []
    for d in q("select name, content from docs where owner=?", (owner,)):
        for p in re.split(r"\n+", d["content"]):
            p = p.strip()
            if p: scored.append((len(qw & words(p)), d["name"], p[:600]))
    scored.sort(key=lambda x: -x[0]); out, used, src = [], 0, []
    for sc, n, p in scored:
        if used + len(p) > limit: break
        out.append(f"[{n}] {p}"); used += len(p)
        if n not in src: src.append(n)
    return "\n".join(out), src, (scored[0][0] if scored else 0)
def llm_ready(): k = os.getenv("LLM_API_KEY"); return bool(k) and k != "YOUR_API_KEY"
def answer_b(owner, task, intent, pr, lang, live, mask_data):
    ctx, srcs, best = context(owner, task)
    L = "Arabic" if lang == "ar" else "English"
    if not live:
        if ctx and best: return "Demo mode — best matches from your data: " + " | ".join(l.split("] ", 1)[-1][:110] for l in ctx.split("\n")[:3]), srcs
        if ctx: return "Demo mode — nothing in your data matches this request.", srcs
        return DEMO_B.get(lang, DEMO_B["en"]), []
    if ctx:
        return llm(f"You are Agent B, a task analyst. Answer ONLY from the DATA provided. If the data does not contain the answer, say so. Mention document names in brackets. Max 120 words. Plain text only, no Markdown. Reply in {L}.",
                   f"Task: {task}\n\nDATA:\n{mask(ctx) if mask_data else ctx}"), srcs
    return llm(f"You are Agent B, a task analyst. Perform the task. No real dataset is attached, so give plausible, clearly illustrative findings. Max 90 words. Plain text only, no Markdown or asterisks. Reply in {L}.",
               f"Intent={intent}; priority={pr}; task={task}"), []

# ---------- server-side agent run (used by scheduled tasks and Telegram) ----------
def run_agent(owner, prompt, mask_data=True):
    CUR.set(owner); u = (q("select email, role from users where email=?", (owner,)) or [{"email": owner, "role": "user"}])[0]
    live, mid = llm_ready(), uuid.uuid4().hex[:4].upper(); lang = "ar" if re.search(r"[\u0600-\u06FF]", prompt) else "en"
    if live:
        try: live_ok(u)
        except HTTPException: live = False
    log("USER", "AGENT_A", "Request received", prompt[:70] + " (background)")
    try: d = jl(llm(SYS_A, prompt, True)) if live else demo_a(prompt)
    except Exception: d, live = demo_a(prompt), False
    ri, rp = str(d.get("intent", "")).upper(), str(d.get("priority", "")).upper()
    intent, pr = (ri if ri in INTENTS else "ANALYZE"), (rp if rp in ("LOW", "MEDIUM", "HIGH") else "MEDIUM")
    task = str(d.get("task", prompt)).replace("|", "/")
    perm("AGENT_A", "SEND_REQUEST"); perm("AGENT_A", "REQUEST_ANALYSIS")
    log("AGENT_A", "AGENTLINK", "REQUEST_ANALYSIS", frame(MSG=mid, INTENT=intent, PRIORITY=pr, TASK=task[:60])[:150])
    perm("AGENT_B", "RECEIVE_REQUEST")
    try: ans, srcs = answer_b(owner, task + " " + prompt, intent, pr, lang, live, mask_data)
    except Exception as e: ans, srcs, live = f"Could not complete the task: {e}", [], False
    perm("AGENT_B", "SEND_RESPONSE"); log("AGENT_B", "AGENTLINK", "Response generated", f"MSG:{mid}" + (f" · data: {', '.join(srcs)}" if srcs else ""))
    return {"answer": ans, "sources": srcs, "mode": "live" if live else "demo", "id": mid}

# ---------- scheduled tasks ----------
def run_task(t):
    try: res = run_agent(t["owner"], t["prompt"])
    except Exception as e: res = {"answer": f"Error: {e}", "sources": [], "mode": "error"}
    nxt = time.time() + t["every_min"] * 60 if t["every_min"] else 0
    q("update tasks set last_run=?, last_result=?, next_run=?, enabled=? where id=?", (time.strftime("%Y-%m-%d %H:%M"), res["answer"][:2000], nxt, 1 if nxt else 0, t["id"]))
    CUR.set(t["owner"]); log("TASK", "AGENTLINK", "Task finished", t["title"][:60])
    tg_notify(t["owner"], f"Task: {t['title']} ({res['mode']})\n{res['answer']}")
def scheduler():
    while True:
        try:
            for t in q("select * from tasks where enabled=1 and next_run<=?", (time.time(),)): run_task(t)
        except Exception: pass
        time.sleep(20)

# ---------- Telegram (long polling: works from your own computer, no public URL needed) ----------
TG = os.getenv("TELEGRAM_BOT_TOKEN", ""); TG_USER = {"name": ""}
def tg_api(method, **p): return httpx.post(f"https://api.telegram.org/bot{TG}/{method}", json=p, timeout=40).json()
def tg_send(chat_id, text):
    try: tg_api("sendMessage", chat_id=chat_id, text=text[:3900])
    except Exception: pass
def tg_notify(email, text):
    r = q("select chat_id from tg_links where email=?", (email,))
    if r and TG: tg_send(r[0]["chat_id"], text)
def tg_handle(chat_id, text):
    text = (text or "").strip(); cid = str(chat_id)
    if text.startswith("/start") or text.startswith("/help"):
        return "AgentLink bot.\n1) In the web app open Tasks & Telegram, press Create link code, then send me: /link CODE\n2) Then send any request and I will answer from your data.\nCommands: /tasks, /run ID, /unlink"
    if text.startswith("/link"):
        code = text[5:].strip().upper(); c = q("select * from tg_codes where code=? and exp>?", (code, time.time()))
        if not c: return "Invalid or expired code. Create a new one in the app."
        q("insert or replace into tg_links values(?,?)", (c[0]["email"], cid)); q("delete from tg_codes where code=?", (code,))
        CUR.set(c[0]["email"]); log("TELEGRAM", "AGENTLINK", "Chat linked", c[0]["email"]); return f"Linked to {c[0]['email']}. Send me a request."
    lk = q("select email from tg_links where chat_id=?", (cid,))
    if not lk: return "This chat is not linked. Open Tasks & Telegram in the app, press Create link code, and send /link CODE here."
    email = lk[0]["email"]; CUR.set(email)
    if text == "/unlink": q("delete from tg_links where email=?", (email,)); return "Unlinked."
    if text == "/tasks": return "\n".join(f"{t['id']}: {t['title']}" for t in q("select id,title from tasks where owner=?", (email,))) or "No tasks yet."
    if text.startswith("/run"):
        t = q("select * from tasks where id=? and owner=?", (text[4:].strip() or 0, email))
        if not t: return "Task not found. Use /tasks."
        text = t[0]["prompt"]
    log("TELEGRAM", "AGENT_A", "Message received", text[:60]); r = run_agent(email, text)
    return r["answer"] + (f"\n\nSources: {', '.join(r['sources'])}" if r["sources"] else "") + ("" if r["mode"] == "live" else "\n(demo mode)")
def tg_poll():
    off = 0
    while True:
        try:
            for up in tg_api("getUpdates", offset=off, timeout=25).get("result", []):
                off = up["update_id"] + 1; m = up.get("message") or {}
                if m.get("text"): tg_send(m["chat"]["id"], tg_handle(m["chat"]["id"], m["text"]))
        except Exception: time.sleep(5)

app = FastAPI(title="AgentLink")
class A(BaseModel): message: str; lang: str = "en"; demo: bool = False
class B(BaseModel): id: str; intent: str; action: str; priority: str; task: str; lang: str = "en"; demo: bool = False; mask: bool = True
class T(BaseModel): id: str; ok: bool; a_ms: float = 0; b_ms: float = 0; enc_ms: float = 0; dec_ms: float = 0; ratio: float = 0; acc: float = 0
class P(BaseModel): agent: str; action: str
class Ag(BaseModel): name: str; company: str = ""; description: str = ""; capabilities: str = ""; languages: str = "English"; status: str = "Online"; protocol: str = "AgentLink"; security: str = "Verified"

@app.get("/api/health")
def health(): return {"llm_configured": bool(os.getenv("LLM_API_KEY")) and os.getenv("LLM_API_KEY") != "YOUR_API_KEY", "provider": os.getenv("LLM_PROVIDER", "openai")}

@app.post("/api/agent-a")
def agent_a(r: A, u: dict = Depends(me)):
    t0 = time.perf_counter(); mid = uuid.uuid4().hex[:4].upper()
    if not r.demo: live_ok(u)
    log("USER", "AGENT_A", "Request received", r.message[:80])
    try: d = demo_a(r.message) if r.demo else jl(llm(SYS_A, r.message, True))
    except Exception as e:
        log("AGENT_A", "SYSTEM", "LLM error", str(e)[:120]); raise HTTPException(502, f"LLM call failed: {e}")
    ri, rp = str(d.get("intent", "")).upper(), str(d.get("priority", "")).upper()
    intent, pr = (ri if ri in INTENTS else "ANALYZE"), (rp if rp in ("LOW", "MEDIUM", "HIGH") else "MEDIUM")
    s = {"intent": intent, "priority": pr, "action": "REQUEST_ANALYSIS", "task": str(d.get("task", r.message)).replace("|", "/"), "language": "Arabic" if r.lang == "ar" else "English"}
    perm("AGENT_A", "SEND_REQUEST"); perm("AGENT_A", s["action"])
    checks = [["Schema valid", True], ["Intent known", ri in INTENTS], ["Priority valid", rp in ("LOW", "MEDIUM", "HIGH")], ["Permission check", True]]
    compact = f"INTENT={intent}|TARGET={d.get('target', 'DATA')}|OUTPUT={d.get('output', 'SUMMARY')}"
    o, c = len(r.message), len(compact)
    comp = {"orig_chars": o, "struct_chars": c, "ratio": round(c / max(o, 1), 2), "tok_orig": math.ceil(o / 4), "tok_struct": math.ceil(c / 4), "compact": compact}
    fr = frame(MSG=mid, INTENT=intent, ACTION=s["action"], PRIORITY=pr, LANG=r.lang, TASK=s["task"][:100])
    log("AGENT_A", "AGENTLINK", "REQUEST_ANALYSIS", f"MSG:{mid} {intent}/{pr}")
    return {"id": mid, "structured": s, "frame": fr, "compression": comp, "checks": checks, "ms": round((time.perf_counter() - t0) * 1000, 1), "mode": "demo" if r.demo else "live"}

@app.post("/api/agent-b")
def agent_b(r: B, u: dict = Depends(me)):
    if not r.demo: live_ok(u)
    t0 = time.perf_counter(); perm("AGENT_B", "RECEIVE_REQUEST")
    log("AGENTLINK", "AGENT_B", "Request delivered", f"MSG:{r.id} decoded from audio")
    try: ans, srcs = answer_b(u["email"], r.task, r.intent, r.priority, r.lang, not r.demo, r.mask)
    except Exception as e:
        log("AGENT_B", "SYSTEM", "LLM error", str(e)[:120]); raise HTTPException(502, f"LLM call failed: {e}")
    perm("AGENT_B", "SEND_RESPONSE")
    log("AGENT_B", "AGENTLINK", "Response generated", f"MSG:{r.id}" + (f" · data: {', '.join(srcs)}" if srcs else ""))
    return {"answer": ans, "sources": srcs, "frame": frame(MSG=r.id, STATUS="OK", LANG=r.lang, RESULT=ans[:90]), "ms": round((time.perf_counter() - t0) * 1000, 1)}

@app.post("/api/telemetry")
def telemetry(t: T, u: dict = Depends(me)):
    q("insert or replace into msgs(id,ok,a_ms,b_ms,enc_ms,dec_ms,ratio,acc,owner) values(?,?,?,?,?,?,?,?,?)", (t.id, int(t.ok), t.a_ms, t.b_ms, t.enc_ms, t.dec_ms, t.ratio, t.acc, u["email"]))
    log("AGENTLINK", "USER", "Final answer delivered" if t.ok else "Pipeline FAILED", f"MSG:{t.id}"); return {"ok": True}

class Cred(BaseModel): email: str; password: str
@app.post("/api/register")
def register(c: Cred):
    e = c.email.strip().lower()
    if "@" not in e or len(c.password) < 6: raise HTTPException(422, "Enter a valid email and a password of 6+ characters")
    if q("select 1 from users where email=?", (e,)): raise HTTPException(409, "Email already registered")
    salt = secrets.token_hex(8); role = "user" if q("select 1 from users") else "admin"
    q("insert into users values(?,?,?,?,?)", (e, salt, hpw(c.password, salt), role, time.strftime("%Y-%m-%d")))
    return login(c)
@app.post("/api/login")
def login(c: Cred):
    e = c.email.strip().lower(); r = q("select * from users where email=?", (e,))
    if not r or not hmac.compare_digest(r[0]["pw"], hpw(c.password, r[0]["salt"])): raise HTTPException(401, "Wrong email or password")
    t = secrets.token_hex(24); q("insert into sessions values(?,?,?)", (t, e, r[0]["role"]))
    return {"token": t, "email": e, "role": r[0]["role"]}
def own(u): return "*" if adm(u) else u["email"]
@app.get("/api/me")
def whoami(u: dict = Depends(me)): return u
@app.get("/api/metrics")
def metrics(u: dict = Depends(me)):
    o = own(u)
    r = q("select count(*) n, sum(ok) s, avg(a_ms) a, avg(b_ms) b, avg(enc_ms) e, avg(dec_ms) d, avg(ratio) cr, avg(acc) ac, avg(a_ms+b_ms+enc_ms+dec_ms) tot from msgs where (?='*' or owner=?)", (o, o))[0]
    rd = lambda x: round(x or 0, 2); s = int(r["s"] or 0)
    return {"messages": r["n"], "ok": s, "failed": r["n"] - s, "avg_total_ms": rd(r["tot"]), "avg_agent_a_ms": rd(r["a"]), "avg_agent_b_ms": rd(r["b"]), "avg_encode_ms": rd(r["e"]),
            "avg_decode_ms": rd(r["d"]), "decode_accuracy_pct": rd((r["ac"] or 0) * 100), "avg_compression_ratio": rd(r["cr"]),
            "recent": q("select id, a_ms+b_ms+enc_ms+dec_ms t from msgs where (?='*' or owner=?) order by rowid desc limit 10", (o, o))}
@app.get("/api/logs")
def logs(limit: int = 40, u: dict = Depends(me)):
    o = own(u); return q("select ts,src,dst,event,detail from logs where (?='*' or owner=?) order by id desc limit ?", (o, o, limit))
@app.post("/api/permission")
def permission(p: P, u: dict = Depends(me)): perm(p.agent, p.action); log(p.agent, "AGENTLINK", "Permission granted", p.action); return {"allowed": True}
@app.get("/api/registry")
def registry(q_: str = "", status: str = "", u: dict = Depends(me)):
    k = f"%{q_}%"
    return q("select * from agents where (name like ? or company like ? or capabilities like ? or description like ?) and (?='' or status=?) and (owner is null or owner=? or ?='*') order by id desc", (k, k, k, k, status, status, u["email"], own(u)))
@app.post("/api/registry")
def reg_add(a: Ag, u: dict = Depends(me)):
    q("insert into agents(name,company,description,capabilities,languages,status,protocol,security,owner) values(?,?,?,?,?,?,?,?,?)", tuple(a.model_dump().values()) + (u["email"],)); log("REGISTRY", "AGENTLINK", "Agent registered", a.name); return {"ok": True}
@app.delete("/api/registry/{i}")
def reg_del(i: int, u: dict = Depends(me)): q("delete from agents where id=? and (owner=? or ?='*')", (i, u["email"], own(u))); return {"ok": True}

class Doc(BaseModel): name: str; content: str
@app.post("/api/docs")
def doc_add(d: Doc, u: dict = Depends(me)):
    if len(d.content) > 200_000: raise HTTPException(413, "Max 200 KB per document")
    if q("select count(*) c from docs where owner=?", (u["email"],))[0]["c"] >= 50: raise HTTPException(413, "Max 50 documents")
    q("insert into docs(owner,name,content,created) values(?,?,?,?)", (u["email"], d.name[:120], d.content, time.strftime("%Y-%m-%d %H:%M")))
    log("DATA", "AGENTLINK", "Document added", d.name[:60]); return {"ok": True}
@app.get("/api/docs")
def doc_list(u: dict = Depends(me)): return q("select id, name, length(content) size, created from docs where owner=? order by id desc", (u["email"],))
@app.delete("/api/docs/{i}")
def doc_del(i: int, u: dict = Depends(me)): q("delete from docs where id=? and owner=?", (i, u["email"])); return {"ok": True}

class Tk(BaseModel): title: str; prompt: str; every_min: int = 0
@app.post("/api/tasks")
def task_add(t: Tk, u: dict = Depends(me)):
    if not t.prompt.strip(): raise HTTPException(422, "Write what the agents should do")
    if q("select count(*) c from tasks where owner=?", (u["email"],))[0]["c"] >= 10: raise HTTPException(413, "Max 10 tasks")
    ev = 0 if t.every_min <= 0 else max(5, t.every_min)
    q("insert into tasks(owner,title,prompt,every_min,next_run,enabled) values(?,?,?,?,?,1)", (u["email"], t.title[:80], t.prompt[:500], ev, time.time() + 3)); log("TASK", "AGENTLINK", "Task created", t.title[:60]); return {"ok": True}
@app.get("/api/tasks")
def task_list(u: dict = Depends(me)): return q("select id,title,prompt,every_min,last_run,last_result,enabled from tasks where owner=? order by id desc", (u["email"],))
@app.post("/api/tasks/{i}/run")
def task_run(i: int, u: dict = Depends(me)):
    t = q("select * from tasks where id=? and owner=?", (i, u["email"]))
    if not t: raise HTTPException(404, "Task not found")
    threading.Thread(target=run_task, args=(t[0],), daemon=True).start(); return {"started": True}
@app.delete("/api/tasks/{i}")
def task_del(i: int, u: dict = Depends(me)): q("delete from tasks where id=? and owner=?", (i, u["email"])); return {"ok": True}

@app.get("/api/telegram")
def tg_status(u: dict = Depends(me)): return {"enabled": bool(TG), "username": TG_USER["name"], "linked": bool(q("select 1 from tg_links where email=?", (u["email"],)))}
@app.post("/api/telegram/code")
def tg_code(u: dict = Depends(me)):
    if not TG: raise HTTPException(400, "Telegram bot is not configured")
    c = secrets.token_hex(3).upper(); q("insert into tg_codes values(?,?,?)", (c, u["email"], time.time() + 600)); return {"code": c, "username": TG_USER["name"]}
@app.delete("/api/telegram/link")
def tg_unlink(u: dict = Depends(me)): q("delete from tg_links where email=?", (u["email"],)); return {"ok": True}

@app.on_event("startup")
def start_background():
    threading.Thread(target=scheduler, daemon=True).start()
    if TG:
        try: TG_USER["name"] = tg_api("getMe").get("result", {}).get("username", "")
        except Exception: pass
        threading.Thread(target=tg_poll, daemon=True).start()

app.mount("/", StaticFiles(directory=os.path.join(ROOT, "frontend"), html=True), name="fe")
