import os, time, base64, hmac, threading, uuid, queue
from functools import wraps
import requests
from flask import Flask, request, jsonify, send_from_directory, Response

app = Flask(__name__, static_folder="static")
E = os.environ.get
TOKEN = E("APP_TOKEN", "")
STT_URL = E("STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
STT_KEY = E("STT_KEY", "")
STT_MODEL = E("STT_MODEL", "whisper-large-v3-turbo")
STT_LANG = E("STT_LANG", "ru")

LOCK, RUN = threading.Lock(), threading.Lock()
MSGS, IMGS = [], {}
JOBS, WAIT = queue.Queue(), {}
agent_seen, nid = 0.0, 0

def add(role, text="", img=None):
    global nid
    with LOCK:
        nid += 1
        m = {"id": nid, "role": role, "text": text}
        if img:
            iid = uuid.uuid4().hex
            IMGS[iid] = img
            m["img"] = iid
            while len(IMGS) > 20:
                IMGS.pop(next(iter(IMGS)))
        MSGS.append(m)
        del MSGS[:-200]
    return m

def guard(f):
    @wraps(f)
    def w(*a, **k):
        t = request.headers.get("Authorization", "").replace("Bearer ", "").strip() or request.args.get("t", "")
        if not TOKEN or not hmac.compare_digest(t, TOKEN):
            return jsonify(error="Unauthorized"), 401
        return f(*a, **k)
    return w

def call_agent(name, args, timeout=900):
    if time.time() - agent_seen > 60:
        return {"text": "ПК-агент не в сети. Запусти agent.py на компьютере."}
    jid = uuid.uuid4().hex
    slot = {"ev": threading.Event(), "res": None}
    WAIT[jid] = slot
    JOBS.put({"id": jid, "name": name, "args": args})
    ok = slot["ev"].wait(timeout)
    WAIT.pop(jid, None)
    return slot["res"] if ok else {"text": "Таймаут: ПК не ответил."}

def process(text, out):
    with RUN:
        add("user", text)
        res = call_agent("task", {"text": text})
        if res.get("image"):
            add("assistant", "", base64.b64decode(res["image"]))
        reply = res.get("text") or "Готово."
        add("assistant", reply)
        out["reply"] = reply

def start(text):
    out = {}
    t = threading.Thread(target=process, args=(text, out), daemon=True)
    t.start()
    if request.args.get("wait"):
        t.join(95)
        return jsonify(text=text, reply=out.get("reply", "Работаю — результат смотри на сайте."))
    return jsonify(text=text)

@app.post("/api/text")
@guard
def api_text():
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify(error="Пустой запрос"), 400
    return start(text)

@app.post("/api/voice")
@guard
def api_voice():
    f = request.files.get("audio") or request.files.get("file")
    if not f:
        return jsonify(error="Нет аудио"), 400
    try:
        r = requests.post(STT_URL, timeout=60, headers={"Authorization": "Bearer " + STT_KEY},
                          files={"file": (f.filename or "voice.m4a", f.read(), f.mimetype or "audio/mp4")},
                          data={"model": STT_MODEL, "language": STT_LANG})
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
    except Exception:
        return jsonify(error="Не удалось распознать голос"), 502
    if not text:
        return jsonify(error="Ничего не слышно"), 422
    return start(text)

@app.get("/api/messages")
@guard
def api_messages():
    a = int(request.args.get("after", 0))
    return jsonify(messages=[m for m in MSGS if m["id"] > a], busy=RUN.locked(),
                   agent=time.time() - agent_seen < 60 or RUN.locked())

@app.get("/img/<iid>")
@guard
def img(iid):
    d = IMGS.get(iid)
    return Response(d, mimetype="image/png") if d else ("", 404)

@app.get("/agent/poll")
@guard
def agent_poll():
    global agent_seen
    end = time.time() + 25
    while time.time() < end:
        agent_seen = time.time()
        try:
            return jsonify(JOBS.get(timeout=2))
        except queue.Empty:
            pass
    return jsonify(None)

@app.post("/agent/result")
@guard
def agent_result():
    d = request.get_json(silent=True) or {}
    s = WAIT.get(d.get("id"))
    if s:
        s["res"] = {"text": d.get("text", ""), "image": d.get("image")}
        s["ev"].set()
    return jsonify(ok=True)

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/health")
def health():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(E("PORT", 8000)), threaded=True)
