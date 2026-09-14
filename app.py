import asyncio
import html
import json
import logging
import os
import re
import secrets
import string
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, jsonify, request
from playwright.async_api import async_playwright


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.json.ensure_ascii = False

BASE = "https://viw.ai"
SPAMOK_API = "https://api.spamok.com/v2"

PROFILE_DIR = (
    os.path.abspath("./chrome_profile")
    if os.name == "nt"
    else "/tmp/chrome_profile"
)
COOKIE_FILE = (
    Path("./session_cookies.json").resolve()
    if os.name == "nt"
    else Path("/tmp/session_cookies.json")
)

SCREENSHOT_INTERVAL = 3
MAX_JOB_SECONDS = 360

PLACEHOLDER_SVG = """<svg xmlns="http://www.w3.org/2000/svg"
width="1366" height="768" viewBox="0 0 1366 768">
<rect width="1366" height="768" fill="#020617"/>
<rect x="20" y="20" width="1326" height="728" rx="16"
fill="#0f172a" stroke="#334155"/>
<text x="683" y="365" text-anchor="middle" fill="#38bdf8"
font-size="28" font-family="sans-serif">Ekran görüntüsü bekleniyor...</text>
<text x="683" y="415" text-anchor="middle" fill="#94a3b8"
font-size="20" font-family="sans-serif">
İşlem başladığında otomatik güncellenir
</text>
</svg>"""


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lock: threading.Lock = field(default_factory=threading.Lock)
    refresh: threading.Event = field(default_factory=threading.Event)
    events: deque = field(default_factory=lambda: deque(maxlen=1000))
    sequence: int = 0
    image: bytes | None = None
    image_time: float = 0
    done: bool = False
    success: bool = False


STATE_LOCK = threading.Lock()
CURRENT_JOB: Job | None = None


def log_event(job, message="", kind="log", **extra):
    with job.lock:
        job.sequence += 1
        job.events.append((
            job.sequence,
            {"kind": kind, "message": message, **extra},
        ))


def find_job(job_id):
    with STATE_LOCK:
        job = CURRENT_JOB
    return job if job and job.id == job_id else None


@app.before_request
def check_post():
    # Kimlik doğrulama değildir; basit çapraz site form isteklerini önler.
    if request.method == "POST":
        if request.headers.get("X-Requested-With") != "ViwPanel":
            return jsonify(error="Geçersiz istek."), 403


