import os
import sys
import re
import json
import time
import random
import string
import subprocess
import requests
from flask import Flask, Response, stream_with_context
from playwright.sync_api import sync_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

app = Flask(__name__)
app.json.ensure_ascii = False

BASE = "https://viw.ai"
SPAMOK_API = "https://api.spamok.com/v2"
PROFILE_DIR = os.path.abspath("./chrome_profile") if os.name == "nt" else "/tmp/chrome_profile"
COOKIE_FILE = os.path.abspath("./session_cookies.json")
SCREENSHOT_FILE = os.path.abspath("./latest_screenshot.png") if os.name == "nt" else "/tmp/latest_screenshot.png"

PLACEHOLDER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450" viewBox="0 0 800 450">
  <rect width="100%" height="100%" fill="#020617"/>
  <rect x="20" y="20" width="760" height="410" rx="10" fill="#0f172a" stroke="#1e293b" stroke-width="2"/>
  <circle cx="400" cy="190" r="36" fill="#1e293b" stroke="#38bdf8" stroke-width="2"/>
  <path d="M388 190 L412 190 M400 178 L400 202" stroke="#38bdf8" stroke-width="3" stroke-linecap="round"/>
  <text x="400" y="260" fill="#94a3b8" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="16" font-weight="500" text-anchor="middle">Ekran görüntüsü henüz alınmadı</text>
  <text x="400" y="285" fill="#64748b" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="13" text-anchor="middle">İstediğiniz an "📸 Anlık SS Al" butonuna basabilirsiniz</text>
