"""Агент на ПК: берёт запросы с сайта и выполняет их через локальную модель в LM Studio.

Память (memory.json), инструменты для файлов, защита от «я всё сделал», отмена запроса
кнопкой на сайте и мгновенный скриншот.

Переменные (все необязательные): SERVER, APP_TOKEN, WORKDIR, LM_URL, LM_MODEL,
MAX_STEPS, CMD_TIMEOUT, MEM_FILE."""

import os, io, re, json, time, base64, threading, subprocess, requests
from datetime import datetime

SERVER = os.environ.get("SERVER", "https://410f7thzhr.onrender.com").rstrip("/")
TOKEN = os.environ.get("APP_TOKEN", "")
WORKDIR = os.environ.get("WORKDIR", os.getcwd())
LM_URL = os.environ.get("LM_URL", "http://localhost:1234/v1").rstrip("/")
LM_MODEL = os.environ.get("LM_MODEL", "gemma-3-4b-it-qat")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "6"))
CMD_TIMEOUT = int(os.environ.get("CMD_TIMEOUT", "60"))
MEM_FILE = os.environ.get("MEM_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json"))
H = {"Authorization": "Bearer " + TOKEN} if TOKEN else {}

# приложить скриншот к ответу, если о нём попросили
SHOT = re.compile(r"скрин|скин|screenshot|снимок экрана|что на экране|покажи экран", re.I)
# запрос ТОЛЬКО на скриншот — модель не нужна, отвечаем сразу
PURE_SHOT = re.compile(
    r"^\W*(?:(?:сделай|сделать|сними|покажи|пришли|отправь|дай|скинь)\s+)?(?:мне\s+)?"
    r"(?:скриншот|скрин|скин|снимок экрана|экран)\W*$", re.I)
# запросы, где нужно реально что-то сделать на ПК (а не просто поболтать)
ACTION_RE = re.compile(r"созда|сделай файл|открой|запусти|включи|закрой|удали|найди файл|скачай|сохрани|запиши в файл|"
                       r"напиши в файл|прочитай файл|покажи файл|папк", re.I)

# команды, которые агент не выполнит, даже если модель их попросит
DANGER = re.compile(
    r"(^|[\s;&|])(format|diskpart|shutdown|rmdir|rd|del|erase|rm|remove-item|bcdedit|cipher|"
    r"reg\s+delete|vssadmin|takeown|icacls)(\s|$)", re.I)


class Cancelled(Exception):
    pass


def wants_shot(text):
    return bool(SHOT.search(text) or PURE_SHOT.match(text.strip()))


# ---------- известные папки ----------
def known_folder(csidl, fallback):
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return fallback


HOME = os.path.expanduser("~")
DESKTOP = known_folder(0x10, os.path.join(HOME, "Desktop"))   # учитывает OneDrive-рабочий стол
DOCS = known_folder(5, os.path.join(HOME, "Documents"))
DOWNLOADS = os.path.join(HOME, "Downloads")


# ---------- память ----------
def load_mem():
    try:
        with open(MEM_FILE, encoding="utf-8") as f:
            m = json.load(f)
        m.setdefault("facts", [])
        m.setdefault("history", [])
        return m
    except Exception:
        return {"facts": [], "history": []}


MEM = load_mem()


