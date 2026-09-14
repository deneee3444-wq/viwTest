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

import requests
from flask import Flask, Response, jsonify, request
from playwright.async_api import async_playwright


logging.basicConfig(level=logging.INFO)
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

PLACEHOLDER_SVG = """<svg xmlns="http://www.w3.org/2000/svg"
width="1920" height="1080" viewBox="0 0 1920 1080">
<rect width="1920" height="1080" fill="#020617"/>
<text x="960" y="540" text-anchor="middle" fill="#38bdf8"
font-size="38" font-family="sans-serif">Ekran görüntüsü bekleniyor...</text>
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
CURRENT_JOB = None


def log(job, message="", kind="log", **extra):
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
def headers(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/healthz")
def health():
    return jsonify(status="ok")


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


@app.post("/refresh-screenshot")
def refresh_screenshot():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Geçersiz veri."), 400

    job = find_job(data.get("job_id"))
    if not job:
        return jsonify(error="İşlem bulunamadı."), 404

    with job.lock:
        active = not job.done

    if active:
        job.refresh.set()

    return jsonify(active=active)


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
            ).start()
        except Exception:
            CURRENT_JOB = None
            logger.exception("İşlem başlatılamadı.")
            return jsonify(error="İşlem başlatılamadı."), 500

    return jsonify(job_id=job.id), 202


@app.get("/events/<job_id>")
def events(job_id):
    job = find_job(job_id)
    if not job:
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


async def capture(page, job, capture_lock):
    async with capture_lock:
        try:
            if page.is_closed():
                return

            image = await page.screenshot(
                type="jpeg",
                quality=75,
                full_page=False,
                timeout=12000,
            )
            # Yalnızca tamamlanmış görüntü HTTP tarafına aktarılır.
            with job.lock:
                job.image = image
                job.image_time = time.time()

            log(job, kind="screenshot")
        except Exception as exc:
            logger.warning("Ekran görüntüsü hatası: %s", type(exc).__name__)


async def screenshot_loop(page, job, capture_lock):
    while True:
        job.refresh.clear()
        await capture(page, job, capture_lock)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if job.refresh.is_set():
                break
            await asyncio.sleep(0.2)


def fetch_json(url):
    with requests.get(url, timeout=(5, 15)) as response:
        response.raise_for_status()
        return response.json()


async def wait_mail(prefix, job):
    deadline = time.monotonic() + 90
    attempts = {}

    while time.monotonic() < deadline:
        try:
            mailbox = await asyncio.to_thread(
                fetch_json, f"{SPAMOK_API}/EmailBox/{prefix}"
            )
            mails = mailbox.get("mails", []) if isinstance(mailbox, dict) else []
            if not isinstance(mails, list):
                mails = []

            for mail in mails:
                if not isinstance(mail, dict):
                    continue
                if "viw" not in str(mail.get("subject", "")).lower():
                    continue

                mid = str(mail.get("id", ""))
                if not re.fullmatch(r"[A-Za-z0-9_-]+", mid):
                    continue
                if attempts.get(mid, 0) >= 3:
                    continue

                detail = await asyncio.to_thread(
                    fetch_json, f"{SPAMOK_API}/Email/{prefix}/{mid}"
                )
                if not isinstance(detail, dict):
                    continue

                attempts[mid] = attempts.get(mid, 0) + 1
                text = html.unescape(
                    str(detail.get("messagePlain") or "")
                    + "\n"
                    + str(detail.get("messageHtml") or "")
                )
                match = re.search(
                    r"https://viw\.ai/api/auth/magic-link/verify"
                    r"\?token=[^\s\"'<>&]+"
                    r"(?:&callbackURL=[^\s\"'<>]+)?",
                    text,
                )
                if match:
                    return match.group(0)

        except Exception as exc:
            log(job, f"[!] Mail kontrol hatası: {type(exc).__name__}")

        await asyncio.sleep(3)

    raise RuntimeError("Doğrulama e-postası 90 saniye içinde gelmedi.")


async def signup_steps(page, context, job, capture_lock):
    alphabet = string.ascii_lowercase + string.digits
    prefix = "".join(secrets.choice(alphabet) for _ in range(12))
    email = f"{prefix}@spamok.com"

    log(job, f"[*] Üretilen e-posta: {email}")
    log(job, "[1] https://viw.ai/ açılıyor...")

    await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    await capture(page, job, capture_lock)

    # İlk koddaki seçici, bekleme ve force=True geri alındı.
    log(job, "[2] Giriş butonu aranıyor...")
    login_selector = "a:has-text('Login'), button:has-text('Login')"
    await page.wait_for_selector(login_selector, timeout=15000)
    login_btn = page.locator(login_selector).first
    await login_btn.click(force=True)
    await page.wait_for_timeout(2000)
    await capture(page, job, capture_lock)

    log(job, "[3] Continue with Email seçeneği tıklanıyor...")
    email_selector = (
        "button:has-text('Continue with Email'), "
        "span:has-text('Continue with Email')"
    )
    await page.wait_for_selector(email_selector, timeout=15000)
    await page.locator(email_selector).first.click(force=True)
    await page.wait_for_timeout(2000)
    await capture(page, job, capture_lock)

    log(job, f"[4] E-posta yazılıyor: {email}")
    await page.wait_for_selector("input#email, input[type='email']", timeout=15000)
    await page.locator("input#email, input[type='email']").first.fill(email)
    await page.wait_for_timeout(1500)
    await capture(page, job, capture_lock)

    log(job, "[5] Turnstile doğrulaması kontrol ediliyor...")
    for _ in range(30):
        state = await page.evaluate("""() => {
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
        }""")
        if state["ready"]:
            log(job, "[+] Doğrulama tamamlandı.")
            break
        if not state["present"]:
            log(job, "[*] Turnstile alanı bulunmadı.")
            break
        await asyncio.sleep(1)
    else:
        raise RuntimeError(
            "Turnstile doğrulaması tamamlanmadı. "
            "Ekran görüntüsü paneli etkileşimli uzak masaüstü değildir."
        )

    log(job, "[6] Form gönderiliyor...")
    submit_btn = page.locator("button[type='submit']")
    await submit_btn.click(timeout=15000)
    await page.wait_for_timeout(4000)
    await capture(page, job, capture_lock)

    log(job, "[7] Doğrulama bağlantısı bekleniyor...")
    magic_link = await wait_mail(prefix, job)

    log(job, "[+] Doğrulama bağlantısı alındı.")
    log(job, "[8] Doğrulama bağlantısı açılıyor...")
    await page.goto(magic_link, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(3000)
    await capture(page, job, capture_lock)

    log(job, "[9] Oturum kontrol ediliyor...")
    session_data = await page.evaluate("""async () => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);
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

    user = session_data.get("user") if isinstance(session_data, dict) else None
    if not isinstance(user, dict) or not user:
        raise RuntimeError("Aktif oturum doğrulanamadı.")

    if str(user.get("email") or "").lower() != email.lower():
        raise RuntimeError(
            "Açık oturum farklı bir e-postaya ait. "
            "Kalıcı profilde eski oturum açık kalmış olabilir."
        )

    log(job, "[+] Oturum başarıyla açıldı.")
    cookies = await context.cookies()

    temp_file = COOKIE_FILE.with_name(f"{COOKIE_FILE.name}.{job.id}.tmp")
    try:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(temp_file),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cookies, handle, indent=2, ensure_ascii=False)
        os.replace(temp_file, COOKIE_FILE)
        log(job, "[+] Çerezler yerel dosyaya kaydedildi.")
    except Exception:
        log(job, "[!] Oturum açıldı ancak çerez dosyası kaydedilemedi.")
    finally:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass

    log(job, f"[+] E-posta: {email}")
    log(job, f"[+] Toplam çerez: {len(cookies)}")


