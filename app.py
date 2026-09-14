import os
import re
import sys
import json
import time
import random
import string
import logging
import requests
from flask import Flask, Response, stream_with_context
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)
app.json.ensure_ascii = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE = "https://viw.ai"
SPAMOK_API = "https://api.spamok.com/v2"
PROFILE_DIR = "/tmp/chrome_profile" if os.name != "nt" else os.path.abspath("./chrome_profile")


def generate_random_prefix(length: int = 12) -> str:
    return "".join(random.SystemRandom().choice(string.ascii_lowercase + string.digits) for _ in range(length))


def wait_for_magic_link_stream(local: str, timeout: int = 100):
    deadline = time.time() + timeout
    seen_ids = set()
    yield f"data: [*] '{local}@spamok.com' gelen kutusu kontrol ediliyor...\n\n"

    while time.time() < deadline:
        try:
            r = requests.get(f"{SPAMOK_API}/EmailBox/{local}", timeout=15)
            if r.ok:
                mails = r.json().get("mails", [])
                for m in mails:
                    if "Viw AI" not in m.get("subject", ""):
                        continue
                    mid = m["id"]
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    d = requests.get(f"{SPAMOK_API}/Email/{local}/{mid}", timeout=15).json()
                    text = d.get("messagePlain", "") + "\n" + d.get("messageHtml", "")
                    match = re.search(
                        r"https://viw\.ai/api/auth/magic-link/verify\?token=[^\s\"'<>&]+(?:&amp;|&)callbackURL=[^\s\"'<>]+",
                        text,
                    )
                    if match:
                        yield f"data: [+] Doğrulama e-postası yakalandı!\n\n"
                        return match.group(0).replace("&amp;", "&")
        except Exception as err:
            yield f"data: [!] Mail kontrol uyarısı: {err}\n\n"
        time.sleep(3)

    raise TimeoutError("Magic link e-postası zaman aşımına uğradı.")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/")
def home():
    return r"""
    <!doctype html>
    <html lang="tr">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Viw.AI Hesap Oluşturucu</title>
        <style>
            * { box-sizing: border-box; }
            body {
                max-width: 800px;
                margin: 40px auto;
                padding: 20px;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: #0f172a;
                color: #f8fafc;
            }
            .card {
                background: #1e293b;
                padding: 24px;
                border-radius: 16px;
                border: 1px solid #334155;
                box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5);
            }
            h1 {
                margin-top: 0;
                font-size: 24px;
                color: #38bdf8;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            p { color: #94a3b8; font-size: 14px; margin-bottom: 20px; }
            button {
                width: 100%;
                padding: 16px;
                font-size: 16px;
                font-weight: 600;
                border-radius: 10px;
                border: none;
                background: linear-gradient(135deg, #2563eb, #0284c7);
                color: white;
                cursor: pointer;
                transition: all 0.2s ease;
            }
            button:hover {
                opacity: 0.95;
                transform: translateY(-1px);
            }
            button:disabled {
                opacity: 0.5;
                cursor: not-allowed;
                transform: none;
            }
            .console-box {
                margin-top: 24px;
                background: #020617;
                border: 1px solid #1e293b;
                border-radius: 10px;
                padding: 16px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 13px;
                line-height: 1.6;
                color: #38bdf8;
                height: 380px;
                overflow-y: auto;
                white-space: pre-wrap;
                word-break: break-all;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <h1><span>⚡</span> Viw.AI Otomatik Hesap Açıcı</h1>
            <p>Butona bastığınızda Playwright ve SpamOk arka planda çalışarak yeni bir hesap açacak ve canlı logları aşağıya aktaracaktır.</p>

            <button id="btnStart" onclick="startSignup()">🚀 Hesap Aç</button>

            <div class="console-box" id="terminal">Sistem hazır. "Hesap Aç" butonuna basınız...</div>
        </div>

        <script>
            function startSignup() {
                const btn = document.getElementById('btnStart');
                const term = document.getElementById('terminal');
                btn.disabled = true;
                btn.innerText = "⏳ Hesap Oluşturuluyor...";
                term.textContent = "[*] İşlem başlatıldı...\n";

                const es = new EventSource('/stream-signup');

                es.onmessage = function(e) {
                    term.textContent += e.data + "\n";
                    term.scrollTop = term.scrollHeight;

                    if (e.data.indexOf("[BITTI]") !== -1 || e.data.indexOf("[HATA]") !== -1) {
                        es.close();
                        btn.disabled = false;
                        btn.innerText = "🚀 Tekrar Hesap Aç";
                    }
                };

                es.onerror = function() {
                    term.textContent += "\n[!] Akış tamamlandı veya bağlantı kapandı.\n";
                    es.close();
                    btn.disabled = false;
                    btn.innerText = "🚀 Hesap Aç";
                };
            }
        </script>
    </body>
    </html>
    """


