"""텔레그램 일회용 코드로 로그인하는 읽기 전용 웹 대시보드.

- 비밀번호를 따로 만들지 않는다: "로그인 코드 받기"를 누르면 6자리 코드가
  TELEGRAM_USER_CHAT_ID(본인 개인 DM)로만 전송되고, 그 코드를 입력해야 들어갈 수 있다.
  즉 텔레그램 앱이 설치된 본인 폰이 있어야만 로그인 가능 - SMS 인증과 비슷한 효과를
  추가 비용 없이 낸다.
- 세션은 서명된 쿠키 하나로 유지한다 (서버 쪽 세션 저장소 불필요).
- 이 대시보드는 조회 전용이다. 매매 제어(청산/중지 등)는 기존처럼 텔레그램 명령으로만 한다.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.config import CONFIG
from src.notify import telegram_bot
from src.utils.logger import get_logger

log = get_logger("web_dashboard")

SESSION_COOKIE = "auto_trade_session"
SESSION_TTL_SEC = 24 * 3600
OTP_TTL_SEC = 5 * 60
OTP_MAX_ATTEMPTS = 5
OTP_REQUEST_COOLDOWN_SEC = 30

# 프로세스 재시작마다 새로 생성 (WEB_SECRET_KEY 를 .env 에 고정해두면 재시작 후에도 세션 유지)
_SECRET_KEY = CONFIG.web_secret_key or secrets.token_hex(32)

_otp_state: dict = {"code": None, "expires_at": 0.0, "attempts": 0, "last_sent_at": 0.0}


def _sign(payload: str) -> str:
    sig = hmac.new(_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _make_session_token() -> str:
    expires_at = time.time() + SESSION_TTL_SEC
    return _sign(str(expires_at))


def _is_valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return time.time() < float(payload)
    except ValueError:
        return False


def _is_authed(request: Request) -> bool:
    return _is_valid_session(request.cookies.get(SESSION_COOKIE))


_LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>auto_trade 로그인</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f1115;color:#e6e6e6;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#1a1d24;padding:2rem;border-radius:12px;width:320px;box-shadow:0 4px 20px rgba(0,0,0,.4)}}
h1{{font-size:1.1rem;margin:0 0 1.2rem}}
button,input{{width:100%;padding:.7rem;border-radius:8px;border:1px solid #333;box-sizing:border-box;font-size:1rem}}
input{{background:#0f1115;color:#fff;margin-bottom:.7rem;text-align:center;letter-spacing:.3em}}
button{{background:#3b82f6;color:#fff;border:none;cursor:pointer;font-weight:600}}
button:hover{{background:#2563eb}}
.msg{{font-size:.85rem;color:#9ca3af;margin-top:.8rem;min-height:1.2em}}
.err{{color:#f87171}}
</style></head><body>
<div class="card">
<h1>🔒 auto_trade 대시보드</h1>
{body}
</div>
</body></html>"""

_LOGIN_FORM_STEP1 = """
<button onclick="requestCode(this)">로그인 코드 받기 (텔레그램 DM)</button>
<div class="msg" id="msg"></div>
<script>
async function requestCode(btn) {
  btn.disabled = true;
  const res = await fetch('/auth/request-code', {method: 'POST'});
  const data = await res.json();
  document.getElementById('msg').innerText = data.message;
  if (data.ok) location.href = '/auth/enter-code';
  else btn.disabled = false;
}
</script>
"""

_LOGIN_FORM_STEP2 = """
<form method="post" action="/auth/verify">
<input type="text" name="code" inputmode="numeric" maxlength="6" placeholder="6자리 코드" autofocus required>
<button type="submit">입장</button>
</form>
<div class="msg{err_class}">{message}</div>
"""

