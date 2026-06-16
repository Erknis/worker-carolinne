import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

try:
    import redis
except Exception:
    redis = None

APP_NAME = os.getenv("BOT_NAME", "Carolinne")
API_KEY = os.getenv("CAROLINNE_WORKER_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "")
TEST_LINK = os.getenv("TEST_LINK") or os.getenv("CHATBOT_URL", "https://sstv.center/chatbot/98d64bd0-1b1d-4343-9896-a635bbd600f5")
PAYMENT_SITE_URL = os.getenv("PAYMENT_SITE_URL", "")
PIX_KEY = os.getenv("PIX_KEY", "03186401046")

app = FastAPI(title="Carolinne Worker", version="1.1.0")

class EvolutionInbound(BaseModel):
    event: Optional[str] = None
    instance: Optional[str] = None
    number: str = ""
    remoteJid: Optional[str] = None
    pushName: Optional[str] = None
    messageId: Optional[str] = None
    text: str = ""
    timestamp: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)

class WorkerReply(BaseModel):
    reply_text: str
    intent: str
    human_handoff: bool = False
    handoff_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class CustomerRecord(BaseModel):
    number: str
    name: Optional[str] = None
    last_text: str = ""
    last_intent: str = ""
    last_seen_at: str = ""
    message_count: int = 0
    requested_test: bool = False
    interested_plan: Optional[str] = None
    payment_method: Optional[str] = None
    needs_human: bool = False
    handoff_reason: Optional[str] = None
    history: List[Dict[str, Any]] = Field(default_factory=list)

class Store:
    def __init__(self):
        self.memory: Dict[str, Dict[str, Any]] = {}
        self.client = None
        if REDIS_URL and redis:
            try:
                self.client = redis.from_url(REDIS_URL, decode_responses=True)
                self.client.ping()
            except Exception as exc:
                print(f"[Store] Redis unavailable: {exc}")
                self.client = None

    def _get_json(self, key: str) -> Dict[str, Any]:
        if self.client:
            raw = self.client.get(key)
            return json.loads(raw) if raw else {}
        return self.memory.get(key, {})

    def _set_json(self, key: str, value: Dict[str, Any]):
        if self.client:
            self.client.set(key, json.dumps(value, ensure_ascii=False))
        else:
            self.memory[key] = value

    def inc_fallback(self, number: str) -> int:
        key = f"fallback:{number}"
        if self.client:
            value = self.client.incr(key)
            self.client.expire(key, 3600)
            return int(value)
        self.memory[key] = {"count": self.memory.get(key, {}).get("count", 0) + 1}
        return int(self.memory[key]["count"])

    def reset_fallback(self, number: str):
        key = f"fallback:{number}"
        if self.client:
            self.client.delete(key)
        else:
            self.memory.pop(key, None)

    def save_interaction(self, req: EvolutionInbound, reply: WorkerReply):
        if not req.number:
            return
        now = datetime.now(timezone.utc).isoformat()
        key = f"customer:{req.number}"
        record = self._get_json(key)
        history = record.get("history", [])[-19:]
        history.append({
            "at": now,
            "message_id": req.messageId,
            "text": req.text,
            "intent": reply.intent,
            "human_handoff": reply.human_handoff,
            "handoff_reason": reply.handoff_reason,
        })
        record.update({
            "number": req.number,
            "name": req.pushName or record.get("name"),
            "last_text": req.text,
            "last_intent": reply.intent,
            "last_seen_at": now,
            "message_count": int(record.get("message_count", 0)) + 1,
            "requested_test": bool(record.get("requested_test") or reply.intent == "gerar_teste"),
            "needs_human": bool(reply.human_handoff),
            "handoff_reason": reply.handoff_reason,
            "history": history,
        })
        if reply.intent == "planos":
            record["interested_plan"] = record.get("interested_plan") or "perguntou_planos"
        if reply.intent == "pix":
            record["payment_method"] = "pix_brl"
        if reply.intent == "pagamento_internacional":
            record["payment_method"] = "internacional"
        self._set_json(key, record)

    def get_customer(self, number: str) -> Dict[str, Any]:
        return self._get_json(f"customer:{number}")

store = Store()

INTENTS = {
    "gerar_teste": ["teste", "testar", "experimentar", "gratis", "grátis"],
    "planos": ["plano", "preço", "preco", "valor", "quanto", "mensal", "trimestral", "anual"],
    "pix": ["pix", "reais", "real", "brl", "chave", "pagar em reais"],
    "pagamento_internacional": ["dolar", "dólar", "euro", "cartao", "cartão", "paypal", "moeda", "internacional", "canada", "canadá", "usa", "eua"],
    "comprovante": ["comprovante", "paguei", "pagamento feito", "transferi", "enviei"],
    "suporte": ["suporte", "travando", "não funciona", "nao funciona", "caiu", "sem sinal", "tela preta", "erro", "cliente"],
    "apps": ["app", "aplicativo", "fire", "roku", "samsung", "lg", "iphone", "android", "tv box"],
    "humano": ["humano", "atendente", "emiliano", "responsável", "responsavel", "falar com alguém", "falar com alguem"],
}

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()

