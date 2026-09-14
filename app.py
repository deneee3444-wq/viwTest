import asyncio
import html
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque

import requests
from flask import Flask, Response, jsonify, request
from playwright.async_api import async_playwright


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("viw-panel")

app = Flask(__name__)
app.json.ensure_ascii = False

BASE = "https://viw.ai"
MAIL_API = "https://api.spamok.com/v2"

LOCK = threading.Lock()
CURRENT = None

EMAIL = "input[type='email']:visible, input#email:visible"

CONTINUE = (
    "button:has-text('Continue with Email'):visible, "
    "[role='button']:has-text('Continue with Email'):visible, "
    "a:has-text('Continue with Email'):visible, "
    "span:has-text('Continue with Email'):visible"
)

LOGIN = (
    "a:has-text('Login'):visible, "
    "button:has-text('Login'):visible, "
    "a:has-text('Log in'):visible, "
    "button:has-text('Log in'):visible, "
    "a:has-text('Sign in'):visible, "
    "button:has-text('Sign in'):visible"
)

PLACEHOLDER = """<svg xmlns="http://www.w3.org/2000/svg"
width="1280" height="720" viewBox="0 0 1280 720">
<rect width="1280" height="720" fill="#020617"/>
<text x="640" y="360" text-anchor="middle"
fill="#38bdf8" font-size="25" font-family="sans-serif">
Ekran görüntüsü bekleniyor...
</text>
</svg>"""


class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.logs = deque(maxlen=400)
        self.sequence = 0
        self.running = True
        self.success = False
        self.image = None
        self.version = 0
        self.captured_at = 0
        self.refresh = threading.Event()


def log(job, message):
    # Doğrulama bağlantısındaki gizli token'ı loglama.
    message = re.sub(
        r"https://viw\.ai/api/auth/magic-link/verify[^\s]*",
        "[doğrulama bağlantısı gizlendi]",
        str(message),
    )
    with LOCK:
        job.sequence += 1
        job.logs.append({
            "id": job.sequence,
            "text": message[:2000],
        })


@app.before_request
def validate_post():
    # Panel şifresizdir. Bu kontrol kimlik doğrulaması değildir;
    # dış sitelerden sıradan form gönderimini engeller.
    if request.method == "POST":
        if request.headers.get("X-Requested-With") != "ViwPanel":
            return jsonify(error="Geçersiz istek."), 403


@app.after_request
def set_headers(response):
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


@app.get("/status")
def status():
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        after = 0

    with LOCK:
        job = CURRENT
        if job is None:
            return jsonify(job=None)

        if request.args.get("job", "") != job.id:
            after = 0

        payload = {
            "id": job.id,
            "running": job.running,
            "success": job.success,
            "version": job.version,
            "captured_at": job.captured_at,
            "logs": [item for item in job.logs if item["id"] > after],
        }

    return jsonify(job=payload)


@app.get("/screenshot")
def screenshot():
    with LOCK:
        job = CURRENT
        image = (
            job.image
            if job and request.args.get("job") == job.id
            else None
        )

    if image is None:
        return Response(PLACEHOLDER, mimetype="image/svg+xml")

    return Response(image, mimetype="image/jpeg")


@app.post("/refresh-screenshot")
def refresh_screenshot():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Geçersiz JSON."), 400

    with LOCK:
        job = CURRENT
        if job is None or data.get("job_id") != job.id:
            return jsonify(error="İşlem bulunamadı."), 404

        running = job.running
        if running:
            job.refresh.set()

    return jsonify(ok=True, running=running)


@app.post("/start")
def start():
    global CURRENT

    with LOCK:
        if CURRENT and CURRENT.running:
            return jsonify(error="Zaten çalışan bir işlem var."), 409

        job = Job()
        CURRENT = job

    try:
        threading.Thread(
            target=worker,
            args=(job,),
            daemon=True,
            name=f"browser-{job.id[:8]}",
        ).start()
    except Exception:
        log(job, "[HATA] İşlem başlatılamadı.")
        with LOCK:
            job.running = False
        return jsonify(error="İşlem başlatılamadı."), 500

    return jsonify(job_id=job.id), 202