def save_mem():
    try:
        with open(MEM_FILE, "w", encoding="utf-8") as f:
            json.dump(MEM, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("память не сохранилась:", e)


def add_fact(t):
    t = t.strip().rstrip(".")
    if t and t.lower() not in [x.lower() for x in MEM["facts"]]:
        MEM["facts"].append(t[:300])
        del MEM["facts"][:-50]
        save_mem()


def add_history(q, a):
    MEM["history"].append({"q": q[:300], "a": a[:600]})
    del MEM["history"][:-20]
    save_mem()


def memory_cmd(text):
    """Команды памяти обрабатываются кодом, без модели — так надёжнее."""
    t = text.strip()
    m = re.match(r"(?is)^(?:запомни|запомните|запиши в память)[\s:,.\-]*(?:что\s+)?(.+)$", t)
    if m:
        add_fact(m.group(1))
        return "Запомнил: " + m.group(1).strip()
    if re.search(r"(?i)забудь\s+(всё|все|память)", t):
        MEM["facts"].clear()
        MEM["history"].clear()
        save_mem()
        return "Память очищена."
    if re.search(r"(?i)что ты (помнишь|знаешь)|покажи память", t):
        return ("Я помню:\n- " + "\n- ".join(MEM["facts"])) if MEM["facts"] else "Пока ничего не запомнил."
    return None


# ---------- промпт ----------
SYSTEM = r"""Ты — голосовой помощник и агент на компьютере пользователя (Windows). Ты действуешь через инструменты.
Сейчас: <<NOW>>. Рабочая папка: <<WORKDIR>>.
Папки пользователя: Рабочий стол = <<DESKTOP>> ; Документы = <<DOCS>> ; Загрузки = <<DOWNLOADS>>

Запрос пришёл голосом с телефона, в нём бывают ошибки распознавания — угадывай смысл по контексту и не переспрашивай по мелочам.

Что ты знаешь о пользователе (память):
<<FACTS>>

На КАЖДОМ шаге отвечай ТОЛЬКО одним JSON-объектом. Варианты:
{"action":"write_file","path":"<полный путь>","content":"<текст файла>"} — создать или перезаписать файл с нужным текстом
{"action":"read_file","path":"<полный путь>"} — прочитать файл
{"action":"cmd","command":"<команда cmd>"} — выполнить команду Windows
{"action":"remember","text":"<факт>"} — запомнить факт о пользователе надолго
{"action":"answer","text":"<ответ>"} — финальный ответ пользователю

Правила:
1. Если просят создать файл с текстом — используй write_file с полным путём и точным содержимым. НЕ открывай блокнот вместо этого.
2. Открывать программы и сайты — через cmd со start (start calc, start https://youtube.com).
3. Нельзя говорить, что задача выполнена, пока не получил результат инструмента. Не выдумывай результаты.
4. Если ничего делать на ПК не нужно (вопрос, разговор) — сразу answer.
5. Ответы короткие, по-русски. Если не знаешь — честно скажи.
6. Ничего не удаляй, не форматируй, не выключай ПК. Скриншот отправляется автоматически — сам не делай.
7. После результата инструмента дай answer, когда задача выполнена.

Примеры:
Запрос: Создай файл Teee.txt на рабочем столе, в нём слово привет
Ты: {"action":"write_file","path":"<<DESKTOP_J>>\\Teee.txt","content":"привет"}
Результат: Файл записан.
Ты: {"action":"answer","text":"Создал Teee.txt на рабочем столе."}

Запрос: открой калькулятор
Ты: {"action":"cmd","command":"start calc"}
Результат: (пустой вывод, код 0)
Ты: {"action":"answer","text":"Открыл калькулятор."}

Запрос: запомни что мою собаку зовут Рекс
Ты: {"action":"remember","text":"Собаку пользователя зовут Рекс"}
Результат: Запомнено.
Ты: {"action":"answer","text":"Запомнил."}

Запрос: привет, как дела
Ты: {"action":"answer","text":"Привет! Всё хорошо. Чем помочь?"}
"""

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "step",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["cmd", "write_file", "read_file", "remember", "answer"]},
                "command": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}
TOOLS = {"cmd", "write_file", "read_file", "remember"}


def system_prompt():
    facts = "\n".join("- " + f for f in MEM["facts"]) or "(пока ничего)"
    j = lambda s: s.replace("\\", "\\\\")
    return (SYSTEM.replace("<<NOW>>", datetime.now().strftime("%d.%m.%Y %H:%M"))
            .replace("<<WORKDIR>>", WORKDIR).replace("<<DESKTOP_J>>", j(DESKTOP))
            .replace("<<DESKTOP>>", DESKTOP).replace("<<DOCS>>", DOCS)
            .replace("<<DOWNLOADS>>", DOWNLOADS).replace("<<FACTS>>", facts))


# ---------- утилиты ----------
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


