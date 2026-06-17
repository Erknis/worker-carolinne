import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
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

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_CLIENTS_TABLE = os.getenv("SUPABASE_CLIENTS_TABLE", "clientes")
SUPABASE_PHONE_COLUMN = os.getenv("SUPABASE_PHONE_COLUMN", "whatsapp")

# Optional direct Evolution API sending. This lets the worker be the full API agent without n8n.
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME") or os.getenv("EVOLUTION_INSTANCE", "liberoutv")
EMILIANO_PHONE = os.getenv("EMILIANO_PHONE") or os.getenv("EMILIANO_WHATSAPP_NUMBER", "")

# LLM / API agent settings. If LLM_API_KEY is empty, the worker uses the reliable rule fallback.
AGENT_MODE = os.getenv("AGENT_MODE", "hybrid").lower()  # hybrid | llm | rules
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "18"))
BRAIN_PATH = Path(os.getenv("CAROLINNE_BRAIN_PATH", "carolinne_brain.md"))

app = FastAPI(title="Carolinne Worker", version="2.0.0-agent-api")


def load_brain() -> str:
    try:
        return BRAIN_PATH.read_text(encoding="utf-8")
    except Exception:
        return """
Você é Carolinne, atendente da Liberou TV. Planos: mensal R$50, trimestral R$130, anual R$381,95. PIX 03186401046. Teste: NUNCA envie link interno de geração; diga que vai gerar por aqui e peça aparelho/nome se necessário. Suporte: orientar roteador 5-6 minutos. Nunca confirme pagamento; escale comprovante/humano/suporte complexo.
""".strip()

CAROLINNE_BRAIN = load_brain()


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
                print("[Store] Redis connected")
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

    def get_customer(self, number: str) -> Dict[str, Any]:
        return self._get_json(f"customer:{number}")

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


store = Store()

