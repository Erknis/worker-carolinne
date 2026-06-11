import os
import asyncio
import random
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, Page

app = FastAPI(title="Worker Carolinne")

CHROMIUM_CDP = os.getenv("CHROMIUM_CDP", "http://chromium-painel:9222")

class GerarTesteRequest(BaseModel):
    pacote: str = "Completo"
    dispositivo: Optional[str] = None

class AtivarClienteRequest(BaseModel):
    usuario: str

class CriarClienteRequest(BaseModel):
    nome_cliente: str
    ultimos_4_whatsapp: str
    pacote: str = "Completo + Adultos"

class PainelActions:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self._playwright = None

    async def conectar(self):
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        contexts = self.browser.contexts
        if contexts:
            pages = contexts[0].pages
            self.page = pages[0] if pages else await contexts[0].new_page()
        else:
            context = await self.browser.new_context()
            self.page = await context.new_page()
        print(f"[Worker] Conectado. URL: {self.page.url}")

    async def desconectar(self):
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _clicar_humano(self, selector: str, descricao: str = ""):
        await self.page.wait_for_selector(selector, state="visible", timeout=15000)
        box = await self.page.locator(selector).bounding_box()
        if box:
            await self.page.mouse.move(
                box["x"] + box["width"] / 2 + random.uniform(-3, 3),
                box["y"] + box["height"] / 2 + random.uniform(-3, 3),
                steps=random.randint(10, 20)
            )
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await self.page.locator(selector).click()
        await asyncio.sleep(random.uniform(0.5, 1.2))
        print(f"[Worker] Clicou: {descricao or selector}")

    async def _digitar_humano(self, selector: str, texto: str, descricao: str = ""):
        await self.page.wait_for_selector(selector, state="visible", timeout=15000)
        await self.page.locator(selector).click()
        await asyncio.sleep(0.2)
        for char in texto:
            await self.page.keyboard.type(char, delay=random.randint(30, 120))
        await asyncio.sleep(0.3)
        print(f"[Worker] Digitou em {descricao or selector}: {texto[:20]}...")

    async def criar_teste(self, pacote: str = "Completo") -> Dict[str, Any]:
        page = self.page
        await self._clicar_humano('a[href*="criar-teste"], button:has-text("Criar Teste"), [data-menu="teste"]', "Menu Criar Teste")
        await self._clicar_humano('select[name="pacote"], select[id*="pacote"], .pacote-select', "Dropdown Pacote")
        await page.select_option('select[name="pacote"], select[id*="pacote"], .pacote-select', label=pacote)
        await asyncio.sleep(0.5)
        await self._clicar_humano('button:has-text("Gerar"), button:has-text("Criar"), button[type="submit"]', "Botão Gerar Teste")
        await page.wait_for_selector('.modal, .popup, .swal2-popup, [role="dialog"]', state="visible", timeout=15000)
        await asyncio.sleep(0.5)
        usuario = await page.locator('input[name="usuario"], .usuario-gerado, .login-gerado').input_value()
        senha = await page.locator('input[name="senha"], .senha-gerada, .password-gerado').input_value()
        dns = await page.locator('input[name="dns"], .dns-gerado').input_value()
        await self._clicar_humano('button:has-text("Fechar"), .modal-close, .swal2-close', "Fechar Popup")
        return {"usuario": usuario, "senha": senha, "dns": dns, "pacote": pacote, "links_apps": {}}

    async def ativar_cliente(self, usuario: str) -> Dict[str, Any]:
        page = self.page
        await self._clicar_humano('a[href*="clientes"], a[href*="lista"], button:has-text("Clientes")', "Menu Clientes")
        await asyncio.sleep(1)
        await self._digitar_humano('input[type="search"], input[placeholder*="Buscar"], input[name="busca"]', usuario, "Busca Usuário")
        await asyncio.sleep(1.5)
        await self._clicar_humano(f'tr:has-text("{usuario}") .calendario-verde, tr:has-text("{usuario}") [title*="crédito"], tr:has-text("{usuario}") .fa-calendar', f"Calendário Verde do {usuario}")
        await page.wait_for_selector('.modal, .popup, [role="dialog"]', state="visible", timeout=10000)
        await self._clicar_humano('input[name="creditos"], input[name="quantidade"], input[type="number"]', "Input Créditos")
        await page.fill('input[name="creditos"], input[name="quantidade"], input[type="number"]', "1")
        await asyncio.sleep(0.3)
        await self._clicar_humano('button:has-text("Confirmar"), button:has-text("Salvar"), button[type="submit"]', "Confirmar Crédito")
        await asyncio.sleep(1)
        return {"usuario": usuario, "acao": "ativado", "creditos_adicionados": 1}

    async def criar_cliente_direto(self, nome_cliente: str, ultimos_4_whatsapp: str, pacote: str = "Completo + Adultos") -> Dict[str, Any]:
        page = self.page
        await self._clicar_humano('a[href*="criar-cliente"], button:has-text("Criar Cliente")', "Menu Criar Cliente")
        await asyncio.sleep(1)
        await self._clicar_humano('input[name="usuario"] + button, button[title*="gerar usuario"], .gerar-usuario', "Gerar Usuário Auto")
        await asyncio.sleep(0.5)
        await self._clicar_humano('input[name="senha"] + button, button[title*="gerar senha"], .gerar-senha', "Gerar Senha Auto")
        await asyncio.sleep(0.5)
        usuario = await page.locator('input[name="usuario"]').input_value()
        senha = await page.locator('input[name="senha"]').input_value()
        nota = f"{nome_cliente} {ultimos_4_whatsapp}"
        await self._digitar_humano('textarea[name="notas"], input[name="notas"], textarea[name="observacao"]', nota, "Notas")
        await self._clicar_humano('select[name="pacote"], select[id*="pacote"]', "Dropdown Pacote")
        await page.select_option('select[name="pacote"], select[id*="pacote"]', label=pacote)
        await asyncio.sleep(0.5)
        await self._clicar_humano('button:has-text("Salvar"), button[type="submit"]', "Salvar Cliente")
        await asyncio.sleep(1.5)
        return {"usuario": usuario, "senha": senha, "nota": nota, "pacote": pacote}

    async def verificar_status(self, usuario: str) -> Dict[str, Any]:
        page = self.page
        await self._clicar_humano('a[href*="clientes"], a[href*="lista"]', "Menu Clientes")
        await asyncio.sleep(1)
        await self._digitar_humano('input[type="search"], input[placeholder*="Buscar"]', usuario, "Busca Status")
        await asyncio.sleep(1.5)
        relogio_amarelo = await page.locator(f'tr:has-text("{usuario}") .relogio-amarelo, tr:has-text("{usuario}") [class*="teste"], tr:has-text("{usuario}") [title*="teste"]').count()
        return {"usuario": usuario, "eh_teste": relogio_amarelo > 0, "status": "teste" if relogio_amarelo > 0 else "ativo"}

painel = PainelActions(CHROMIUM_CDP)

@app.on_event("startup")
async def startup():
    await painel.conectar()

@app.on_event("shutdown")
async def shutdown():
    await painel.desconectar()

@app.post("/gerar-teste")
async def gerar_teste(req: GerarTesteRequest):
    try:
        resultado = await painel.criar_teste(pacote=req.pacote)
        return {"success": True, "data": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ativar-cliente")
async def ativar_cliente(req: AtivarClienteRequest):
    try:
        resultado = await painel.ativar_cliente(req.usuario)
        return {"success": True, "data": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/criar-cliente")
async def criar_cliente(req: CriarClienteRequest):
    try:
        resultado = await painel.criar_cliente_direto(
            nome_cliente=req.nome_cliente,
            ultimos_4_whatsapp=req.ultimos_4_whatsapp,
            pacote=req.pacote
        )
        return {"success": True, "data": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status/{usuario}")
async def status(usuario: str):
    try:
        resultado = await painel.verificar_status(usuario)
        return {"success": True, "data": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok"}