def send_shot(sid):
    """Скриншот по кнопке на сайте — работает даже пока модель думает."""
    try:
        res = {"id": sid, "text": "", "image": screenshot()}
    except Exception as e:
        res = {"id": sid, "text": "Скриншот не получился: %s" % e}
    try:
        requests.post(SERVER + "/agent/result", headers=H, timeout=60, json=res)
    except Exception as e:
        print("сеть:", e)


def ask_llm(messages, cancel):
    body = {"model": LM_MODEL, "messages": messages, "temperature": 0.2,
            "max_tokens": 1200, "stream": True, "response_format": SCHEMA}
    r = requests.post(LM_URL + "/chat/completions", json=body, timeout=(10, 300), stream=True)
    if r.status_code == 400:  # вдруг эта версия LM Studio не знает response_format
        r.close()
        body.pop("response_format")
        r = requests.post(LM_URL + "/chat/completions", json=body, timeout=(10, 300), stream=True)
    r.raise_for_status()
    parts = []
    try:
        for line in r.iter_lines():
            if cancel.is_set():
                raise Cancelled()
            if not line or not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                parts.append(json.loads(data)["choices"][0]["delta"].get("content") or "")
            except Exception:
                continue
    finally:
        r.close()  # обрыв соединения останавливает генерацию в LM Studio
    if cancel.is_set():
        raise Cancelled()
    return "".join(parts)


def parse(s):
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    raw = m.group()
    for cand in (raw, re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", raw)):
        try:
            return json.loads(cand)
        except Exception:
            pass
    return None


# ---------- инструменты ----------
def run_cmd(cmd, cancel):
    if DANGER.search(cmd):
        return "ЗАБЛОКИРОВАНО: опасная команда. Сделай другим способом или скажи пользователю, что не можешь."
    p = subprocess.Popen(cmd, shell=True, cwd=WORKDIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL)
    t0 = time.time()
    while True:
        try:
            out, err = p.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            stop = cancel.is_set()
            if stop or time.time() - t0 > CMD_TIMEOUT:
                try:  # убиваем всё дерево процессов
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
                except Exception:
                    p.kill()
                try:
                    p.communicate(timeout=3)
                except Exception:
                    pass
                if stop:
                    raise Cancelled()
                return "Таймаут: команда не закончилась за %d с." % CMD_TIMEOUT
    text = (dec(out) + dec(err)).strip()
    return (text or "(пустой вывод, код %d)" % p.returncode)[-2000:]


def safe_path(p):
    """Разрешаем работать с файлами только внутри папки пользователя."""
    p = os.path.expandvars((p or "").strip().strip('"'))
    if not p:
        return None
    if not os.path.isabs(p):
        p = os.path.join(WORKDIR, p)
    p = os.path.abspath(p)
    try:
        ok = os.path.commonpath([os.path.normcase(p), os.path.normcase(HOME)]) == os.path.normcase(HOME)
    except ValueError:
        ok = False
    return p if ok else None


def write_file(path, content):
    p = safe_path(path)
    if not p:
        return "ЗАБЛОКИРОВАНО: писать файлы можно только в папке пользователя (%s)." % HOME
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return "Файл записан: %s (%d байт)." % (p, os.path.getsize(p))
    except Exception as e:
        return "Ошибка записи файла: %s" % e


def read_file(path):
    p = safe_path(path)
    if not p:
        return "ЗАБЛОКИРОВАНО: читать можно только файлы в папке пользователя (%s)." % HOME
    try:
        with open(p, "rb") as f:
            return dec(f.read(4000)).strip() or "(файл пустой)"
    except Exception as e:
        return "Ошибка чтения файла: %s" % e


def do_tool(act, s, cancel):
    if act == "cmd":
        c = (s.get("command") or "").strip()
        print("   $", c)
        return run_cmd(c, cancel) if c else "Пустая команда."
    if act == "write_file":
        print("   записать:", s.get("path"))
        return write_file(s.get("path"), s.get("content") or "")
    if act == "read_file":
        print("   прочитать:", s.get("path"))
        return read_file(s.get("path"))
    if act == "remember":
        add_fact(s.get("text") or "")
        return "Запомнено."
    return "Неизвестное действие."


# ---------- основная логика ----------
def run_task(text, cancel):
    mc = memory_cmd(text)
    if mc:
        return mc
    if PURE_SHOT.match(text.strip()):  # просто скриншот — без модели, мгновенно
        return "Вот скриншот экрана."
    messages = [{"role": "system", "content": system_prompt()}]
    for h in MEM["history"][-6:]:
        messages.append({"role": "user", "content": h["q"]})
        messages.append({"role": "assistant",
                         "content": json.dumps({"action": "answer", "text": h["a"]}, ensure_ascii=False)})
    messages.append({"role": "user", "content": text})
    used_tool, nudged, last = False, False, ""
    try:
        for _ in range(MAX_STEPS + 1):
            if cancel.is_set():
                raise Cancelled()
            raw = ask_llm(messages, cancel)
            step = parse(raw)
            if not step:  # модель ответила не JSON-ом — считаем это ответом
                return raw.strip()[-3500:] or "Модель ничего не ответила."
            act = step.get("action")
            if act in TOOLS:
                last = do_tool(act, step, cancel)
                used_tool = True
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "Результат: " + last})
                continue
            ans = (step.get("text") or "").strip()
            if not used_tool and not nudged and ACTION_RE.search(text):
                # модель говорит «готово», хотя ничего не делала — возвращаем её к работе
                nudged = True
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                                 "Ты ещё ничего не выполнил. Нельзя говорить, что задача сделана, пока нет результата "
                                 "инструмента. Выбери действие: write_file, read_file или cmd."})
                continue
            return (ans or last or "Готово.")[-3500:]
    except Cancelled:
        return "Отменено."
    except requests.exceptions.ConnectionError:
        return ("LM Studio недоступен на %s. Загрузи модель %s и включи Local Server "
                "(вкладка Developer → Start Server)." % (LM_URL, LM_MODEL))
    except Exception as e:
        return "Ошибка модели: %s" % e
    return "Не уложился в %d шагов. Последний результат: %s" % (MAX_STEPS, last[-1500:])