@app.get("/stream-signup")
def stream_signup():
    def generate():
        local_prefix = generate_random_prefix(12)
        test_email = f"{local_prefix}@spamok.com"

        yield f"data: ============================================================\n\n"
        yield f"data:  Viw AI - Canlı Hesap Oluşturma Akışı\n\n"
        yield f"data:  Profil Dizini: {PROFILE_DIR}\n\n"
        yield f"data: ============================================================\n\n"
        yield f"data: [1] Üretilen E-posta: {test_email}\n\n"

        try:
            with sync_playwright() as p:
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1280,800",
                ]

                # Persistent context ile başlat
                context = p.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    headless=True,
                    args=launch_args,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800},
                )

                page = context.pages[0] if context.pages else context.new_page()

                yield f"data: [2] https://viw.ai/ açılıyor...\n\n"
                page.goto("https://viw.ai/", wait_until="networkidle", timeout=60000)

                yield f"data: [3] Giriş modalı tetikleniyor...\n\n"
                login_btn = page.locator("a:has-text('Login'), button:has-text('Login')").first
                if login_btn.count() > 0 and login_btn.is_visible():
                    login_btn.click()
                    page.wait_for_timeout(1500)

                yield f"data: [4] 'Continue with Email' seçeneği tıklanıyor...\n\n"
                email_btn = page.locator("button:has-text('Continue with Email'), span:has-text('Continue with Email')").first
                if email_btn.count() > 0:
                    email_btn.click()
                    page.wait_for_timeout(1500)

                yield f"data: [5] E-posta yazılıyor: {test_email}\n\n"
                email_input = page.locator("input#email, input[type='email']").first
                email_input.fill(test_email)
                page.wait_for_timeout(1500)

                yield f"data: [6] Turnstile token kontrol ediliyor...\n\n"
                turnstile_token = None
                for i in range(25):
                    token = page.evaluate("""() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el ? el.value : '';
                    }""")
                    if token:
                        turnstile_token = token
                        yield f"data:     [+] Turnstile token hazır! (Uzunluk: {len(token)})\n\n"
                        break

                    # Frame içi checkbox tıklama denemesi
                    try:
                        ts_frame = page.frame_locator("iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']").first
                        cb = ts_frame.locator("input[type='checkbox'], label, .ctp-checkbox-label, #challenge-stage").first
                        if cb.count() > 0 and cb.is_visible():
                            cb.click(force=True, timeout=2000)
                    except Exception:
                        pass

                    page.wait_for_timeout(1000)

                yield f"data: [7] Form gönderiliyor...\n\n"
                submit_btn = page.locator("button[type='submit']")
                if submit_btn.count() > 0:
                    submit_btn.click()
                    page.wait_for_timeout(4000)

                # SpamOk e-posta dinleme
                magic_link = None
                for msg in wait_for_magic_link_stream(local_prefix, timeout=90):
                    yield msg
                    if "http" in msg:
                        magic_link = msg.split()[-1]

                if not magic_link:
                    # Alternatif doğrudan yakalama
                    magic_link = wait_for_magic_link_direct(local_prefix)

                yield f"data: [8] Doğrulama linki tarayıcıda açılıyor: {magic_link}\n\n"
                page.goto(magic_link, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(3000)

                session_data = page.evaluate("""async () => {
                    try {
                        const r = await fetch('/api/auth/get-session');
                        if (r.ok) return await r.json();
                    } catch(e) {}
                    return null;
                }""")

                user_id = session_data.get("user", {}).get("id") if session_data else "Bilinmiyor"
                cookies = context.cookies()

                yield f"data: \n\n"
                yield f"data: ============================================================\n\n"
                yield f"data: [🎉] HESAP BAŞARIYLA OLUŞTURULDU & GİRİŞ YAPILDI!\n\n"
                yield f"data: E-POSTA : {test_email}\n\n"
                yield f"data: KULLANICI ID : {user_id}\n\n"
                yield f"data: TOPLAM ÇEREZ SAYISI : {len(cookies)}\n\n"
                yield f"data: ============================================================\n\n"
                yield f"data: [BITTI]\n\n"

                context.close()

        except Exception as err:
            yield f"data: [HATA] Bir hata oluştu: {str(err)}\n\n"
            yield f"data: [BITTI]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def wait_for_magic_link_direct(local: str, timeout: int = 60) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{SPAMOK_API}/EmailBox/{local}", timeout=15)
        if r.ok:
            mails = r.json().get("mails", [])
            for m in mails:
                if "Viw AI" in m.get("subject", ""):
                    d = requests.get(f"{SPAMOK_API}/Email/{local}/{m['id']}", timeout=15).json()
                    text = d.get("messagePlain", "") + "\n" + d.get("messageHtml", "")
                    match = re.search(r"https://viw\.ai/api/auth/magic-link/verify\?token=[^\s\"'<>&]+(?:&amp;|&)callbackURL=[^\s\"'<>]+", text)
                    if match:
                        return match.group(0).replace("&amp;", "&")
        time.sleep(2)
    raise TimeoutError("Magic link alınamadı.")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