async def browser_job(job):
    async with async_playwright() as playwright:
        context = None
        page = None
        screenshot_task = None
        capture_lock = asyncio.Lock()

        try:
            log(job, "[*] Tarayıcı başlatılıyor (Headless: False)...")

            # İlk kodundaki tarayıcı argümanları ve ekran boyutu.
            options = {
                "user_data_dir": PROFILE_DIR,
                "headless": False,
                "viewport": {"width": 1920, "height": 1080},
                "timeout": 60000,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1920,1080",
                    "--start-maximized",
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
            screenshot_task = asyncio.create_task(
                screenshot_loop(page, job, capture_lock)
            )
            await signup_steps(page, context, job, capture_lock)

        finally:
            if screenshot_task is not None:
                screenshot_task.cancel()
                try:
                    await screenshot_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.warning("Görüntü görevi kapatılırken hata.")

            if page is not None:
                await capture(page, job, capture_lock)

            if context is not None:
                try:
                    await asyncio.wait_for(context.close(), timeout=15)
                except Exception:
                    logger.warning("Tarayıcı kapatılırken hata.")


async def timed_job(job):
    await asyncio.wait_for(browser_job(job), timeout=360)


def safe_error(exc):
    # Hatanın ayrıntısı görünür; oturum bağlantıları maskelenir.
    text = str(exc)
    text = re.sub(
        r"https://viw\.ai/api/auth/magic-link/verify[^\s\"'<>]*",
        "[GIZLI_DOGRULAMA_BAGLANTISI]",
        text,
    )
    text = re.sub(
        r"(?i)(token=)[^\s&\"'<>]+",
        r"\1[GIZLI]",
        text,
    )
    return text[:5000]


def run_job(job):
    success = False
    try:
        asyncio.run(timed_job(job))
        success = True
        log(job, "[BITTI] Hesap ve oturum doğrulandı.")
    except Exception as exc:
        message = safe_error(exc) or "İşlem zaman aşımına uğradı."
        log(job, f"[HATA] {type(exc).__name__}: {message}")
    finally:
        with job.lock:
            job.success = success
            job.sequence += 1
            job.events.append((
                job.sequence,
                {"kind": "done", "success": success},
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
main { max-width: 1500px; margin: auto; }
h1 { color: #38bdf8; font-size: 25px; }
p { color: #94a3b8; }
.buttons { display: flex; gap: 12px; flex-wrap: wrap; margin: 20px 0; }
button {
    padding: 14px 20px; border: 1px solid #475569; border-radius: 10px;
    background: #334155; color: white; cursor: pointer; font-size: 15px;
}
#start { background: #2563eb; }
button:disabled { opacity: .5; cursor: not-allowed; }
.grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr);
    gap: 16px;
}
.panel {
    min-width: 0; background: #020617; border: 1px solid #334155;
    border-radius: 12px; overflow: hidden;
}
.header {
    padding: 12px; background: #1e293b; display: flex;
    justify-content: space-between; gap: 12px; font-size: 13px;
}
#terminal {
    height: 480px; overflow: auto; margin: 0; padding: 14px;
    white-space: pre-wrap; overflow-wrap: anywhere;
    color: #7dd3fc; font: 13px/1.6 monospace;
}
.preview { padding: 8px; }
#screen { display: block; width: 100%; height: auto; }
#imageStatus { padding: 12px; font-size: 12px; color: #94a3b8; }
@media(max-width: 900px) {
    body { padding: 12px; }
    .grid { grid-template-columns: minmax(0, 1fr); }
    #terminal { height: 280px; }
}
</style>
</head>
<body>
<main>
<h1>Viw.AI Otomatik Hesap Açıcı</h1>
<p>Canlı işlem logları ve tarayıcı ekran görüntüsü.</p>
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
let source = null;
let pollTimer = null;
let currentURL = null;
let loadingImage = false;
let reloadImage = false;
let lastStamp = null;

function appendLog(text) {
    terminal.textContent += text + "\n";
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
    if (loadingImage) {
        reloadImage = true;
        return;
    }

    loadingImage = true;
    const id = jobId;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    let temporaryURL = null;

    try {
        const response = await fetch(
            `/screenshot?job=${encodeURIComponent(id)}&t=${Date.now()}`,
            {cache: "no-store", signal: controller.signal}
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const type = response.headers.get("Content-Type") || "";
        if (!type.startsWith("image/")) {
            throw new Error("Sunucu görüntü döndürmedi.");
        }

        const stamp = response.headers.get("X-Screenshot-Time");
        const blob = await response.blob();
        if (id !== jobId || (stamp && stamp === lastStamp)) return;

        temporaryURL = URL.createObjectURL(blob);
        const preview = new Image();
        preview.src = temporaryURL;
        await preview.decode();
        if (id !== jobId) return;

        const oldURL = currentURL;
        currentURL = temporaryURL;
        temporaryURL = null;
        screenImage.src = currentURL;
        lastStamp = stamp;
        if (oldURL) URL.revokeObjectURL(oldURL);

        imageTime.textContent = stamp
            ? new Date(Number(stamp) * 1000).toLocaleTimeString("tr-TR")
            : "Henüz görüntü yok";
        imageStatus.textContent = stamp
            ? "Son başarılı ekran görüntüsü."
            : "Tarayıcı görüntüsü bekleniyor.";
    } catch (error) {
        if (id === jobId) {
            imageStatus.textContent =
                "Önceki görüntü korunuyor. " + error.message;
        }
    } finally {
        clearTimeout(timer);
        if (temporaryURL) URL.revokeObjectURL(temporaryURL);
        loadingImage = false;
        if (reloadImage) {
            reloadImage = false;
            void loadScreenshot();
        }
    }
}

function stopWatching() {
    if (source) source.close();
    if (pollTimer) clearInterval(pollTimer);
    source = null;
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
    lastStamp = null;
    terminal.textContent = "";
    startButton.disabled = true;
    statusBox.textContent = "Çalışıyor";
    void loadScreenshot();
    pollTimer = setInterval(loadScreenshot, 5000);

    const connection = new EventSource(`/events/${encodeURIComponent(id)}`);
    source = connection;

    connection.onopen = () => {
        if (source === connection) statusBox.textContent = "Bağlı";
    };

    connection.onmessage = event => {
        if (source !== connection) return;
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

    connection.onerror = async () => {
        if (source !== connection) return;
        statusBox.textContent = "Yeniden bağlanıyor";
        try {
            const state = await api("/status");
            if (source !== connection) return;
            if (state.job_id !== id) {
                stopWatching();
                startButton.disabled = false;
                statusBox.textContent = "İşlem bulunamadı";
                appendLog("[!] Sunucu yeniden başlamış veya işlem değişmiş olabilir.");
            }
            // Aynı işlem varsa EventSource Last-Event-ID ile tekrar bağlanır.
        } catch {
            // Geçici bağlantı hatasında otomatik yeniden deneme sürer.
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
            ? "Yeni ekran görüntüsü istendi."
            : "İşlem bitti. Son görüntü gösteriliyor.";
    } catch (error) {
        imageStatus.textContent = error.message;
    } finally {
        refreshButton.disabled = false;
    }
});

async function restore() {
    startButton.disabled = true;
    try {
        const data = await api("/status");
        if (data.job_id) {
            watchJob(data.job_id);
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
    if (currentURL) URL.revokeObjectURL(currentURL);
});

void restore();
</script>
</body>
</html>
"""


@app.get("/")
def home():
    return Response(HOME_HTML, content_type="text/html; charset=utf-8")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        threaded=True,
        debug=False,
    )
