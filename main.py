import os, asyncio, random
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, Page

app = FastAPI(title='Worker Carolinne')
CHROMIUM_CDP = os.getenv('CHROMIUM_CDP', 'http://chromium-painel:9222')

class GerarTesteRequest(BaseModel):
    pacote: str = 'Completo'
    dispositivo: Optional[str] = None

class PainelActions:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.browser = None
        self.page = None
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
        print(f'[Worker] Conectado ao Navegador Persistente')

    async def solve_cloudflare(self):
        print('[Worker] Verificando se há captcha...')
        try:
            selector = 'input[type="checkbox"], #cf-turnstile-challenge, .cf-turnstile'
            await self.page.wait_for_selector(selector, timeout=5000)
            box = await self.page.locator(selector).bounding_box()
            if box:
                await self.page.mouse.move(box['x'] + 10, box['y'] + 10, steps=15)
                await asyncio.sleep(random.uniform(0.3, 0.7))
                await self.page.mouse.click(box['x'] + random.uniform(10, 20), box['y'] + random.uniform(10, 20))
                print('[Worker] Clique humano executado no captcha.')
                await asyncio.sleep(3)
        except:
            print('[Worker] Sem captcha visível, seguindo...')

    async def criar_teste(self, pacote='Completo'):
        await self.page.goto('https://sstv.center/dashboard')
        await self.solve_cloudflare()
        return {'status': 'Sessao Validada', 'msg': 'Acessei o painel e tentei liberar o captcha.'}

painel = PainelActions(CHROMIUM_CDP)

@app.on_event('startup')
async def startup():
    try: await painel.conectar()
    except Exception as e: print(f'Erro de Conexao: {e}')

@app.get('/health')
async def health(): return {'status': 'ok'}

@app.post('/gerar-teste')
async def gerar_teste(req: GerarTesteRequest):
    try: return {'success': True, 'data': await painel.criar_teste(req.pacote)}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