</svg>"""


def generate_random_prefix(length: int = 12) -> str:
    """Rastgele e-posta kullanıcı adı üretir."""
    return "".join(random.SystemRandom().choice(string.ascii_lowercase + string.digits) for _ in range(length))


def capture_screen_from_system():
    """Playwright'a dokunmadan doğrudan işletim sistemi/X11 üzerinden anlık ekran görüntüsü alır."""
    # Linux / Render ortamında Xvfb (:99) üzerinden scrot ile anında yakala
    if os.name != "nt":
        try:
            disp = os.environ.get("DISPLAY", ":99")
            res = subprocess.run(
                ["scrot", "-o", SCREENSHOT_FILE],
                env={**os.environ, "DISPLAY": disp},
                capture_output=True,
                timeout=4,
            )
            if res.returncode == 0 and os.path.exists(SCREENSHOT_FILE) and os.path.getsize(SCREENSHOT_FILE) > 0:
                return True
        except Exception:
            pass

    # Windows ortamında Pillow fallback
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab()
        im.save(SCREENSHOT_FILE)
        return True
    except Exception:
        pass

    return False


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/take-screenshot")
def take_screenshot_endpoint():
    """Kullanıcı butona bastığında X11/ekran üzerinden anlık SS alır."""
    capture_screen_from_system()
    for path in [SCREENSHOT_FILE, "/tmp/latest_screenshot.png", "./latest_screenshot.png"]:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                return Response(
                    data,
                    mimetype="image/png",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
                )
            except Exception:
                pass

    return Response(
        PLACEHOLDER_SVG,
        mimetype="image/svg+xml",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/screenshot")
def get_screenshot():
    for path in [SCREENSHOT_FILE, "/tmp/latest_screenshot.png", "./latest_screenshot.png"]:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                return Response(
                    data,
                    mimetype="image/png",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
                )
            except Exception:
                pass

    return Response(
        PLACEHOLDER_SVG,
        mimetype="image/svg+xml",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


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
                max-width: 1200px;
                margin: 30px auto;
                padding: 16px;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: #0b1120;
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
            p { color: #94a3b8; font-size: 14px; margin-bottom: 20px; line-height: 1.5; }
            .button-row {
                display: flex;
                gap: 12px;
                margin-bottom: 20px;
            }
            button {
                padding: 14px 22px;
                font-size: 15px;
                font-weight: 600;
                border-radius: 10px;
                border: none;
                cursor: pointer;
                transition: all 0.2s ease;
            }
            #btnStart {
                flex: 1;
                background: linear-gradient(135deg, #2563eb, #0284c7);
                color: white;
            }
            #btnStart:hover { opacity: 0.95; transform: translateY(-1px); }
            #btnStart:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
            #btnSS {
                background: linear-gradient(135deg, #059669, #10b981);
                color: #ffffff;
                display: flex;
                align-items: center;
                gap: 8px;
            }
            #btnSS:hover { opacity: 0.95; }
            #btnSS:disabled { opacity: 0.6; cursor: wait; }
            .main-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 16px;
            }
            @media (max-width: 860px) {
                .main-grid { grid-template-columns: 1fr; }
            }
            .panel {
                background: #020617;
                border: 1px solid #1e293b;
                border-radius: 10px;
                display: flex;
                flex-direction: column;
                height: 480px;
                overflow: hidden;
            }
            .panel-header {
                padding: 10px 14px;
                background: #0f172a;
                border-bottom: 1px solid #1e293b;
                font-size: 13px;
                font-weight: 600;
                color: #94a3b8;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .console-box {
                flex: 1;
                padding: 14px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 12.5px;
                line-height: 1.6;
                color: #38bdf8;
                overflow-y: auto;
                white-space: pre-wrap;
                word-break: break-all;
            }
            .ss-container {
                flex: 1;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 10px;
                background: #030712;
                position: relative;
                overflow: hidden;
            }
            #ssImage {
                max-width: 100%;
                max-height: 100%;
                object-fit: contain;
                border-radius: 6px;
                border: 1px solid #1f2937;
            }
            .badge {
                font-size: 11px;
                padding: 2px 6px;
                border-radius: 4px;
                background: #1e293b;
                color: #38bdf8;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <h1><span>⚡</span> Viw.AI Otomatik Hesap Açıcı</h1>
            <p>Hesap açma sürecini başlatabilir, sağdaki butonla istediğiniz an tarayıcının ekran görüntüsünü alabilirsiniz.</p>

            <div class="button-row">
                <button id="btnStart" onclick="startSignup()">🚀 Hesap Aç</button>
                <button id="btnSS" onclick="takeManualScreenshot()">📸 Anlık SS Al</button>
            </div>

            <div class="main-grid">
                <!-- Sol Panel: Konsol Logları -->
                <div class="panel">
                    <div class="panel-header">
                        <span>📟 Canlı Konsol Logları</span>
                        <span class="badge" id="statusBadge">Hazır</span>
                    </div>
                    <div class="console-box" id="terminal">Sistem hazır. "Hesap Aç" butonuna basınız...</div>
                </div>

                <!-- Sağ Panel: Canlı Ekran Görüntüsü -->
                <div class="panel">
                    <div class="panel-header">
                        <span>📸 Tarayıcı Ekranı</span>
                        <span id="ssTime" class="badge">Henüz alınmadı</span>
                    </div>
                    <div class="ss-container">
                        <img id="ssImage" src="/screenshot" alt="Tarayıcı Ekranı">
                    </div>
                </div>
            </div>
        </div>

        <script>
            async function takeManualScreenshot() {
                const btn = document.getElementById('btnSS');
                const img = document.getElementById('ssImage');
                const badge = document.getElementById('ssTime');

                btn.disabled = true;
                const origText = btn.innerText;
                btn.innerText = "⏳ SS Çekiliyor...";
                badge.innerText = "Çekiliyor...";

                try {
                    const response = await fetch('/take-screenshot?t=' + new Date().getTime());
                    if (response.ok) {
                        const blob = await response.blob();
                        img.src = URL.createObjectURL(blob);
                        badge.innerText = new Date().toLocaleTimeString();
                    } else {
                        badge.innerText = "Alınamadı";
                    }
                } catch (err) {
                    console.error("SS Alma Hatası:", err);
                    badge.innerText = "Hata";
                } finally {
                    btn.disabled = false;
                    btn.innerText = origText;
                }
            }

            function startSignup() {
                const btn = document.getElementById('btnStart');
                const term = document.getElementById('terminal');
                const badge = document.getElementById('statusBadge');

                btn.disabled = true;
                btn.innerText = "⏳ Hesap Oluşturuluyor...";
                badge.innerText = "Çalışıyor...";
                badge.style.color = "#f59e0b";
                term.textContent = "[*] İşlem başlatıldı...\n";

                let isFinished = false;
                const es = new EventSource('/stream-signup');

                es.onmessage = function(e) {
                    term.textContent += e.data + "\n";
                    term.scrollTop = term.scrollHeight;

                    if (e.data.indexOf("[BITTI]") !== -1 || e.data.indexOf("[HATA]") !== -1) {
                        isFinished = true;
                        es.close();
                        btn.disabled = false;
                        btn.innerText = "🚀 Tekrar Hesap Aç";
                        badge.innerText = e.data.indexOf("[HATA]") !== -1 ? "Hata" : "Tamamlandı";
                        badge.style.color = e.data.indexOf("[HATA]") !== -1 ? "#ef4444" : "#10b981";
                    }
                };

                es.onerror = function() {
                    if (!isFinished) {
                        term.textContent += "\n[!] Akış tamamlandı veya bağlantı kapandı.\n";
                        btn.disabled = false;
                        btn.innerText = "🚀 Hesap Aç";
                        badge.innerText = "Bağlantı Kapandı";
                        badge.style.color = "#94a3b8";
                    }
                    es.close();
                };
            }
        </script>
    </body>
    </html>
    """


@app.get("/stream-signup")
def stream_signup():
    def generate():
        yield f"data: ============================================================\n\n"
        yield f"data:  Viw AI - Tam Otomatik Kayıt & Oturum Alma\n\n"
        yield f"data:  Profil Dizini: {PROFILE_DIR}\n\n"
        yield f"data: ============================================================\n\n"

        # 1. Test E-postası oluştur
        local_prefix = generate_random_prefix(12)
        test_email = f"{local_prefix}@spamok.com"
        yield f"data: [*] Üretilen e-posta: {test_email}\n\n"

        context = None
        page = None
        try:
            with sync_playwright() as p:
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1920,1080",
                    "--start-maximized",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]

                try:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=PROFILE_DIR,
                        channel="chrome" if os.name == "nt" else None,
                        headless=False,
                        args=launch_args,
                        viewport={"width": 1920, "height": 1080},
                    )
                    yield f"data: [*] Chrome kalıcı profiliyle başlatıldı (Headless: False).\n\n"
                except Exception:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=PROFILE_DIR,
                        headless=False,
                        args=launch_args,
                        viewport={"width": 1920, "height": 1080},
                    )
                    yield f"data: [*] Chromium kalıcı profiliyle başlatıldı (Headless: False).\n\n"

                page = context.pages[0] if context.pages else context.new_page()

                # [1] https://viw.ai/ açılıyor (networkidle tam yüklenme garantiler - max 5 dk)
                yield f"data: [1] https://viw.ai/ açılıyor...\n\n"
                try:
                    page.goto("https://viw.ai/", wait_until="networkidle", timeout=300000)
                except Exception:
                    page.goto("https://viw.ai/", wait_until="domcontentloaded", timeout=300000)
                    page.wait_for_timeout(3000)

                yield f"data: [*] Sayfa yüklendi: '{page.title()}'\n\n"

                # [2] Giriş butonu aranıyor (Otomatik algılar, geldiği an tıklar - max 5 dk)
                yield f"data: [2] Giriş butonu aranıyor...\n\n"
                page.wait_for_selector("a:has-text('Login'), button:has-text('Login')", timeout=300000)
                login_btn = page.locator("a:has-text('Login'), button:has-text('Login')").first
                if login_btn.count() > 0 and login_btn.is_visible():
                    login_btn.click()
                    page.wait_for_timeout(2000)

                # [3] 'Continue with Email' seçeneği tıklanıyor (Otomatik algılar - max 5 dk)
                yield f"data: [3] 'Continue with Email' seçeneği tıklanıyor...\n\n"
                page.wait_for_selector("button:has-text('Continue with Email'), span:has-text('Continue with Email')", timeout=300000)
                email_btn = page.locator("button:has-text('Continue with Email'), span:has-text('Continue with Email')").first
                if email_btn.count() > 0:
                    email_btn.click()
                    page.wait_for_timeout(2000)

                # [4] E-posta Girişi (Otomatik algılar - max 5 dk)
                yield f"data: [4] E-posta yazılıyor: {test_email}\n\n"
                page.wait_for_selector("input#email, input[type='email']", timeout=300000)
                email_input = page.locator("input#email, input[type='email']").first
                email_input.fill(test_email)
                page.wait_for_timeout(1500)

                # [5] Turnstile Token ve Etkileşim Kontrolü
                yield f"data: [5] Turnstile doğrulaması kontrol ediliyor...\n\n"
                for i in range(35):
                    yield ": ping\n\n"
                    token = page.evaluate("""() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el ? el.value : '';
                    }""")
                    if token:
                        yield f"data:     [+] Turnstile token hazır! (Uzunluk: {len(token)})\n\n"
                        break

                    # Turnstile frame kontrolü ve tıklama
                    try:
                        ts_frame = page.frame_locator("iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']").first
                        cb = ts_frame.locator("input[type='checkbox'], label, .ctp-checkbox-label, #challenge-stage").first
                        if cb.count() > 0 and cb.is_visible():
                            cb.click(force=True, timeout=2000)
                    except Exception:
                        pass

                    page.wait_for_timeout(1000)

                # [6] Form Gönderimi (Submit)
                yield f"data: [6] Form gönderiliyor...\n\n"
                submit_btn = page.locator("button[type='submit']").last
                if submit_btn.count() > 0:
                    submit_btn.click(force=True)
                    page.wait_for_timeout(4000)

                # [7] SpamOk Mail Bekleme (max 5 dk)
                yield f"data: [7] Doğrulama bağlantısı bekleniyor...\n\n"
                yield f"data: [*] '{local_prefix}@spamok.com' gelen kutusu dinleniyor...\n\n"

                deadline = time.time() + 300
                seen_ids = set()
                magic_link = None

                while time.time() < deadline:
                    yield ": ping\n\n"
                    try:
                        r = requests.get(f"{SPAMOK_API}/EmailBox/{local_prefix}", timeout=15)
                        if r.ok:
                            mails = r.json().get("mails", [])
                            for m in mails:
                                if "Viw AI" not in m.get("subject", ""):
                                    continue
                                mid = m["id"]
                                if mid in seen_ids:
                                    continue
                                seen_ids.add(mid)
                                d = requests.get(f"{SPAMOK_API}/Email/{local_prefix}/{mid}", timeout=15).json()
                                text = d.get("messagePlain", "") + "\n" + d.get("messageHtml", "")
                                match = re.search(
                                    r"https://viw\.ai/api/auth/magic-link/verify\?token=[^\s\"'<>&]+(?:&amp;|&)callbackURL=[^\s\"'<>]+",
                                    text,
                                )
                                if match:
                                    magic_link = match.group(0).replace("&amp;", "&")
                                    break
                        if magic_link:
                            break
                    except Exception as err:
                        yield f"data: [!] Mail kontrolü sırasında hata: {err}\n\n"
                    time.sleep(3)

                if not magic_link:
                    raise TimeoutError("Magic link e-postası zaman aşımına uğradı (gelmedi).")

                yield f"data: \n\n"
                yield f"data: [+] Doğrulama linki alındı: {magic_link}\n\n"
                yield f"data: \n\n"

                # [8] Linke tarayıcı üzerinden git ve oturumu tamamla (max 5 dk)
                yield f"data: [8] Doğrulama linki açılıyor...\n\n"
                page.goto(magic_link, wait_until="networkidle", timeout=300000)
                page.wait_for_timeout(3000)

                # [9] Oturum Bilgisini Al
                session_data = page.evaluate("""async () => {
                    try {
                        const r = await fetch('/api/auth/get-session');
                        if (r.ok) return await r.json();
                    } catch(e) {}
                    return null;
                }""")

                if session_data and session_data.get("user"):
                    yield f"data: [🎉] BAŞARILI! Oturum açıldı.\n\n"
                    yield f"data:      Kullanıcı ID: {session_data['user'].get('id')}\n\n"
                    yield f"data:      E-posta: {session_data['user'].get('email')}\n\n"
                else:
                    yield f"data: [*] Sayfa yüklendi, oturum durumu: {session_data}\n\n"

                # [10] Çerezleri kaydet
                cookies = context.cookies()
                try:
                    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                        json.dump(cookies, f, indent=2, ensure_ascii=False)
                    yield f"data: [+] Çerezler '{COOKIE_FILE}' dosyasına başarıyla kaydedildi.\n\n"
                except Exception as fe:
                    yield f"data: [!] Çerez kaydedilirken hata: {fe}\n\n"

                yield f"data: \n\n"
                yield f"data: ============================================================\n\n"
                yield f"data: [🎉] HESAP BAŞARIYLA OLUŞTURULDU!\n\n"
                yield f"data: E-POSTA       : {test_email}\n\n"
                yield f"data: TOPLAM ÇEREZ  : {len(cookies)}\n\n"
                yield f"data: ============================================================\n\n"
                yield f"data: [BITTI]\n\n"

                context.close()

        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            yield f"data: [HATA] Bir hata oluştu: {type(err).__name__}: {str(err)}\n\n"
            yield f"data: [BITTI]\n\n"
            if context:
                try:
                    context.close()
                except Exception:
                    pass

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
