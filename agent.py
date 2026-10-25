"""Агент на ПК: берёт запросы с сайта и отдаёт их DeepSeek Harness (dsh, headless).
Переменные: SERVER, APP_TOKEN, WORKDIR, DSH_CMD, TASK_TIMEOUT."""
import os, io, re, time, base64, shlex, shutil, subprocess, requests

SERVER = os.environ.get("SERVER", "https://voice-remote.onrender.com").rstrip("/")
TOKEN = os.environ.get("APP_TOKEN", "")
WORKDIR = os.environ.get("WORKDIR", os.getcwd())
DSH_CMD = os.environ.get("DSH_CMD", "npx -y @deepseek-ai/dsh --profile headless")
TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "900"))
H = {"Authorization": "Bearer " + TOKEN}
SHOT = re.compile(r"скрин|скин|screenshot|экран", re.I)
HIST = []

def dec(b):
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("utf-8", "replace")

def screenshot():
    from PIL import ImageGrab
    im = ImageGrab.grab(all_screens=True)
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()

def run_dsh(text):
    ctx = " ".join("Запрос: %s Ответ: %s." % h for h in HIST[-3:])
    prompt = ("Ты работаешь на ПК пользователя, рабочая папка %s. Запрос пришёл голосом, возможны ошибки "
              "распознавания. Скриншот экрана пользователю отправляется автоматически, сам его не делай. "
              "Отвечай коротко по-русски. %s Новый запрос: %s"
              % (WORKDIR, ("Предыдущие запросы: " + ctx) if ctx else "", text))
    # одна строка и без символов, которые ломают cmd.exe при запуске npx.cmd
    prompt = " ".join(re.sub(r'["%^&|<>]', lambda m: "'" if m.group() == '"' else " ", prompt).split())
    parts = shlex.split(DSH_CMD, posix=False)
    exe = shutil.which(parts[0])
    if not exe:
        return "Не найден «%s». Установи Node.js или поправь DSH_CMD." % parts[0]
    try:
        r = subprocess.run([exe] + parts[1:] + [prompt], cwd=WORKDIR, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "Таймаут: dsh не закончил за %d с." % TIMEOUT
    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", dec(r.stdout)).strip()
    if not out:
        out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", dec(r.stderr)).strip() or "dsh ничего не вернул (код %d)." % r.returncode
    return out[-3500:]

print("Агент запущен. Сервер:", SERVER, "| папка:", WORKDIR, "| dsh:", DSH_CMD)
while True:
    try:
        r = requests.get(SERVER + "/agent/poll", headers=H, timeout=40)
        if r.status_code == 401:
            print("Неверный APP_TOKEN"); time.sleep(10); continue
        job = r.json() if r.status_code == 200 else None
        if not job:
            continue
        text = job["args"]["text"]
        print("->", text)
        res = {"text": run_dsh(text)}
        HIST.append((text[:200], res["text"][:300]))
        if SHOT.search(text):
            try:
                res["image"] = screenshot()
            except Exception as e:
                res["text"] += "\n(скриншот не получился: %s)" % e
        requests.post(SERVER + "/agent/result", headers=H, timeout=60, json={"id": job["id"], **res})
    except Exception as e:
        print("сеть:", e); time.sleep(3)