INTENTS = {
    "gerar_teste": ["teste", "testar", "experimentar", "gratis", "grátis", "trial"],
    "planos": ["plano", "preço", "preco", "valor", "quanto", "mensal", "trimestral", "anual"],
    "pix": ["pix", "reais", "real", "brl", "chave", "pagar em reais"],
    "pagamento_internacional": ["dolar", "dólar", "euro", "cartao", "cartão", "paypal", "moeda", "internacional", "canada", "canadá", "usa", "eua"],
    "comprovante": ["comprovante", "paguei", "pagamento feito", "transferi", "enviei", "recibo"],
    "suporte": ["suporte", "travando", "não funciona", "nao funciona", "caiu", "sem sinal", "tela preta", "erro", "cliente"],
    "apps": ["app", "aplicativo", "fire", "firestick", "downloader", "instalar", "instalação", "roku", "samsung", "lg", "iphone", "ipad", "android", "tv box", "vizzion", "xcloud", "stv", "smarters", "ss player", "ibo"],
    "canais": ["canais", "canal", "filmes", "series", "séries", "adultos", "futebol", "novela", "globo", "sportv", "premiere"],
    "humano": ["humano", "atendente", "emiliano", "responsável", "responsavel", "falar com alguém", "falar com alguem", "ligação", "ligacao", "audio", "áudio"],
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
    greeting_patterns = [r"^oi[!,. ]*$", r"^olá[!,. ]*$", r"^ola[!,. ]*$", r"^bom dia[!,. ]*$", r"^boa tarde[!,. ]*$", r"^boa noite[!,. ]*$"]
    if len(t) <= 35 and any(re.search(pattern, t) for pattern in greeting_patterns):
        return "saudacao"
    for intent, keywords in INTENTS.items():
        if any(k in t for k in keywords):
            return intent
    return "fallback"


def greeting(name: Optional[str]) -> str:
    first = (name or "").split(" ")[0]
    who = f", {first}" if first else ""
    return f"Oi{who}! Tudo bem? 😊\nMe fala rapidinho: você quer teste, planos ou suporte?"


def device_reply(text: str, number: str = "") -> str:
    t = normalize(text)
    tz = infer_timezone(number, text)
    usa_canada = tz in {"America/New_York", "America/Toronto"} or any(w in t for w in ["eua", "usa", "estados unidos", "canada", "canadá"])
    if "apple tv" in t:
        return "Na Apple TV usamos o Max Player 😊\nVocê consegue procurar Max Player na App Store da Apple TV?"
    if "fire" in t or "android tv" in t or "google tv" in t or "tv box" in t:
        code = "952155 ou 5269346" if usa_canada else "441676 ou 4618458"
        app = "STV.1 Auto Update" if usa_canada else "STV Smarters"
        return f"Perfeito. Nesse aparelho usamos o Downloader.\n\nBaixa o Downloader e digita o código {code}.\nEle vai instalar o {app}."
    if "iphone" in t or "ipad" in t:
        return "No iPhone/iPad pode usar Vizzion Play ou XCloud.\n\nSe abrir o XCloud, coloca o provider: LiberouTV"
    if "roku" in t or "lg" in t or "samsung" in t:
        return "Nesse modelo a prioridade é Vizzion Play.\nProcura Vizzion Play na loja da TV e me avisa se encontrou."
    if "android" in t or "celular" in t or "telefone" in t:
        link = "https://sdev.cx/stvnovo.apk" if usa_canada else "https://sdev.cx/stv.apk"
        return f"No telefone Android é mais simples.\nAbre esse link nele:\n{link}"
    return "Me diz o aparelho certinho.\nÉ Fire Stick, Apple TV, Roku, LG, Samsung, Android ou iPhone?"


def rules_reply(req: EvolutionInbound) -> WorkerReply:
    intent = detect_intent(req.text)
    if intent != "fallback":
        store.reset_fallback(req.number)

    if intent == "saudacao":
        return WorkerReply(intent=intent, reply_text=greeting(req.pushName))
    if intent == "gerar_teste":
        return WorkerReply(intent=intent, human_handoff=True, handoff_reason="cliente pediu teste; acionar bot/ativação internamente", reply_text="Claro 😊\nVou gerar seu teste por aqui.\n\nMe diz só em qual aparelho vai usar?")
    if intent == "planos":
        return WorkerReply(intent=intent, reply_text="Claro 😊\nVocê está em qual país?\n\nAí te passo os valores na moeda certinha.")
    if intent == "pix":
        return WorkerReply(intent=intent, reply_text=f"Para finalizar seu acesso, segue os dados do Pix:\n🇧🇷 Chave Pix (CPF): {PIX_KEY}\n👤 Nome: Emiliano Louzada de Oliveira\n✅ Assim que o pagamento for confirmado, seu acesso é ativado na hora!\n📲 Me manda o comprovante aqui no WhatsApp para agilizar!\nObrigado pela confiança! 🙏🔥")
    if intent == "pagamento_internacional":
        return WorkerReply(intent=intent, reply_text="🎬 Esse tipo de pagamento é pelo nosso site\n\n👉 www.liberoutv.com\n\nEntra no site, clica no seu país, toca em Acessar Agora e escolhe o plano.\n\nDepois me manda o comprovante aqui 🚀")
    if intent == "comprovante":
        return WorkerReply(intent=intent, human_handoff=True, handoff_reason="cliente enviou/avisou comprovante", reply_text="Recebi, obrigada 😊\nVou encaminhar para conferência e ativação. Assim que estiver tudo certo, avisamos por aqui 📺")
    if intent == "suporte":
        return WorkerReply(intent=intent, reply_text="Vamos começar pelo principal 😊\n\nTira o roteador da tomada por 5 a 6 minutos, não menos.\nDepois liga de novo, fecha o app totalmente e abre novamente.")
    if intent == "apps":
        return WorkerReply(intent=intent, reply_text=device_reply(req.text, req.number))
    if intent == "canais":
        return WorkerReply(intent=intent, reply_text="Tem mais de 9 mil canais 😊\n\nBrasil completo, EUA, Canadá, Europa, filmes, séries, 24h e adultos.\n\nQuer que eu libere um teste pra você ver na prática?")
    if intent == "humano":
        return WorkerReply(intent=intent, human_handoff=True, handoff_reason="cliente pediu atendimento humano", reply_text="Claro 😊\nVou direcionar seu atendimento para o setor responsável.")

    count = store.inc_fallback(req.number)
    handoff = count >= 2
    if handoff:
        return WorkerReply(intent="fallback", human_handoff=True, handoff_reason="fallback repetido", reply_text="Certo 😊\nVou verificar isso por aqui pra você.")
    return WorkerReply(intent="fallback", reply_text="Me explica rapidinho o que você precisa?\nÉ teste, pagamento ou suporte?")


def safe_json_from_text(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise


def recent_context(number: str) -> Dict[str, Any]:
    if not number:
        return {}
    record = store.get_customer(number)
    if not record:
        return {}
    # keep only useful compact history
    compact = dict(record)
    compact["history"] = compact.get("history", [])[-8:]
    return compact


async def supabase_customer(number: str) -> Dict[str, Any]:
    """Optional permanent customer lookup from Supabase.

    Configure:
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=...
    SUPABASE_CLIENTS_TABLE=clientes
    SUPABASE_PHONE_COLUMN=whatsapp
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and number):
        return {}
    clean = re.sub(r"\D", "", number)
    url = SUPABASE_URL.rstrip("/") + f"/rest/v1/{SUPABASE_CLIENTS_TABLE}"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    params = {
        "select": "*",
        SUPABASE_PHONE_COLUMN: f"eq.{clean}",
        "limit": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            rows = response.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return {}
    except Exception as exc:
        return {"_lookup_error": str(exc)[:300]}


def infer_timezone(number: str, text: str = "") -> str:
    t = (text or "").lower()
    if "australia" in t or "austrália" in t or "aussie" in t:
        return "Australia/Sydney"
    if "nova zeland" in t or "new zealand" in t:
        return "Pacific/Auckland"
    if "canada" in t or "canadá" in t or "toronto" in t:
        return "America/Toronto"
    if "estados unidos" in t or "eua" in t or "usa" in t or "florida" in t or "new york" in t:
        return "America/New_York"
    digits = re.sub(r"\D", "", number or "")
    if digits.startswith("1"):
        return "America/New_York"
    if digits.startswith("61"):
        return "Australia/Sydney"
    if digits.startswith("64"):
        return "Pacific/Auckland"
    if digits.startswith("55"):
        return "America/Sao_Paulo"
    return "America/Toronto"


def local_time_context(req: EvolutionInbound) -> Dict[str, str]:
    tz_name = infer_timezone(req.number, req.text)
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        tz_name = "America/Toronto"
        now = datetime.now(ZoneInfo(tz_name))
    hour = now.hour
    if 5 <= hour < 12:
        greeting = "Bom dia"
    elif 12 <= hour < 18:
        greeting = "Boa tarde"
    else:
        greeting = "Boa noite"
    return {
        "timezone": tz_name,
        "local_time": now.strftime("%Y-%m-%d %H:%M"),
        "suggested_greeting": greeting,
    }


def sanitize_reply(reply: WorkerReply, req: EvolutionInbound) -> WorkerReply:
    """Hard safety layer after LLM/rules."""
    text = reply.reply_text or ""
    internal_patterns = [
        r"https?://sstv\.center/chatbot/\S+",
        r"https?://[^\s]*(?:autoreply|auto[-_]?reply|chatbot|gerar[-_]?teste)[^\s]*",
    ]
    for pattern in internal_patterns:
        text = re.sub(pattern, "", text, flags=re.I).strip()

    # Trial generation must never expose internal link. Until RPA is ready, hand off to activation.
    if detect_intent(req.text) == "gerar_teste" or reply.intent == "gerar_teste":
        if not text or "http" in text.lower() or len(text) > 240:
            text = "Claro. Eu gero o teste por aqui pra você.\n\nMe diz só em qual aparelho vai usar?"
        reply.intent = "gerar_teste"
        reply.human_handoff = True
        reply.handoff_reason = reply.handoff_reason or "cliente pediu teste; setor de ativação precisa gerar internamente"

    # Audio/call cannot be handled by this path.
    t = normalize(req.text)
    if any(k in t for k in ["áudio", "audio", "ligação", "ligacao"]):
        text = "Não consigo atender ligação/áudio por aqui agora.\nMe manda por texto ou uma foto da tela que eu te ajudo rapidinho."
        reply.human_handoff = True
        reply.handoff_reason = reply.handoff_reason or "cliente enviou áudio/ligação"

    # Avoid extremely long robotic walls unless the customer asked for detailed info.
    if len(text) > 900 and not any(k in t for k in ["tutorial", "passo", "completo", "detalhe"]):
        text = text[:850].rsplit("\n", 1)[0].strip() + "\n\nSe quiser, continuo te guiando por partes."

    reply.reply_text = text or "Certo. Vou direcionar seu atendimento para o setor responsável."
    return reply


async def llm_reply(req: EvolutionInbound) -> WorkerReply:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY/OPENAI_API_KEY not configured")

    detected = detect_intent(req.text)
    customer = recent_context(req.number)
    permanent_customer = await supabase_customer(req.number)
    time_ctx = local_time_context(req)
    system = f"""
{CAROLINNE_BRAIN}

Variáveis atuais:
- PIX_KEY: {PIX_KEY}
- PAYMENT_SITE_URL/site: {PAYMENT_SITE_URL or "www.liberoutv.com"}

REGRA CRÍTICA:
- Nunca envie TEST_LINK, CHATBOT_URL, link interno de AutoReply, endpoint SSTV ou URL de geração de teste ao cliente.
- Quando cliente pedir teste, diga que vai gerar por aqui e pergunte o aparelho.
- Se ainda não houver automação de geração, marque human_handoff=true para setor de ativação.

REGRAS ABSOLUTAS:
- Nunca envie links internos de teste para o cliente.
- Para pedido de teste, responda curto: diga que você gera por aqui, pergunte/confirme o aparelho, e marque human_handoff=true para ativação enquanto a geração automática não existir.
- Respostas no WhatsApp devem ser curtas, humanas, sem textão, geralmente 1 a 4 linhas.
- Não use lista grande de opções, a não ser que o cliente peça.
- Evite repetir "bom dia/boa tarde/boa noite" em toda mensagem. Use saudação só no início ou quando o cliente cumprimentar.
- Use poucos emojis. Não use o mesmo emoji em toda resposta.
- Se precisar de ação que ainda não está automatizada, marque human_handoff=true.
- Quando human_handoff=true e você não conseguir resolver, diga ao cliente de forma humana que vai direcionar para o setor correto: financeiro, suporte técnico/TI, ativação ou atendimento.
- Nunca diga que é robô, IA, automático ou sistema.
- Se a pessoa mandar áudio ou tentar ligação, diga que por aqui você não consegue atender ligação/áudio agora e peça texto ou foto.

Responda SOMENTE em JSON válido. Não use markdown.
Schema obrigatório:
{{
  "reply_text": "texto exato a enviar ao cliente no WhatsApp",
  "intent": "saudacao|gerar_teste|planos|pix|pagamento_internacional|comprovante|suporte|apps|canais|humano|fallback",
  "human_handoff": false,
  "handoff_reason": null,
  "metadata": {{}}
}}
""".strip()
    user = {
        "customer": {
            "number": req.number,
            "name": req.pushName,
            "last_known_record": customer,
            "supabase_customer_record": permanent_customer,
        },
        "message": req.text,
        "local_time": time_ctx,
        "rule_detected_intent": detected,
        "instruction": "Responda como Carolinne com base no cérebro oficial. Seja curta, humana e útil. Use a saudação sugerida apenas se for início de conversa. Não mande textão; 1 a 4 linhas sempre que possível. Se precisar de humano, marque human_handoff=true.",
    }
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.65,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = safe_json_from_text(content)
    return WorkerReply(
        reply_text=str(parsed.get("reply_text") or "Desculpa, não consegui responder agora. Vou chamar o atendimento para ajudar."),
        intent=str(parsed.get("intent") or detected or "fallback"),
        human_handoff=bool(parsed.get("human_handoff", False)),
        handoff_reason=parsed.get("handoff_reason"),
        metadata=parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {},
    )


async def build_reply(req: EvolutionInbound) -> WorkerReply:
    if AGENT_MODE == "rules":
        reply = rules_reply(req)
        reply.metadata["agent_engine"] = "rules"
        return reply

    if AGENT_MODE in {"hybrid", "llm"} and LLM_API_KEY:
        try:
            reply = await llm_reply(req)
            if reply.intent != "fallback":
                store.reset_fallback(req.number)
            reply.metadata["agent_engine"] = "llm"
            reply.metadata["llm_model"] = LLM_MODEL
            return reply
        except Exception as exc:
            print(f"[LLM] failed, using rules fallback: {exc}")
            if AGENT_MODE == "llm":
                return WorkerReply(
                    intent="humano",
                    human_handoff=True,
                    handoff_reason="falha no agente LLM/API",
                    reply_text="Desculpa, tive uma instabilidade aqui. Vou chamar o atendimento humano para te ajudar rapidinho 😊",
                    metadata={"agent_engine": "llm_failed", "error": str(exc)[:300]},
                )

    reply = rules_reply(req)
    reply.metadata["agent_engine"] = "rules_fallback"
    return reply


def sanitize_reply(req: EvolutionInbound, reply: WorkerReply) -> WorkerReply:
    """Final safety filter before saving/sending to WhatsApp."""
    text = reply.reply_text or ""
    internal_patterns = [
        r"https?://sstv\.center/chatbot/\S*",
        r"sstv\.center/chatbot/\S*",
        r"98d64bd0-1b1d-4343-9896-a635bbd600f5",
        r"https?://[^\s]*(?:autoreply|auto[-_]?reply|chatbot|gerar[-_]?teste)[^\s]*",
    ]
    leaked = any(re.search(p, text, flags=re.I) for p in internal_patterns)
    for pattern in internal_patterns:
        text = re.sub(pattern, "", text, flags=re.I).strip()

    detected = detect_intent(req.text)
    if leaked or detected == "gerar_teste" or reply.intent == "gerar_teste":
        if not text or "http" in text.lower() or len(text) > 240:
            text = "Claro. Eu gero o teste por aqui pra você.\n\nMe diz só em qual aparelho vai usar?"
        reply.intent = "gerar_teste"
        reply.human_handoff = True
        reply.handoff_reason = reply.handoff_reason or "cliente pediu teste; setor de ativação precisa gerar internamente"
        if leaked:
            reply.metadata["safety_filter"] = "blocked_internal_test_link"

    t = normalize(req.text)
    if any(k in t for k in ["áudio", "audio", "ligação", "ligacao"]):
        text = "Não consigo atender ligação/áudio por aqui agora.\nMe manda por texto ou uma foto da tela que eu te ajudo rapidinho."
        reply.human_handoff = True
        reply.handoff_reason = reply.handoff_reason or "cliente enviou áudio/ligação"

    if len(text) > 900 and not any(k in t for k in ["tutorial", "passo", "completo", "detalhe"]):
        text = text[:850].rsplit("\n", 1)[0].strip() + "\n\nSe quiser, continuo te guiando por partes."

    reply.reply_text = text or "Certo. Vou direcionar seu atendimento para o setor responsável."
    return reply


async def reply_for(req: EvolutionInbound) -> WorkerReply:
    reply = await build_reply(req)
    reply = sanitize_reply(req, reply)
    store.save_interaction(req, reply)
    reply.metadata.update({"customer_saved": bool(req.number), "redis": bool(store.client)})
    return reply


def check_api_key(x_api_key: Optional[str], api_key_query: Optional[str] = None):
    provided = x_api_key or api_key_query
    if API_KEY and provided != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def normalize_evolution_payload(body: Dict[str, Any]) -> EvolutionInbound:
    data = body.get("data") or body
    msg = data.get("message") or data.get("messages") or {}
    key = data.get("key") or msg.get("key") or {}
    raw_jid = key.get("remoteJid") or data.get("remoteJid") or (data.get("key") or {}).get("remoteJid") or ""
    text = (
        msg.get("conversation")
        or (msg.get("extendedTextMessage") or {}).get("text")
        or (msg.get("imageMessage") or {}).get("caption")
        or (msg.get("documentMessage") or {}).get("caption")
        or data.get("text")
        or body.get("text")
        or ""
    )
    if not text and ("audioMessage" in msg or "ptt" in msg):
        text = "áudio recebido; pedir para enviar por texto"
    if not text and ("imageMessage" in msg or "documentMessage" in msg or "videoMessage" in msg):
        text = "comprovante ou imagem recebida; analisar como possível comprovante ou foto de suporte"
    number = re.sub(r"\D", "", str(raw_jid).replace("@s.whatsapp.net", ""))
    return EvolutionInbound(
        event=body.get("event"),
        instance=body.get("instance") or data.get("instance") or EVOLUTION_INSTANCE_NAME,
        number=number,
        remoteJid=raw_jid,
        pushName=data.get("pushName") or body.get("pushName") or "",
        messageId=key.get("id") or data.get("id") or "",
        text=text,
        raw=body,
    )


async def send_evolution_text(number: str, text: str) -> Dict[str, Any]:
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        raise RuntimeError("EVOLUTION_API_URL/EVOLUTION_API_KEY not configured")
    clean_number = re.sub(r"\D", "", number or "")
    if not clean_number:
        raise RuntimeError("missing destination number")
    url = EVOLUTION_API_URL.rstrip("/") + f"/message/sendText/{EVOLUTION_INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": clean_number, "text": text}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return {"status_code": response.status_code, "text": response.text[:500]}


async def notify_human(req: EvolutionInbound, reply: WorkerReply) -> Optional[Dict[str, Any]]:
    if not reply.human_handoff or not EMILIANO_PHONE:
        return None
    alert = (
        "🚨 Carolinne pediu atendimento humano\n"
        f"Cliente: {req.pushName or 'sem nome'}\n"
        f"WhatsApp: {req.number}\n"
        f"Motivo: {reply.handoff_reason or 'não informado'}\n"
        f"Mensagem: {req.text}"
    )
    return await send_evolution_text(EMILIANO_PHONE, alert)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "carolinne-worker",
        "version": app.version,
        "redis": bool(store.client),
        "agent_mode": AGENT_MODE,
        "llm_configured": bool(LLM_API_KEY),
        "llm_model": LLM_MODEL if LLM_API_KEY else None,
        "brain_loaded": bool(CAROLINNE_BRAIN),
        "direct_evolution_configured": bool(EVOLUTION_API_URL and EVOLUTION_API_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
        "supabase_table": SUPABASE_CLIENTS_TABLE if SUPABASE_URL else None,
        "evolution_instance": EVOLUTION_INSTANCE_NAME,
        "short_human_style": True,
    }


@app.get("/customer/{number}")
async def get_customer_debug(number: str, x_api_key: Optional[str] = Header(default=None), api_key: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    check_api_key(x_api_key, api_key)
    clean = re.sub(r"\D", "", number)
    return {
        "number": clean,
        "redis": store.get_customer(clean),
        "supabase": await supabase_customer(clean),
    }



@app.post("/webhook/evolution", response_model=WorkerReply)
async def webhook_evolution(req: EvolutionInbound, x_api_key: Optional[str] = Header(default=None)) -> WorkerReply:
    check_api_key(x_api_key)
    return await reply_for(req)


@app.post("/evolution/inbound")
async def evolution_inbound_direct(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Direct Evolution webhook endpoint.

    Point Evolution API webhook here if you want to bypass n8n:
    https://WORKER_URL/evolution/inbound?api_key=123456

    The worker will normalize the inbound event, generate the Carolinne reply, send it
    through Evolution API, and send Emiliano a handoff alert when needed.
    """
    check_api_key(x_api_key, api_key)
    body = await request.json()
    req = normalize_evolution_payload(body)
    if not req.text or not req.number:
        return {"ok": True, "ignored": True, "reason": "no text or number"}
    if (body.get("data") or body).get("key", {}).get("fromMe"):
        return {"ok": True, "ignored": True, "reason": "fromMe"}
    reply = await reply_for(req)
    send_result = await send_evolution_text(req.number, reply.reply_text)
    handoff_result = None
    try:
        handoff_result = await notify_human(req, reply)
    except Exception as exc:
        handoff_result = {"error": str(exc)[:300]}
    return {
        "ok": True,
        "sent": True,
        "to": req.number,
        "intent": reply.intent,
        "human_handoff": reply.human_handoff,
        "send_result": send_result,
        "handoff_result": handoff_result,
    }


@app.post("/agent/respond", response_model=WorkerReply)
async def agent_respond(req: EvolutionInbound, x_api_key: Optional[str] = Header(default=None)) -> WorkerReply:
    """Pure API agent endpoint: receive normalized text and return Carolinne's answer JSON."""
    check_api_key(x_api_key)
    return await reply_for(req)


@app.post("/preview", response_model=WorkerReply)
async def preview(req: EvolutionInbound) -> WorkerReply:
    return await reply_for(req)


@app.get("/customers/{number}")
def get_customer(number: str, x_api_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    check_api_key(x_api_key)
    record = store.get_customer(number)
    if not record:
        raise HTTPException(status_code=404, detail="customer not found")
    return record