@app.after_request
def add_headers(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/healthz")
def health():
    return jsonify(status="ok")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/screenshot")
def screenshot():
    job = find_job(request.args.get("job", ""))

    if job:
        with job.lock:
            image = job.image
            stamp = job.image_time

        if image:
            return Response(
                image,
                mimetype="image/jpeg",
                headers={"X-Screenshot-Time": str(stamp)},
            )

    return Response(PLACEHOLDER_SVG, mimetype="image/svg+xml")


@app.get("/status")
def status():
    with STATE_LOCK:
        job = CURRENT_JOB

    if job is None:
        return jsonify(job_id=None)

    with job.lock:
        return jsonify(
            job_id=job.id,
            done=job.done,
            success=job.success,
        )


@app.post("/refresh-screenshot")
def refresh_screenshot():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Geçersiz veri."), 400

    job = find_job(str(data.get("job_id", "")))
    if job is None:
        return jsonify(error="İşlem bulunamadı."), 404

    with job.lock:
        active = not job.done

    if active:
        job.refresh.set()

    return jsonify(ok=True, active=active)


@app.post("/start-signup")
def start_signup():
    global CURRENT_JOB

    with STATE_LOCK:
        if CURRENT_JOB:
            with CURRENT_JOB.lock:
                busy = not CURRENT_JOB.done

            if busy:
                return jsonify(
                    error="Zaten çalışan bir işlem var.",
                    job_id=CURRENT_JOB.id,
                ), 409

        job = Job()
        CURRENT_JOB = job

        try:
            threading.Thread(
                target=run_job,
                args=(job,),
                daemon=True,
                name=f"signup-{job.id[:8]}",
            ).start()
        except Exception:
            CURRENT_JOB = None
            logger.exception("İşlem başlatılamadı.")
            return jsonify(error="İşlem başlatılamadı."), 500

    return jsonify(job_id=job.id), 202


@app.get("/events/<job_id>")
def events(job_id):
    job = find_job(job_id)
    if job is None:
        return jsonify(error="İşlem bulunamadı."), 404

    try:
        last_id = max(0, int(request.headers.get("Last-Event-ID", "0")))
    except ValueError:
        last_id = 0

    def generate():
        cursor = last_id
        heartbeat = time.monotonic()
        yield "retry: 3000\n\n"

        while True:
            with job.lock:
                pending = [
                    (number, payload)
                    for number, payload in job.events
                    if number > cursor
                ]
                finished = job.done

            for number, payload in pending:
                body = json.dumps(payload, ensure_ascii=False)
                yield f"id: {number}\ndata: {body}\n\n"
                cursor = number

            if finished:
                return

            if time.monotonic() - heartbeat >= 10:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()

            time.sleep(0.25)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


async def capture_screenshot(page, job):
    try:
        if page.is_closed():
            return

        # Görüntü tamamen hazır olmadan HTTP tarafına aktarılmaz.
        image = await page.screenshot(
            type="jpeg",
            quality=70,
            full_page=False,
            timeout=12000,
        )

        with job.lock:
            job.image = image
            job.image_time = time.time()

        log_event(job, kind="screenshot")

    except Exception as exc:
        logger.warning(
            "Ekran görüntüsü alınamadı: %s",
            type(exc).__name__,
        )


async def screenshot_loop(page, job):
    while True:
        job.refresh.clear()
        await capture_screenshot(page, job)

        deadline = time.monotonic() + SCREENSHOT_INTERVAL
        while time.monotonic() < deadline:
            if job.refresh.is_set():
                break
            await asyncio.sleep(0.2)


def fetch_json(url):
    with requests.get(url, timeout=(5, 15)) as response:
        response.raise_for_status()
        return response.json()


def extract_magic_link(message):
    decoded = html.unescape(message)
    candidates = re.findall(
        r"https://viw\.ai/api/auth/magic-link/verify\?[^\s\"'<>]+",
        decoded,
    )

    for candidate in candidates:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "viw.ai"
            and parsed.path == "/api/auth/magic-link/verify"
            and re.search(r"(?:^|&)token=[^&]+", parsed.query)
        ):
            return candidate

    return None


async def wait_for_magic_link(prefix, job):
    deadline = time.monotonic() + 90
    attempts = {}

    while time.monotonic() < deadline:
        try:
            mailbox = await asyncio.to_thread(
                fetch_json,
                f"{SPAMOK_API}/EmailBox/{prefix}",
            )
            mails = (
                mailbox.get("mails", [])
                if isinstance(mailbox, dict)
                else []
            )

            if not isinstance(mails, list):
                mails = []

            for mail in mails:
                if not isinstance(mail, dict):
                    continue
                if "viw" not in str(mail.get("subject", "")).lower():
                    continue

                message_id = str(mail.get("id", ""))
                if not re.fullmatch(r"[A-Za-z0-9_-]+", message_id):
                    continue
                if attempts.get(message_id, 0) >= 3:
                    continue

                detail = await asyncio.to_thread(
                    fetch_json,
                    f"{SPAMOK_API}/Email/{prefix}/{message_id}",
                )
                if not isinstance(detail, dict):
                    continue

                attempts[message_id] = attempts.get(message_id, 0) + 1
                message = (
                    str(detail.get("messagePlain") or "")
                    + "\n"
                    + str(detail.get("messageHtml") or "")
                )

                link = extract_magic_link(message)
                if link:
                    return link

        except Exception as exc:
            log_event(
                job,
                f"[!] Mail kontrol hatası: {type(exc).__name__}",
            )

        await asyncio.sleep(3)

    raise RuntimeError(
        "90 saniye içinde doğrulama e-postası bulunamadı."
    )