def detect_intent(text: str) -> str:
    t = normalize(text)
    if t in {"1", "01"}: return "gerar_teste"
    if t in {"2", "02"}: return "planos"
    if t in {"3", "03"}: return "suporte"
    if t in {"4", "04"}: return "pix"
    if t in {"5", "05"}: return "pagamento_internacional"
    if t in {"6", "06"}: return "humano"
    if any(w in t for w in ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite"]):
        if len(t) <= 30: return "saudacao"
    for intent, keywords in INTENTS.items():
        if any(k in t for k in keywords):
            return intent
    return "fallback"

def greeting(name: Optional[str]) -> str:
    first = (name or "").split(" ")[0]
    who = f", {first}" if first else ""
    return f"Oi{who}! Tudo bem? 😊\nEu sou a {APP_NAME}, da Liberou TV 📺\n\nComo posso te ajudar?\n\n1️⃣ Fazer teste grátis\n2️⃣ Ver planos\n3️⃣ Suporte para cliente\n4️⃣ Pagar em reais via PIX\n5️⃣ Pagar em outra moeda\n6️⃣ Falar com atendimento"

def build_reply(req: EvolutionInbound) -> WorkerReply:
    intent = detect_intent(req.text)
    if intent != "fallback":
        store.reset_fallback(req.number)

    if intent == "saudacao":
        return WorkerReply(intent=intent, reply_text=greeting(req.pushName))
    if intent == "gerar_teste":
        return WorkerReply(intent=intent, reply_text=f"Perfeito 😊\n\nPara gerar seu teste grátis de 3 horas, acesse o link abaixo:\n\n👉 {TEST_LINK}\n\nDepois que gerar, me diga qual aparelho você vai usar que eu te passo o app certo 📺")
    if intent == "planos":
        return WorkerReply(intent=intent, reply_text="Claro 😊 Esses são os planos da Liberou TV:\n\n📺 Mensal: R$50\n📺 Trimestral: R$130\n📺 Anual promocional: R$381,95\n\nO anual é o melhor custo-benefício ✅\n\nQual você prefere ativar?")
    if intent == "pix":
        return WorkerReply(intent=intent, reply_text=f"Perfeito 😊 Para pagamento em reais, pode fazer via PIX:\n\n🔑 PIX: {PIX_KEY}\n\nPlanos:\n📺 Mensal: R$50\n📺 Trimestral: R$130\n📺 Anual promocional: R$381,95\n\nDepois do pagamento, envie o comprovante aqui para ativarmos sua assinatura.")
    if intent == "pagamento_internacional":
        if PAYMENT_SITE_URL:
            return WorkerReply(intent=intent, reply_text=f"Sem problema 😊\nPara pagamento em dólar, euro, cartão ou outra moeda, finalize pelo nosso site:\n\n👉 {PAYMENT_SITE_URL}\n\nDepois me envie a confirmação aqui para agilizar sua ativação 📺")
        return WorkerReply(intent=intent, human_handoff=True, handoff_reason="pagamento internacional sem link configurado", reply_text="Sem problema 😊\nPara pagamento em outra moeda, vou chamar o atendimento humano para finalizar com você certinho.")
    if intent == "comprovante":
        return WorkerReply(intent=intent, human_handoff=True, handoff_reason="cliente enviou/avisou comprovante", reply_text="Recebi, obrigada 😊\nVou encaminhar para conferência e ativação. Assim que estiver tudo certo, avisamos por aqui 📺")
    if intent == "suporte":
        return WorkerReply(intent=intent, reply_text="Entendi 😊 Vamos começar pelo procedimento que resolve a maioria dos casos:\n\n1️⃣ Tire o roteador da tomada\n2️⃣ Aguarde de 5 a 6 minutos\n3️⃣ Ligue novamente\n4️⃣ Reinicie a TV/aparelho\n5️⃣ Abra o app e teste de novo\n\nSe continuar com problema, me envie: aparelho, app usado e uma foto/vídeo do erro.")
    if intent == "apps":
        return WorkerReply(intent=intent, reply_text="A Liberou TV funciona em vários aparelhos 😊\n\n📌 Fire Stick / Android TV / TV Box: app STV\n📌 Roku / Samsung / LG: app Vizzion\n📌 iPhone / iPad: app XCloud\n\nMe fala qual aparelho você usa que eu te passo a orientação certinha.")
    if intent == "humano":
        return WorkerReply(intent=intent, human_handoff=True, handoff_reason="cliente pediu atendimento humano", reply_text="Claro 😊 Vou chamar o atendimento humano para te ajudar.\n\nMe envie seu nome e o motivo do atendimento para agilizar.")

    count = store.inc_fallback(req.number)
    handoff = count >= 2
    return WorkerReply(intent="fallback", human_handoff=handoff, handoff_reason="fallback repetido" if handoff else None, reply_text="Desculpa, não consegui entender direitinho 😅\n\nEscolha uma opção:\n1️⃣ Fazer teste grátis\n2️⃣ Ver planos\n3️⃣ Suporte\n4️⃣ PIX\n5️⃣ Outra moeda\n6️⃣ Atendimento humano")

def reply_for(req: EvolutionInbound) -> WorkerReply:
    reply = build_reply(req)
    store.save_interaction(req, reply)
    reply.metadata.update({"customer_saved": bool(req.number), "redis": bool(store.client)})
    return reply

def check_api_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "carolinne-worker", "redis": bool(store.client)}

@app.post("/webhook/evolution", response_model=WorkerReply)
def webhook_evolution(req: EvolutionInbound, x_api_key: Optional[str] = Header(default=None)) -> WorkerReply:
    check_api_key(x_api_key)
    return reply_for(req)

@app.post("/preview", response_model=WorkerReply)
def preview(req: EvolutionInbound) -> WorkerReply:
    return reply_for(req)

@app.get("/customers/{number}")
def get_customer(number: str, x_api_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    check_api_key(x_api_key)
    record = store.get_customer(number)
    if not record:
        raise HTTPException(status_code=404, detail="customer not found")
    return record