_DASHBOARD_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>auto_trade 대시보드</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:1.5rem}}
h1{{font-size:1.2rem;display:flex;justify-content:space-between;align-items:center}}
a{{color:#9ca3af;font-size:.85rem;text-decoration:none}}
pre{{background:#1a1d24;padding:1.2rem;border-radius:12px;white-space:pre-wrap;line-height:1.6;font-size:.95rem}}
.badge{{display:inline-block;padding:.2rem .6rem;border-radius:999px;font-size:.75rem;background:#1f2937;color:#9ca3af;margin-left:.5rem}}
</style></head><body>
<h1>📊 auto_trade 대시보드 <span><span class="badge" id="mode">{mode}</span> <a href="/logout">로그아웃</a></span></h1>
<pre id="status">불러오는 중...</pre>
<script>
async function refresh() {{
  try {{
    const res = await fetch('/api/status');
    if (res.status === 401) {{ location.href = '/'; return; }}
    const data = await res.json();
    document.getElementById('status').innerText = data.status_text;
  }} catch (e) {{ /* 네트워크 일시 오류 무시, 다음 주기에 재시도 */ }}
}}
refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""


def create_app(engine) -> FastAPI:
    app = FastAPI(title="auto_trade dashboard", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if _is_authed(request):
            return RedirectResponse("/dashboard")
        return HTMLResponse(_LOGIN_HTML.format(body=_LOGIN_FORM_STEP1))

    @app.post("/auth/request-code")
    async def request_code():
        now = time.time()
        if now - _otp_state["last_sent_at"] < OTP_REQUEST_COOLDOWN_SEC:
            wait = int(OTP_REQUEST_COOLDOWN_SEC - (now - _otp_state["last_sent_at"]))
            return JSONResponse({"ok": False, "message": f"{wait}초 후 다시 시도해주세요."})

        code = f"{secrets.randbelow(1_000_000):06d}"
        _otp_state.update(code=code, expires_at=now + OTP_TTL_SEC, attempts=0, last_sent_at=now)

        sent = await telegram_bot.send_to_personal(
            f"🔑 웹 대시보드 로그인 코드: {code}\n({OTP_TTL_SEC // 60}분 이내에 입력하세요. 요청하지 않았다면 무시하세요.)"
        )
        if not sent:
            return JSONResponse({"ok": False, "message": "TELEGRAM_USER_CHAT_ID 가 설정되지 않아 코드를 보낼 수 없습니다."})
        log.info("웹 로그인 코드 발송됨")
        return JSONResponse({"ok": True, "message": "텔레그램 DM으로 코드를 보냈습니다."})

    @app.get("/auth/enter-code", response_class=HTMLResponse)
    async def enter_code():
        return HTMLResponse(_LOGIN_HTML.format(body=_LOGIN_FORM_STEP2.format(err_class="", message="")))

    @app.post("/auth/verify")
    async def verify(code: str = Form(...)):
        now = time.time()
        if not _otp_state["code"] or now > _otp_state["expires_at"]:
            msg = "코드가 만료되었습니다. 다시 요청해주세요."
        elif _otp_state["attempts"] >= OTP_MAX_ATTEMPTS:
            msg = "시도 횟수를 초과했습니다. 새 코드를 요청해주세요."
        elif not hmac.compare_digest(code.strip(), _otp_state["code"]):
            _otp_state["attempts"] += 1
            msg = f"코드가 일치하지 않습니다. ({_otp_state['attempts']}/{OTP_MAX_ATTEMPTS})"
        else:
            _otp_state.update(code=None, expires_at=0.0, attempts=0)
            token = _make_session_token()
            resp = RedirectResponse("/dashboard", status_code=303)
            resp.set_cookie(
                SESSION_COOKIE, token, max_age=SESSION_TTL_SEC,
                httponly=True, samesite="lax", secure=(CONFIG.web_host != "127.0.0.1"),
            )
            log.info("웹 로그인 성공")
            return resp

        return HTMLResponse(
            _LOGIN_HTML.format(body=_LOGIN_FORM_STEP2.format(err_class=" err", message=msg)),
            status_code=401,
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not _is_authed(request):
            return RedirectResponse("/")
        return HTMLResponse(_DASHBOARD_HTML.format(mode=CONFIG.trading_mode.upper()))

    @app.get("/api/status")
    async def api_status(request: Request):
        if not _is_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        text = await engine.get_status_text()
        return JSONResponse({"status_text": text, "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    return app
