"""Robô de automação do painel SSTV (sstv.center).

Gera testes automaticamente usando Playwright + 2Captcha (para o reCAPTCHA v2).

Fluxo:
  1. Abre sstv.center no Chromium headless
  2. Preenche login (usuário + senha)
  3. Resolve o reCAPTCHA "não sou um robô" via 2Captcha
  4. Clica em login
  5. Na dashboard, rola até o final e clica em "Completo + adultos"
  6. Captura as credenciais geradas (usuário/senha) ou qualquer mensagem de sucesso
  7. Retorna os dados para o worker enviar ao cliente no WhatsApp

Requer: playwright instalado + Chromium baixado (ver Dockerfile).
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger("carolinne.sstv_rpa")

# Configuração via variáveis de ambiente (NUNCA hardcodear)
SSTV_LOGIN_URL = os.getenv("SSTV_LOGIN_URL", "https://sstv.center/")
SSTV_USER = os.getenv("SSTV_USER", "")
SSTV_PASS = os.getenv("SSTV_PASS", "")
TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "")
SSTV_RPA_TIMEOUT = int(os.getenv("SSTV_RPA_TIMEOUT", "120"))  # segundos totais
SSTV_HEADLESS = os.getenv("SSTV_HEADLESS", "true").lower() == "true"


class SSTVResult:
    """Resultado da geração de teste."""

    def __init__(
        self,
        success: bool,
        username: Optional[str] = None,
        password: Optional[str] = None,
        raw_text: Optional[str] = None,
        error: Optional[str] = None,
    ):
        self.success = success
        self.username = username
        self.password = password
        self.raw_text = raw_text
        self.error = error

    def to_dict(self):
        return {
            "success": self.success,
            "username": self.username,
            "password": self.password,
            "raw_text": self.raw_text,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 2Captcha — resolve reCAPTCHA v2
# ---------------------------------------------------------------------------
async def solve_recaptcha_v2(page, api_key: str) -> Optional[str]:
    """Envia o sitekey do reCAPTCHA pro 2Captcha e retorna o token g-recaptcha-response."""
    # Extrai o sitekey do reCAPTCHA da página
    try:
        sitekey = await page.evaluate("""
            () => {
                const el = document.querySelector('.g-recaptcha[data-sitekey]')
                          || document.querySelector('iframe[src*="recaptcha"]');
                if (el && el.dataset && el.dataset.sitekey) return el.dataset.sitekey;
                const iframe = document.querySelector('iframe[src*="recaptcha"]');
                if (iframe) {
                    const match = iframe.src.match(/[?&]sitekey=([^&]+)/);
                    if (match) return match[1];
                }
                return null;
            }
        """)
    except Exception as exc:
        logger.warning("Erro ao extrair sitekey: %s", exc)
        return None

    if not sitekey:
        logger.info("Nenhum reCAPTCHA encontrado na página (talvez já esteja resolvido)")
        return None

    logger.info("reCAPTCHA detectado. Sitekey: %s. Enviando ao 2Captcha...", sitekey)

    # Submete o captcha ao 2Captcha
    async with httpx.AsyncClient(timeout=30) as client:
        submit = await client.post(
            "https://2captcha.com/in.php",
            data={
                "key": api_key,
                "method": "userrecaptcha",
                "googlekey": sitekey,
                "pageurl": page.url,
                "json": 1,
            },
        )
        submit_data = submit.json()
        if submit_data.get("status") != 1:
            logger.error("2Captcha rejeitou submissão: %s", submit_data)
            return None
        captcha_id = submit_data["request"]
        logger.info("Captcha submetido. ID: %s. Aguardando resolução...", captcha_id)

    # Polling até resolver (2Captcha leva 10-60s normalmente)
    for attempt in range(30):
        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
            )
            data = resp.json()
            if data.get("status") == 1:
                token = data["request"]
                logger.info("reCAPTCHA resolvido pelo 2Captcha! Token: %s...", token[:30])
                return token
            if data.get("request") != "CAPCHA_NOT_READY":
                logger.error("2Captcha erro: %s", data)
                return None

    logger.error("2Captcha demorou demais (timeout)")
    return None


async def inject_recaptcha_token(page, token: str) -> None:
    """Injeta o token do reCAPTCHA no campo hidden da página."""
    await page.evaluate(f"""
        () => {{
            document.getElementById('g-recaptcha-response').value = '{token}';
            document.getElementById('g-recaptcha-response').style.display = 'block';
        }}
    """)


# ---------------------------------------------------------------------------
# Robô principal
# ---------------------------------------------------------------------------
async def gerar_teste_sstv(device: str = "", cliente_nome: str = "", cliente_numero: str = "") -> SSTVResult:
    """Gera um teste no painel SSTV. Retorna as credenciais ou erro.

    Args:
        device: aparelho escolhido pelo cliente (Fire Stick, etc) — só pra log
        cliente_nome: nome do cliente (pra log)
        cliente_numero: WhatsApp do cliente (pra log)
    """
    if not SSTV_USER or not SSTV_PASS:
        return SSTVResult(success=False, error="SSTV_USER ou SSTV_PASS não configurados")
    if not TWOCAPTCHA_API_KEY:
        return SSTVResult(success=False, error="TWOCAPTCHA_API_KEY não configurado")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return SSTVResult(success=False, error="Playwright não instalado no container")

    logger.info(
        "Iniciando geração de teste SSTV — cliente=%s numero=%s aparelho=%s",
        cliente_nome, cliente_numero, device,
    )

    start = time.time()
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=SSTV_HEADLESS)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
            page = await context.new_page()

            # ---- PASSO 1: Abrir página de login ----
            logger.info("Abrindo %s ...", SSTV_LOGIN_URL)
            await page.goto(SSTV_LOGIN_URL, wait_until="networkidle", timeout=30000)

            # ---- PASSO 2: Preencher login ----
            # Tenta vários seletores comuns pra campo de usuário/senha
            logger.info("Preenchendo login (usuário: %s)", SSTV_USER)

            # Campo usuário (tenta vários seletores)
            user_selectors = [
                'input[name="username"]', 'input[name="user"]', 'input[name="email"]',
                'input[type="email"]', 'input[type="text"]', '#username', '#user',
                'input[placeholder*="usuário"]', 'input[placeholder*="usuario"]',
                'input[placeholder*="user" i]', 'input[placeholder*="login" i]',
            ]
            user_filled = False
            for sel in user_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(SSTV_USER)
                        user_filled = True
                        logger.info("Usuário preenchido no seletor: %s", sel)
                        break
                except Exception:
                    continue
            if not user_filled:
                return SSTVResult(success=False, error="Não encontrou campo de usuário no login")

            # Campo senha
            pass_selectors = [
                'input[name="password"]', 'input[name="pass"]', 'input[name="senha"]',
                'input[type="password"]', '#password', '#pass',
                'input[placeholder*="senha" i]', 'input[placeholder*="password" i]',
            ]
            pass_filled = False
            for sel in pass_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(SSTV_PASS)
                        pass_filled = True
                        logger.info("Senha preenchida no seletor: %s", sel)
                        break
                except Exception:
                    continue
            if not pass_filled:
                return SSTVResult(success=False, error="Não encontrou campo de senha no login")

            # ---- PASSO 3: Resolver reCAPTCHA (se existir) ----
            token = await solve_recaptcha_v2(page, TWOCAPTCHA_API_KEY)
            if token:
                await inject_recaptcha_token(page, token)

            # ---- PASSO 4: Clicar em login/entrar ----
            login_selectors = [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Entrar")', 'button:has-text("Login")', 'button:has-text("login")',
                'button:has-text("Acessar")', 'button:has-text("Logar")',
                'a:has-text("Entrar")', 'a:has-text("Login")',
                '.btn-login', '#login-button', '#btn-login',
            ]
            login_clicked = False
            for sel in login_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        login_clicked = True
                        logger.info("Botão de login clicado: %s", sel)
                        break
                except Exception:
                    continue
            if not login_clicked:
                # Tenta submit do form diretamente
                try:
                    await page.keyboard.press("Enter")
                    login_clicked = True
                    logger.info("Login submetido via Enter")
                except Exception:
                    pass
            if not login_clicked:
                return SSTVResult(success=False, error="Não encontrou botão de login")

            # Aguarda navegação/redirecionamento após login
            await page.wait_for_load_state("networkidle", timeout=20000)
            logger.info("Após login, URL atual: %s", page.url)

            # Verifica se login funcionou (se voltou pra tela de login, falhou)
            page_text_lower = (await page.inner_text("body")).lower()
            if any(w in page_text_lower for w in ["inválido", "invalid", "incorreta", "incorrect", "senha incorreta"]):
                return SSTVResult(success=False, error="Login recusado pelo SSTV (usuário/senha inválidos)")

            # ---- PASSO 5: Dashboard → rolar até final → clicar "Completo + adultos" ----
            logger.info("Na dashboard. Rolando até o final...")

            # Rola a página toda até o final (gradualmente)
            for _ in range(10):
                await page.evaluate("window.scrollBy(0, 600)")
                await asyncio.sleep(0.3)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)

            # Procura e clica no botão "Completo + adultos"
            btn_selectors = [
                'button:has-text("Completo + adultos")',
                'a:has-text("Completo + adultos")',
                'button:has-text("Completo + Adultos")',
                'a:has-text("Completo + Adultos")',
                'text="Completo + adultos"',
                # Variantes mais flexíveis
                'button:has-text("Completo")',
                'a:has-text("Completo")',
            ]
            btn_clicked = False
            for sel in btn_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.scroll_into_view_if_needed(timeout=5000)
                        await el.click()
                        btn_clicked = True
                        logger.info("Botão clicado: %s", sel)
                        break
                except Exception:
                    continue

            if not btn_clicked:
                # Captura o texto da página pra debugar
                body_text = await page.inner_text("body")
                return SSTVResult(
                    success=False,
                    error="Não encontrou botão 'Completo + adultos' na dashboard",
                    raw_text=body_text[:1000],
                )

            # ---- PASSO 6: Capturar credenciais geradas ----
            # Após clicar, o sistema pode mostrar um popup, modal, ou texto na tela.
            # Vamos capturar tudo que aparecer nos próximos segundos.
            logger.info("Botão clicado. Aguardando credenciais...")
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Captura todo o texto visível na página
            body_text = await page.inner_text("body")
            logger.info("Texto após clique (primeiros 500 chars): %s", body_text[:500])

            # Tenta extrair usuário e senha do texto (vários formatos possíveis)
            username = None
            password = None

            # Padrões exatos do popup do SSTV:
            # "✅ Usuário: 4VWgmnpnJQ"
            # "✅ Senha: Tdg9BMmgT2"
            user_patterns = [
                r"usuário\s*:?\s*([A-Za-z0-9_.\-]{4,40})",
                r"usuario\s*:?\s*([A-Za-z0-9_.\-]{4,40})",
                r"user\s*:?\s*([A-Za-z0-9_.\-]{4,40})",
                r"username\s*:?\s*([A-Za-z0-9_.\-]{4,40})",
            ]
            pass_patterns = [
                r"senha\s*:?\s*([A-Za-z0-9_.\-@!#$%]{4,40})",
                r"password\s*:?\s*([A-Za-z0-9_.\-@!#$%]{4,40})",
            ]
            for pat in user_patterns:
                m = re.search(pat, body_text, re.I)
                if m:
                    username = m.group(1).strip()
                    break
            for pat in pass_patterns:
                m = re.search(pat, body_text, re.I)
                if m:
                    password = m.group(1).strip()
                    break

            # Tenta também capturar de modais/popups específicos
            modal_selectors = [".modal", ".popup", ".alert", ".notification", ".result", ".success"]
            for sel in modal_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        modal_text = await el.inner_text()
                        for pat in user_patterns:
                            m = re.search(pat, modal_text, re.I)
                            if m and not username:
                                username = m.group(1).strip()
                        for pat in pass_patterns:
                            m = re.search(pat, modal_text, re.I)
                            if m and not password:
                                password = m.group(1).strip()
                        if modal_text and not body_text:
                            body_text = modal_text
                except Exception:
                    continue

            elapsed = round(time.time() - start, 1)
            if username or password or "sucesso" in body_text.lower() or "success" in body_text.lower() or "criado" in body_text.lower():
                logger.info("Teste gerado com sucesso! user=%s pass=%s (%ss)", username, "***" if password else None, elapsed)
                return SSTVResult(
                    success=True,
                    username=username,
                    password=password,
                    raw_text=body_text[:2000],
                )

            return SSTVResult(
                success=False,
                error="Botão clicado mas não conseguiu extrair credenciais (painel pode ter mudado)",
                raw_text=body_text[:2000],
            )

    except asyncio.TimeoutError:
        return SSTVResult(success=False, error="Timeout na automação (painel demorou demais)")
    except Exception as exc:
        logger.exception("Erro na automação SSTV")
        return SSTVResult(success=False, error=f"Erro inesperado: {str(exc)[:200]}")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Formatação da mensagem pro cliente (baseada no aparelho)
# ---------------------------------------------------------------------------
def formatar_mensagem_cliente(result: "SSTVResult", device: str = "") -> list[str]:
    """Gera as mensagens formatadas pra enviar ao cliente no WhatsApp.

    Retorna uma lista de mensagens (cada uma é um balão separado).
    Envia APENAS as instruções relevantes pro aparelho do cliente, nunca o popup inteiro.
    """
    if not result.success or not result.username or not result.password:
        return []

    user = result.username
    pwd = result.password
    d = (device or "").lower()
    messages = []

    # Mensagem 1 — credenciais + aviso de maiúsculas/minúsculas
    messages.append(
        f"CREDENCIAIS DE ACESSO\n"
        f"✅ Usuário: {user}\n"
        f"✅ Senha: {pwd}"
    )

    # Mensagem 2 — instruções específicas do aparelho
    if "apple tv" in d:
        messages.append(
            f"Na Apple TV, abra o Max Player.\n"
            f"Acesso: Usuário + Senha + DNS\n"
            f"DNS: http://stv.cx"
        )
    elif "fire" in d or "android tv" in d or "google tv" in d or "tv box" in d:
        messages.append(
            f"No Fire Stick/Android TV, use o Downloader.\n"
            f"Downloader: 952155 ou 5269346\n"
            f"Vai instalar o STV.1 (Auto Update).\n\n"
            f"Acesso: Usuário + Senha + DNS\n"
            f"DNS: http://stv.cx"
        )
    elif "roku" in d or "samsung" in d or "lg" in d:
        messages.append(
            f"Na sua TV, use o Vizzion Play.\n"
            f"Ao abrir, clique em entrar com código.\n"
            f"Código: 646482 ou 018270 ou 161070\n\n"
            f"Depois informe Usuário + Senha."
        )
    elif "iphone" in d or "ipad" in d:
        messages.append(
            f"No iPhone/iPad, use o XCloud TV ou Vizzion Play.\n"
            f"Se XCloud: ServerSSTV + Usuário + Senha\n"
            f"Se Vizzion: Código 646482 + Usuário + Senha"
        )
    elif "celular" in d or "android" in d:
        messages.append(
            f"No celular Android, instale o app:\n"
            f"https://sdev.cx/stvnovo.apk\n\n"
            f"Acesso: Usuário + Senha + DNS\n"
            f"DNS: http://stv.cx"
        )
    else:
        # Aparelho não identificado — manda credenciais + DNS genérico
        messages.append(
            f"DNS (URL): http://stv.cx\n\n"
            f"Me diz qual aparelho você vai usar que eu te mando o passo a passo certinho 😊"
        )

    # Mensagem final — aviso de maiúsculas/minúsculas (sempre importante)
    messages.append(
        "ATENÇÃO 🚨\n\n"
        "Cuidado com as letras maiúsculas e minúsculas para saírem corretamente.\n"
        "Letras que mais costumam enganar:\n\n"
        "K - k\n"
        "I (i maiúsculo) com l (L minúsculo) - l\n"
        "S - s\n"
        "O - o\n"
        "V - v\n"
        "W - w"
    )

    return messages