async def wait_for_turnstile(page, job):
    log_event(job, "[5] Turnstile doğrulaması kontrol ediliyor...")

    script = """() => {
        const input = document.querySelector(
            'input[name="cf-turnstile-response"]'
        );
        const widget = document.querySelector(
            '.cf-turnstile, iframe[src*="challenges.cloudflare.com"],'
            + ' iframe[src*="turnstile"]'
        );
        return {
            present: Boolean(input || widget),
            ready: Boolean(input && input.value)
        };
    }"""

    for _ in range(30):
        state = await page.evaluate(script)

        if state["ready"]:
            log_event(job, "[+] Doğrulama tamamlandı.")
            return

        if not state["present"]:
            log_event(job, "[*] Sayfada Turnstile alanı bulunmadı.")
            return

        await asyncio.sleep(1)

    raise RuntimeError(
        "Turnstile doğrulaması tamamlanmadı. "
        "Panel yalnızca ekran görüntüsü gösterir; "
        "etkileşimli doğrulama yapılamaz."
    )


async def signup_steps(page, context, job):
    alphabet = string.ascii_lowercase + string.digits
    prefix = "".join(secrets.choice(alphabet) for _ in range(12))
    email = f"{prefix}@spamok.com"

    log_event(job, f"[*] Üretilen e-posta: {email}")
    log_event(job, "[1] https://viw.ai/ açılıyor...")

    await page.goto(
        BASE,
        wait_until="domcontentloaded",
        timeout=60000,
    )
    await asyncio.sleep(2)

    log_event(job, "[2] Login butonu aranıyor...")
    login = page.locator(
        "a:has-text('Login'), button:has-text('Login')"
    ).first
    await login.wait_for(state="visible", timeout=20000)
    await login.click()
    await asyncio.sleep(2)

    log_event(job, "[3] Continue with Email seçiliyor...")
    email_button = page.locator(
        "button:has-text('Continue with Email'), "
        "span:has-text('Continue with Email')"
    ).first
    await email_button.wait_for(state="visible", timeout=20000)
    await email_button.click()

    log_event(job, "[4] E-posta yazılıyor...")
    email_input = page.locator(
        "input#email, input[type='email']"
    ).first
    await email_input.wait_for(state="visible", timeout=20000)
    await email_input.fill(email)
    await asyncio.sleep(2)

    await wait_for_turnstile(page, job)

    log_event(job, "[6] Form gönderiliyor...")
    submit = page.locator("button[type='submit']:visible").first
    await submit.wait_for(state="visible", timeout=15000)
    await submit.click(timeout=15000)
    await asyncio.sleep(3)

    log_event(job, "[7] Doğrulama bağlantısı bekleniyor...")
    magic_link = await wait_for_magic_link(prefix, job)

    # Oturum açma bağlantısı loglarda gösterilmez.
    log_event(job, "[+] Doğrulama bağlantısı alındı.")
    log_event(job, "[8] Doğrulama bağlantısı açılıyor...")

    await page.goto(
        magic_link,
        wait_until="domcontentloaded",
        timeout=60000,
    )
    await asyncio.sleep(3)

    log_event(job, "[9] Oturum kontrol ediliyor...")
    session_data = None

    for _ in range(5):
        session_data = await page.evaluate("""async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 8000);

            try {
                const response = await fetch('/api/auth/get-session', {
                    credentials: 'include',
                    cache: 'no-store',
                    signal: controller.signal
                });
                return response.ok ? await response.json() : null;
            } catch {
                return null;
            } finally {
                clearTimeout(timer);
            }
        }""")

        if (
            isinstance(session_data, dict)
            and isinstance(session_data.get("user"), dict)
            and session_data["user"]
        ):
            break

        await asyncio.sleep(2)

    if not (
        isinstance(session_data, dict)
        and isinstance(session_data.get("user"), dict)
        and session_data["user"]
    ):
        raise RuntimeError(
            "Doğrulama bağlantısı açıldı ancak aktif oturum doğrulanamadı."
        )

    actual_email = str(session_data["user"].get("email") or "")
    if actual_email.lower() != email.lower():
        raise RuntimeError(
            "Açık oturumun e-postası oluşturulan e-postayla eşleşmiyor. "
            "Eski profil oturumu açık kalmış olabilir."
        )

    log_event(job, "[+] Oturum başarıyla açıldı.")
    cookies = await context.cookies()
    temp_file = COOKIE_FILE.with_name(
        f"{COOKIE_FILE.name}.{job.id}.tmp"
    )

    try:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(temp_file),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(cookies, handle, indent=2, ensure_ascii=False)

        os.replace(temp_file, COOKIE_FILE)
        log_event(job, "[+] Çerezler yerel dosyaya kaydedildi.")

    except Exception as exc:
        logger.warning("Çerez kayıt hatası: %s", type(exc).__name__)
        log_event(job, "[!] Oturum açıldı ancak çerezler kaydedilemedi.")

    finally:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass

    log_event(job, f"[+] E-posta: {email}")
    log_event(job, f"[+] Toplam çerez: {len(cookies)}")