def check_mail(prefix, seen, deadline):
    """Önceki kodda kullanılan SpamOk yanıt biçimini kullanır."""
    if time.monotonic() >= deadline:
        return None

    response = requests.get(
        f"{MAIL_API}/EmailBox/{prefix}",
        timeout=(5, 10),
    )
    response.raise_for_status()
    inbox = response.json()

    if not isinstance(inbox, dict):
        raise ValueError("Beklenmeyen gelen kutusu yanıtı.")

    for item in inbox.get("mails", []):
        if time.monotonic() >= deadline:
            return None
        if not isinstance(item, dict):
            continue

        mid = item.get("id")
        if mid is None or str(mid) in seen:
            continue
        if "viw" not in str(item.get("subject", "")).lower():
            continue

        response = requests.get(
            f"{MAIL_API}/Email/{prefix}/{mid}",
            timeout=(5, 10),
        )
        response.raise_for_status()
        detail = response.json()

        if not isinstance(detail, dict):
            continue

        text = html.unescape(
            str(detail.get("messagePlain") or "")
            + "\n"
            + str(detail.get("messageHtml") or "")
        )

        match = re.search(
            r"""https://viw\.ai/api/auth/magic-link/verify\?[^ \t\r\n"'<>]+""",
            text,
        )
        if match and "token=" in match.group(0):
            return match.group(0)

        seen.add(str(mid))

    return None


async def has_visible(page, selector):
    if page is None or page.is_closed():
        return False
    try:
        return await page.locator(selector).count() > 0
    except Exception:
        return False