print("Агент запущен. Сервер:", SERVER, "| папка:", WORKDIR, "| модель:", LM_MODEL, "@", LM_URL)
print("Память:", MEM_FILE, "| фактов: %d" % len(MEM["facts"]), "| рабочий стол:", DESKTOP)

connected = False
while True:
    try:
        r = requests.get(SERVER + "/agent/poll", headers=H, timeout=40)
        if r.status_code == 401:
            print("Сервер ждёт APP_TOKEN, а у агента он не задан или не совпадает")
            time.sleep(10)
            continue
        if r.status_code != 200:
            print("Сервер ответил %d (возможно, Render просыпается), жду..." % r.status_code)
            time.sleep(5)
            continue
        if not connected:
            print("Подключился к серверу, жду команды...")
            connected = True
        job = r.json()
        if not job:
            continue
        if job.get("name") == "screenshot":  # кнопка «Скриншот» на сайте
            send_shot(job["id"])
            continue

        text = job["args"]["text"]
        print("->", text)
        cancel = threading.Event()
        box = {}
        worker = threading.Thread(target=lambda: box.update(text=run_task(text, cancel)), daemon=True)
        worker.start()
        cancelled = False
        while worker.is_alive():
            worker.join(1.5)
            if not worker.is_alive():
                break
            try:  # пока модель думает: проверяем отмену и просьбы о скриншоте
                st = requests.get(SERVER + "/agent/state", params={"id": job["id"]}, headers=H, timeout=10).json()
            except Exception:
                continue
            for sid in st.get("shots", []):
                send_shot(sid)
            if st.get("cancel"):
                cancel.set()
                cancelled = True
                worker.join(15)
                break
        if cancelled:
            print("<- отменено")
            continue  # сервер уже ответил пользователю «Отменено.»

        res = {"text": box.get("text") or "Готово."}
        print("<-", res["text"][:200])
        if res["text"] != "Отменено.":
            add_history(text, res["text"])
        if wants_shot(text):
            try:
                res["image"] = screenshot()
            except Exception as e:
                res["text"] += "\n(скриншот не получился: %s)" % e
        requests.post(SERVER + "/agent/result", headers=H, timeout=60, json={"id": job["id"], **res})
    except Exception as e:
        connected = False
        print("сеть:", e)
        time.sleep(3)