async def browser_job(job):
    async with async_playwright() as playwright:
        context = None
        page = None
        screenshot_task = None

        try:
            log_event(job, "[*] Tarayıcı başlatılıyor (headless=False)...")

            options = {
                "user_data_dir": PROFILE_DIR,
                "headless": False,
                "viewport": {"width": 1366, "height": 768},
                "device_scale_factor": 1,
                "timeout": 60000,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1366,768",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }

            if os.name == "nt":
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        channel="chrome", **options
                    )
                except Exception:
                    context = await playwright.chromium.launch_persistent_context(
                        **options
                    )
            else:
                context = await playwright.chromium.launch_persistent_context(
                    **options
                )

            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(20000)

            screenshot_task = asyncio.create_task(
                screenshot_loop(page, job)
            )
            await signup_steps(page, context, job)

        finally:
            if screenshot_task is not None:
                screenshot_task.cancel()
                try:
                    await screenshot_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.error("Görüntü görevi kapatılamadı.")

            # Hata anının görüntüsü tarayıcı kapanmadan önce alınır.
            if page is not None:
                await capture_screenshot(page, job)

            if context is not None:
                try:
                    await asyncio.wait_for(context.close(), timeout=15)
                except Exception:
                    logger.error("Tarayıcı kapatılırken hata.")


async def timed_job(job):
    await asyncio.wait_for(browser_job(job), timeout=MAX_JOB_SECONDS)


def run_job(job):
    success = False

    try:
        asyncio.run(timed_job(job))
        success = True
        log_event(job, "[BITTI] Hesap ve oturum doğrulandı.")

    except TimeoutError:
        log_event(job, "[HATA] İşlem zaman aşımına uğradı.")

    except Exception as exc:
        logger.error("İşlem hatası: %s", type(exc).__name__)
        message = (
            str(exc)
            if type(exc) is RuntimeError
            else (
                f"{type(exc).__name__}. "
                "Son işlem adımını ve ekran görüntüsünü kontrol edin."
            )
        )
        log_event(job, f"[HATA] {message}")

    finally:
        # Son olay ve bitiş bilgisi aynı kilit altında güncellenir.
        with job.lock:
            job.success = success
            job.sequence += 1
            job.events.append((
                job.sequence,
                {
                    "kind": "done",
                    "message": "Tamamlandı" if success else "Başarısız",
                    "success": success,
                },
            ))
            job.done = True