async def find_page(context, selector, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for candidate in reversed(context.pages):
            if await has_visible(candidate, selector):
                return candidate
        await asyncio.sleep(0.4)
    return None


async def signup(job):
    prefix = secrets.token_hex(6)
    email = f"{prefix}@spamok.com"

    log(job, "[*] Tarayıcı başlatılıyor (Headless: True)...")
    log(job, f"[*] Üretilen e-posta: {email}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        active = {"page": None}
        capture_lock = asyncio.Lock()
        image_task = None

        async def capture():
            async with capture_lock:
                page = active["page"]
                if page is None or page.is_closed():
                    return

                try:
                    image = await page.screenshot(
                        type="jpeg",
                        quality=65,
                        full_page=False,
                        timeout=5000,
                    )
                    with LOCK:
                        job.image = image
                        job.version += 1
                        job.captured_at = time.time()
                except Exception:
                    logger.debug("Ekran görüntüsü alınamadı.")

        async def capture_loop():
            next_capture = 0
            while True:
                if (
                    time.monotonic() >= next_capture
                    or job.refresh.is_set()
                ):
                    job.refresh.clear()
                    await capture()
                    next_capture = time.monotonic() + 3
                await asyncio.sleep(0.25)

        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                device_scale_factor=1,
            )
            context.set_default_timeout(20000)

            page = await context.new_page()
            active["page"] = page
            context.on(
                "page",
                lambda new_page: active.update(page=new_page),
            )
            image_task = asyncio.create_task(capture_loop())

            log(job, "[1] https://viw.ai/ açılıyor...")
            await page.goto(
                BASE,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await capture()

            log(job, "[2] Giriş ekranı kontrol ediliyor...")
            auth_selector = f"{EMAIL}, {CONTINUE}"
            auth_page = await find_page(context, auth_selector, 2)

            if auth_page is None:
                log(job, "[*] Login düğmesine tıklanıyor...")
                try:
                    await page.locator(LOGIN).first.click(
                        timeout=20000
                    )
                except Exception as exc:
                    log(
                        job,
                        "[!] Login tıklaması tamamlanamadı: "
                        f"{type(exc).__name__}. "
                        "Açılan giriş içeriği kontrol ediliyor.",
                    )

                # URL değişimini değil, modal veya popup içeriğini bekle.
                auth_page = await find_page(
                    context, auth_selector, 15
                )

            if auth_page is None:
                log(job, "[*] Yedek giriş adresi deneniyor...")
                active["page"] = page
                await page.goto(
                    f"{BASE}/login",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                auth_page = await find_page(
                    context, auth_selector, 20
                )

            if auth_page is None:
                raise RuntimeError(
                    "Giriş formu bulunamadı. "
                    "Son ekran görüntüsünü kontrol edin."
                )

            page = auth_page
            active["page"] = page
            await capture()

            if not await has_visible(page, EMAIL):
                log(job, "[3] Continue with Email tıklanıyor...")
                await page.locator(CONTINUE).first.click(
                    timeout=20000
                )
            else:
                log(job, "[3] E-posta formu zaten açık.")

            email_page = await find_page(context, EMAIL, 25)
            if email_page is None:
                raise RuntimeError("E-posta alanı bulunamadı.")

            page = email_page
            active["page"] = page
            email_input = page.locator(EMAIL).first

            log(job, "[4] E-posta yazılıyor...")
            await email_input.fill(email)
            await capture()
            await asyncio.sleep(2)

            log(job, "[5] Doğrulama durumu kontrol ediliyor...")
            challenge = (
                "input[name='cf-turnstile-response'], "
                ".cf-turnstile, "
                "iframe[src*='challenges.cloudflare.com']"
            )

            if await page.locator(challenge).count():
                log(job, "[*] Turnstile doğrulaması bekleniyor...")
                deadline = time.monotonic() + 45

                while time.monotonic() < deadline:
                    ready = await page.evaluate("""
                        () => Array.from(document.querySelectorAll(
                            'input[name="cf-turnstile-response"]'
                        )).some(element => Boolean(element.value))
                    """)
                    if ready:
                        log(job, "[+] Doğrulama tamamlandı.")
                        break
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError(
                        "Turnstile doğrulaması tamamlanmadı. "
                        "Panel doğrulamayı aşmaz; ekran görüntüsü "
                        "üzerinden manuel tıklama yapılamaz."
                    )

            log(job, "[6] Form gönderiliyor...")
            form = email_input.locator("xpath=ancestor::form[1]")
            submit = form.locator(
                "button[type='submit']:visible, "
                "input[type='submit']:visible"
            )

            if await submit.count() == 0:
                submit = page.locator(
                    "button[type='submit']:visible, "
                    "input[type='submit']:visible"
                )

            if await submit.count():
                await submit.first.click(timeout=20000)
            else:
                await email_input.press("Enter")

            await asyncio.sleep(2)
            await capture()

            log(job, "[7] Doğrulama e-postası bekleniyor...")
            deadline = time.monotonic() + 90
            seen = set()
            magic_link = None

            while time.monotonic() < deadline:
                try:
                    magic_link = await asyncio.to_thread(
                        check_mail, prefix, seen, deadline
                    )
                    if magic_link:
                        break
                except Exception as exc:
                    log(
                        job,
                        "[!] Mail kontrolü başarısız: "
                        f"{type(exc).__name__}",
                    )
                await asyncio.sleep(3)

            if not magic_link:
                raise RuntimeError(
                    "Doğrulama e-postası alınamadı. "
                    "Form reddedilmiş, e-posta gecikmiş veya "
                    "posta servisi yanıtı değişmiş olabilir."
                )

            log(job, "[8] Doğrulama bağlantısı açılıyor...")
            await page.goto(
                magic_link,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await capture()

            log(job, "[9] Oturum doğrulanıyor...")
            session = None
            deadline = time.monotonic() + 25

            while time.monotonic() < deadline:
                try:
                    session = await page.evaluate("""
                        async () => {
                            if (location.origin !== "https://viw.ai") {
                                return null;
                            }
                            try {
                                const response = await fetch(
                                    "/api/auth/get-session",
                                    {
                                        credentials: "include",
                                        cache: "no-store",
                                        signal: AbortSignal.timeout(8000)
                                    }
                                );
                                return response.ok
                                    ? await response.json()
                                    : null;
                            } catch (_) {
                                return null;
                            }
                        }
                    """)
                except Exception:
                    session = None

                if (
                    isinstance(session, dict)
                    and isinstance(session.get("user"), dict)
                    and session["user"].get("id")
                ):
                    break

                await asyncio.sleep(1)
            else:
                raise RuntimeError(
                    "Doğrulama bağlantısı açıldı ancak "
                    "oturum doğrulanamadı."
                )

            await capture()
            log(job, "[+] BAŞARILI: Oturum doğrulandı.")
            log(
                job,
                "E-posta: "
                + str(session["user"].get("email") or email),
            )

            with LOCK:
                job.success = True

        except (Exception, asyncio.CancelledError):
            await capture()
            raise

        finally:
            if image_task is not None:
                image_task.cancel()
                try:
                    await image_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Görüntü görevi hatası")

            try:
                await asyncio.wait_for(browser.close(), timeout=15)
            except Exception:
                logger.exception("Tarayıcı kapatma hatası")


async def run_job(job):
    async with asyncio.timeout(360):
        await signup(job)


def worker(job):
    try:
        asyncio.run(run_job(job))
    except TimeoutError:
        log(job, "[HATA] İşlemin toplam bekleme süresi aşıldı.")
    except Exception as exc:
        log(job, f"[HATA] {type(exc).__name__}: {exc}")
    finally:
        log(job, "[BITTI]")
        with LOCK:
            job.running = False


@app.get("/")
def home():
    return HTML


HTML = r"""
<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Viw.ai Panel</title>
<style>
* { box-sizing: border-box; }
body {
    margin: 0;
    padding: 22px;
    background: #0b1120;
    color: #e2e8f0;
    font-family: system-ui, sans-serif;
}
main { max-width: 1250px; margin: auto; }
h1 { color: #38bdf8; font-size: 24px; }
p { color: #94a3b8; line-height: 1.5; }
.controls {
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
    margin: 20px 0;
}
button {
    padding: 13px 20px;
    border: 0;
    border-radius: 9px;
    background: #2563eb;
    color: white;
    font: inherit;
    cursor: pointer;
}
button.secondary { background: #334155; }
button:disabled { opacity: .5; cursor: not-allowed; }
.grid {
    display: grid;
    grid-template-columns: 1fr 1.2fr;
    gap: 16px;
}
.panel {
    min-width: 0;
    background: #020617;
    border: 1px solid #334155;
    border-radius: 12px;
    overflow: hidden;
}
.title {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    padding: 12px;
    background: #0f172a;
    font-size: 13px;
}
pre {
    height: 460px;
    margin: 0;
    padding: 14px;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: #38bdf8;
    font: 12px/1.65 ui-monospace, monospace;
}
.screen {
    height: 460px;
    padding: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
}
img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
}
#notice {
    min-height: 28px;
    color: #fbbf24;
    overflow-wrap: anywhere;
}
@media (max-width: 850px) {
    body { padding: 12px; }
    .grid { grid-template-columns: 1fr; }
    pre { height: 300px; }
    .screen { height: auto; min-height: 220px; }
}
</style>
</head>
<body>
<main>
<h1>Viw.ai İşlem Paneli</h1>
<p>
Tarayıcı görüntüsü işlem sırasında otomatik yenilenir.
Görüntü etkileşimli değildir.
</p>

<div class="controls">
    <button id="start" disabled>Hesap Aç</button>
    <button id="refresh" class="secondary">Ekranı Yenile</button>
    <span id="status">Bağlanıyor...</span>
</div>

<div id="notice"></div>

<div class="grid">
    <section class="panel">
        <div class="title">İşlem kayıtları</div>
        <pre id="logs">Sistem durumu alınıyor...</pre>
    </section>

    <section class="panel">
        <div class="title">
            <span>Tarayıcı ekranı</span>
            <span id="time">Görüntü bekleniyor</span>
        </div>
        <div class="screen">
            <img id="image" src="/screenshot"
                 alt="Son tarayıcı ekran görüntüsü">
        </div>
    </section>
</div>
</main>

<script>
const startButton = document.getElementById("start");
const refreshButton = document.getElementById("refresh");
const statusText = document.getElementById("status");
const notice = document.getElementById("notice");
const logs = document.getElementById("logs");
const image = document.getElementById("image");
const timeLabel = document.getElementById("time");

let jobId = "";
let after = 0;
let shownVersion = 0;
let starting = false;
let imageBusy = false;
let imageUrl = null;

async function api(url, method = "GET", body = {}) {
    const options = {
        method,
        cache: "no-store",
        credentials: "same-origin",
        headers: {},
        signal: AbortSignal.timeout(15000)
    };

    if (method === "POST") {
        options.headers["Content-Type"] = "application/json";
        options.headers["X-Requested-With"] = "ViwPanel";
        options.body = JSON.stringify(body);
    }

    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(data.error || `HTTP ${response.status}`);
    }

    return data;
}

function resetImage() {
    image.src = "/screenshot";
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
    shownVersion = 0;
    timeLabel.textContent = "Görüntü bekleniyor";
}

async function loadImage(job) {
    if (imageBusy || !job.version || job.version <= shownVersion) {
        return;
    }

    imageBusy = true;
    let newUrl = null;

    try {
        const response = await fetch(
            "/screenshot?job=" + encodeURIComponent(job.id)
            + "&v=" + job.version,
            {
                cache: "no-store",
                signal: AbortSignal.timeout(12000)
            }
        );

        if (!response.ok) {
            throw new Error("Görüntü alınamadı.");
        }

        const blob = await response.blob();
        if (!blob.type.startsWith("image/jpeg")) return;

        newUrl = URL.createObjectURL(blob);
        const preview = new Image();
        preview.src = newUrl;
        await preview.decode();

        if (jobId !== job.id) return;

        const oldUrl = imageUrl;
        imageUrl = newUrl;
        image.src = imageUrl;
        newUrl = null;

        if (oldUrl) URL.revokeObjectURL(oldUrl);

        shownVersion = job.version;
        timeLabel.textContent = new Date(
            job.captured_at * 1000
        ).toLocaleTimeString();
    } catch (error) {
        notice.textContent =
            "Görüntü yüklenemedi; otomatik tekrar denenecek.";
    } finally {
        if (newUrl) URL.revokeObjectURL(newUrl);
        imageBusy = false;
    }
}

async function poll() {
    try {
        const data = await api(
            "/status?job=" + encodeURIComponent(jobId)
            + "&after=" + after
        );

        const job = data.job;

        if (!job) {
            if (jobId) resetImage();
            jobId = "";
            after = 0;
            startButton.disabled = starting;
            startButton.textContent = "Hesap Aç";
            statusText.textContent = "Hazır";
            logs.textContent = "Başlamak için Hesap Aç düğmesine basın.";
            return;
        }

        if (job.id !== jobId) {
            jobId = job.id;
            after = 0;
            logs.textContent = "";
            resetImage();
        }

        for (const entry of job.logs) {
            if (entry.id > after) {
                logs.textContent += entry.text + "\n";
                after = entry.id;
            }
        }

        if (job.logs.length) {
            logs.scrollTop = logs.scrollHeight;
        }

        startButton.disabled = starting || job.running;
        startButton.textContent = job.running
            ? "İşlem sürüyor..."
            : "Hesap Aç";

        statusText.textContent = job.running
            ? "Çalışıyor"
            : (job.success ? "Tamamlandı" : "Hata ile sonlandı");

        void loadImage(job);
    } catch (error) {
        startButton.disabled = true;
        statusText.textContent = "Bağlantı bekleniyor";
        notice.textContent = error.message;
    }
}

startButton.addEventListener("click", async () => {
    starting = true;
    startButton.disabled = true;
    notice.textContent = "";

    try {
        await api("/start", "POST");
        statusText.textContent = "İşlem başlatıldı";
    } catch (error) {
        notice.textContent = error.message;
    } finally {
        starting = false;
    }
});

refreshButton.addEventListener("click", async () => {
    if (!jobId) {
        notice.textContent = "Önce bir işlem başlatın.";
        return;
    }

    refreshButton.disabled = true;

    try {
        const result = await api(
            "/refresh-screenshot",
            "POST",
            {job_id: jobId}
        );

        shownVersion = 0;
        notice.textContent = result.running
            ? "Yeni görüntü istendi; otomatik yüklenecek."
            : "Tarayıcı kapalı; son kaydedilen görüntü gösteriliyor.";
    } catch (error) {
        notice.textContent = error.message;
    } finally {
        refreshButton.disabled = false;
    }
});

async function loop() {
    await poll();
    setTimeout(loop, 1500);
}

loop();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        threaded=True,
        debug=False,
    )