HOME_HTML = r"""
<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Viw.AI Hesap Oluşturucu</title>
<style>
* { box-sizing: border-box; }
body {
    margin: 0; padding: 24px; background: #0b1120; color: #f8fafc;
    font-family: system-ui, sans-serif;
}
main { max-width: 1400px; margin: auto; }
h1 { color: #38bdf8; font-size: 25px; }
p { color: #94a3b8; line-height: 1.6; }
.buttons { display: flex; flex-wrap: wrap; gap: 12px; margin: 20px 0; }
button {
    padding: 13px 20px; border: 1px solid #475569; border-radius: 10px;
    background: #334155; color: white; font-size: 15px; cursor: pointer;
}
#start { background: #2563eb; }
button:disabled { opacity: .5; cursor: not-allowed; }
.grid {
    display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr);
    gap: 16px;
}
.panel {
    min-width: 0; background: #020617; border: 1px solid #334155;
    border-radius: 12px; overflow: hidden;
}
.header {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 12px; background: #1e293b; font-size: 13px;
}
#terminal {
    height: 460px; margin: 0; padding: 15px; overflow: auto;
    white-space: pre-wrap; overflow-wrap: anywhere;
    color: #7dd3fc; font: 13px/1.6 monospace;
}
.preview {
    padding: 8px; min-height: 280px; display: flex;
    align-items: center; justify-content: center;
}
#screen { display: block; width: 100%; height: auto; object-fit: contain; }
#imageStatus { padding: 12px; color: #94a3b8; font-size: 12px; }
@media (max-width: 900px) {
    body { padding: 12px; }
    .grid { grid-template-columns: minmax(0, 1fr); }
    #terminal { height: 280px; }
}
</style>
</head>
<body>
<main>
<h1>Viw.AI Otomatik Hesap Açıcı</h1>
<p>Canlı işlem logları ve tarayıcının son ekran görüntüsü.</p>
<div class="buttons">
    <button id="start">Hesap Aç</button>
    <button id="refresh">Anlık SS Al / Yenile</button>
</div>
<div class="grid">
    <section class="panel">
        <div class="header">
            <span>Canlı Konsol Logları</span>
            <span id="status">Hazır</span>
        </div>
        <pre id="terminal">Sistem hazır.</pre>
    </section>
    <section class="panel">
        <div class="header">
            <span>Canlı Tarayıcı Ekranı</span>
            <span id="imageTime">Henüz görüntü yok</span>
        </div>
        <div class="preview">
            <img id="screen" src="/screenshot" alt="Tarayıcı ekran görüntüsü">
        </div>
        <div id="imageStatus">Ekran görüntüsü bekleniyor.</div>
    </section>
</div>
</main>
<script>
const startButton = document.getElementById("start");
const refreshButton = document.getElementById("refresh");
const terminal = document.getElementById("terminal");
const statusBox = document.getElementById("status");
const screenImage = document.getElementById("screen");
const imageTime = document.getElementById("imageTime");
const imageStatus = document.getElementById("imageStatus");

let jobId = null;
let eventSource = null;
let pollTimer = null;
let currentImageURL = null;
let fetchingImage = false;
let refreshAgain = false;
let lastImageStamp = null;

function appendLog(message) {
    terminal.textContent += message + "\n";
    terminal.scrollTop = terminal.scrollHeight;
}

async function api(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    const options = {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal
    };

    if (body !== undefined) {
        options.method = "POST";
        options.headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "ViwPanel"
        };
        options.body = JSON.stringify(body);
    }

    try {
        const response = await fetch(path, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data.error || `HTTP ${response.status}`);
            error.data = data;
            throw error;
        }
        return data;
    } finally {
        clearTimeout(timer);
    }
}

async function loadScreenshot() {
    if (!jobId) return;
    if (fetchingImage) {
        refreshAgain = true;
        return;
    }

    fetchingImage = true;
    const requestedJob = jobId;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    let temporaryURL = null;

    try {
        const response = await fetch(
            `/screenshot?job=${encodeURIComponent(requestedJob)}&t=${Date.now()}`,
            {
                cache: "no-store",
                credentials: "same-origin",
                signal: controller.signal
            }
        );

        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const type = response.headers.get("Content-Type") || "";
        if (!type.startsWith("image/")) {
            throw new Error("Sunucu görüntü döndürmedi.");
        }

        const stamp = response.headers.get("X-Screenshot-Time");
        const blob = await response.blob();

        if (requestedJob !== jobId) return;
        if (stamp && stamp === lastImageStamp) return;

        temporaryURL = URL.createObjectURL(blob);
        const preview = new Image();
        preview.src = temporaryURL;
        await preview.decode();

        if (requestedJob !== jobId) return;

        const previousURL = currentImageURL;
        currentImageURL = temporaryURL;
        temporaryURL = null;
        screenImage.src = currentImageURL;
        lastImageStamp = stamp;

        if (previousURL) URL.revokeObjectURL(previousURL);

        imageTime.textContent = stamp
            ? new Date(Number(stamp) * 1000).toLocaleTimeString("tr-TR")
            : "Henüz görüntü yok";
        imageStatus.textContent = stamp
            ? "Son başarılı ekran görüntüsü."
            : "Tarayıcı görüntüsü bekleniyor.";

    } catch (error) {
        if (requestedJob === jobId) {
            imageStatus.textContent =
                "Görüntü alınamadı; önceki görüntü korunuyor. " + error.message;
        }
    } finally {
        clearTimeout(timer);
        if (temporaryURL) URL.revokeObjectURL(temporaryURL);
        fetchingImage = false;

        if (refreshAgain) {
            refreshAgain = false;
            void loadScreenshot();
        }
    }
}

function stopWatching() {
    if (eventSource) eventSource.close();
    if (pollTimer) clearInterval(pollTimer);
    eventSource = null;
    pollTimer = null;
}

function finish(success) {
    stopWatching();
    startButton.disabled = false;
    statusBox.textContent = success ? "Tamamlandı" : "Hata";
    void loadScreenshot();
}

function watchJob(id) {
    stopWatching();
    jobId = id;
    lastImageStamp = null;
    terminal.textContent = "";
    startButton.disabled = true;
    statusBox.textContent = "Çalışıyor";

    void loadScreenshot();
    pollTimer = setInterval(loadScreenshot, 5000);

    const source = new EventSource(`/events/${encodeURIComponent(id)}`);
    eventSource = source;

    source.onopen = () => {
        if (eventSource === source) statusBox.textContent = "Bağlı";
    };

    source.onmessage = event => {
        if (eventSource !== source) return;
        let data;
        try {
            data = JSON.parse(event.data);
        } catch {
            return;
        }

        if (data.kind === "screenshot") {
            void loadScreenshot();
        } else if (data.kind === "done") {
            finish(data.success);
        } else {
            appendLog(data.message || "");
        }
    };

    source.onerror = async () => {
        if (eventSource !== source) return;
        statusBox.textContent = "Yeniden bağlanıyor";

        try {
            const state = await api("/status");
            if (eventSource !== source) return;

            if (state.job_id !== id) {
                stopWatching();
                startButton.disabled = false;
                statusBox.textContent = "İşlem bulunamadı";
                appendLog("[!] Sunucu yeniden başlamış veya işlem değişmiş olabilir.");
            } else if (state.done) {
                // Son logları baştan al; done olayı bağlantıyı kapatacak.
                watchJob(id);
            }
        } catch {
            // EventSource otomatik yeniden bağlanmayı sürdürür.
        }
    };
}

startButton.addEventListener("click", async () => {
    startButton.disabled = true;
    statusBox.textContent = "Başlatılıyor";

    try {
        const data = await api("/start-signup", {});
        watchJob(data.job_id);
    } catch (error) {
        if (error.data && error.data.job_id) {
            watchJob(error.data.job_id);
            return;
        }
        appendLog("[HATA] " + error.message);
        statusBox.textContent = "Hata";
        startButton.disabled = false;
    }
});

refreshButton.addEventListener("click", async () => {
    if (!jobId) {
        imageStatus.textContent = "Önce bir işlem başlatın.";
        return;
    }

    refreshButton.disabled = true;
    try {
        const data = await api("/refresh-screenshot", {job_id: jobId});
        await loadScreenshot();
        imageStatus.textContent = data.active
            ? "Yeni görüntü istendi; hazır olduğunda gösterilecek."
            : "İşlem bitti. Son kaydedilmiş görüntü gösteriliyor.";
    } catch (error) {
        imageStatus.textContent = error.message;
    } finally {
        refreshButton.disabled = false;
    }
});

async function restoreSession() {
    startButton.disabled = true;
    try {
        const state = await api("/status");
        if (state.job_id) {
            watchJob(state.job_id);
        } else {
            startButton.disabled = false;
        }
    } catch (error) {
        appendLog("[HATA] " + error.message);
        startButton.disabled = false;
    }
}

window.addEventListener("beforeunload", () => {
    stopWatching();
    if (currentImageURL) URL.revokeObjectURL(currentImageURL);
});

void restoreSession();
</script>
</body>
</html>
"""


@app.get("/")
def home():
    return Response(
        HOME_HTML,
        content_type="text/html; charset=utf-8",
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        threaded=True,
        debug=False,
    )
