import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

try:
    import redis
except Exception:  # pragma: no cover
    redis = None

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
APP_NAME = os.getenv("BOT_NAME", "Carolinne")
API_KEY = os.getenv("CAROLINNE_WORKER_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "")
TEST_LINK = os.getenv("TEST_LINK") or os.getenv(
    "CHATBOT_URL",
    "https://sstv.center/chatbot/98d64bd0-1b1d-4343-9896-a635bbd600f5",
)
PAYMENT_SITE_URL = os.getenv("PAYMENT_SITE_URL", "")
PIX_KEY = os.getenv("PIX_KEY", "03186401046")

# Supabase — banco permanente de clientes
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv(
    "SUPABASE_ANON_KEY", ""
)
SUPABASE_CLIENTS_TABLE = os.getenv("SUPABASE_CLIENTS_TABLE", "clientes")
SUPABASE_PHONE_COLUMN = os.getenv("SUPABASE_PHONE_COLUMN", "whatsapp")
SUPABASE_HANDOFFS_TABLE = os.getenv("SUPABASE_HANDOFFS_TABLE", "handoffs")

# Evolution API (envio direto, sem depender do n8n)
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME") or os.getenv(
    "EVOLUTION_INSTANCE", "liberoutv"
)
EMILIANO_PHONE = os.getenv("EMILIANO_PHONE") or os.getenv(
    "EMILIANO_WHATSAPP_NUMBER", ""
)

# LLM / agente por API. Se LLM_API_KEY estiver vazio, usa o fallback de regras.
AGENT_MODE = os.getenv("AGENT_MODE", "hybrid").lower()  # hybrid | llm | rules
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()  # anthropic | openai
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or os.getenv(
    "OPENAI_BASE_URL", "https://api.anthropic.com/v1"
)
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL", "claude-sonnet-4-5-20250929")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "18"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.4"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "300"))
BRAIN_PATH = Path(os.getenv("CAROLINNE_BRAIN_PATH", "carolinne_brain.md"))

# Rate limiting — mensagens por número numa janela
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "20"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "3600"))  # segundos

# RPA SSTV — automação de geração de teste no painel sstv.center
SSTV_RPA_ENABLED = os.getenv("SSTV_RPA_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Logging estruturado
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("carolinne.worker")


def log_event(event: str, **fields: Any) -> None:
    """Log estruturado em uma linha, fácil de parsear / mandar pra Loki/CloudWatch."""
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


app = FastAPI(title="Carolinne Worker", version="3.0.0")


# ---------------------------------------------------------------------------
# Cérebro
# ---------------------------------------------------------------------------
def load_brain() -> str:
    try:
        return BRAIN_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        log_event("brain_load_failed", error=str(exc)[:200])
        return """
Você é Carolinne, atendente da Liberou TV. Planos: mensal R$50, trimestral R$130, anual R$381,95.
PIX 03186401046. Teste: NUNCA envie link interno de geração; diga que vai gerar por aqui e
peça aparelho/nome se necessário. Suporte: orientar roteador 5-6 minutos. Nunca confirme
pagamento; escale comprovante/humano/suporte complexo.
""".strip()


CAROLINNE_BRAIN = load_brain()


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
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
    # Mensagens extras enviadas ANTES da reply_text (ex.: cumprimento + boas-vindas
    # separados em balões diferentes, como uma humana faria).
    extra_messages: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Store (Redis com fallback em memória) + métricas
# ---------------------------------------------------------------------------
class Metrics:
    """Contadores em memória para o endpoint /metrics. Não persistem entre restarts.

    Para série histórica, exporte para o Redis/Supabase ou um coletor externo.
    """

    def __init__(self) -> None:
        self.intents: Counter = Counter()
        self.handoffs: Counter = Counter()
        self.engines: Counter = Counter()
        self.latency_buckets: List[float] = []
        self.started_at = datetime.now(timezone.utc)

    def record(self, intent: str, engine: str, handoff: bool, latency: float) -> None:
        self.intents[intent] += 1
        self.engines[engine] += 1
        if handoff:
            self.handoffs[intent] += 1
        self.latency_buckets.append(latency)
        if len(self.latency_buckets) > 1000:
            self.latency_buckets = self.latency_buckets[-1000:]

    def snapshot(self) -> Dict[str, Any]:
        lat = self.latency_buckets
        avg = round(sum(lat) / len(lat), 3) if lat else 0.0
        return {
            "started_at": self.started_at.isoformat(),
            "total_messages": sum(self.intents.values()),
            "by_intent": dict(self.intents),
            "by_engine": dict(self.engines),
            "handoffs": dict(self.handoffs),
            "total_handoffs": sum(self.handoffs.values()),
            "latency_avg_ms": avg,
            "latency_max_ms": round(max(lat), 3) if lat else 0.0,
        }


class Store:
    def __init__(self) -> None:
        self.memory: Dict[str, Dict[str, Any]] = {}
        self.client = None
        if REDIS_URL and redis:
            try:
                self.client = redis.from_url(REDIS_URL, decode_responses=True)
                self.client.ping()
                logger.info("[Store] Redis connected")
            except Exception as exc:
                logger.warning("[Store] Redis unavailable: %s", exc)
                self.client = None

    # ---- helpers de baixo nível ----
    def _get_json(self, key: str) -> Dict[str, Any]:
        if self.client:
            raw = self.client.get(key)
            return json.loads(raw) if raw else {}
        return self.memory.get(key, {})

    def _set_json(self, key: str, value: Dict[str, Any]) -> None:
        if self.client:
            self.client.set(key, json.dumps(value, ensure_ascii=False))
        else:
            self.memory[key] = value

    # ---- rate limiting ----
    def rate_check(self, number: str) -> tuple[bool, int]:
        """Retorna (permitido, contagem_atual). Janela deslizante simples."""
        if not number:
            return True, 0
        key = f"rate:{number}"
        if self.client:
            count = int(self.client.incr(key))
            if count == 1:
                self.client.expire(key, RATE_LIMIT_WINDOW)
            return count <= RATE_LIMIT_MAX, count
        bucket = self.memory.setdefault(key, {"count": 0, "ts": time.time()})
        now = time.time()
        if now - bucket["ts"] > RATE_LIMIT_WINDOW:
            bucket.update(count=0, ts=now)
        bucket["count"] += 1
        return bucket["count"] <= RATE_LIMIT_MAX, bucket["count"]

    # ---- fallback de intents não resolvidas ----
    def inc_fallback(self, number: str) -> int:
        key = f"fallback:{number}"
        if self.client:
            value = self.client.incr(key)
            self.client.expire(key, 3600)
            return int(value)
        bucket = self.memory.setdefault(key, {"count": 0})
        bucket["count"] += 1
        return int(bucket["count"])

    def reset_fallback(self, number: str) -> None:
        key = f"fallback:{number}"
        if self.client:
            self.client.delete(key)
        else:
            self.memory.pop(key, None)

    # ---- estado de conversa (máquina de estados simples) ----
    def get_state(self, number: str) -> str:
        return str(self._get_json(f"state:{number}").get("await", ""))

    def set_state(self, number: str, await_state: str) -> None:
        self._set_json(f"state:{number}", {"await": await_state, "at": datetime.now(timezone.utc).isoformat()})

    def clear_state(self, number: str) -> None:
        key = f"state:{number}"
        if self.client:
            self.client.delete(key)
        else:
            self.memory.pop(key, None)

    # ---- aparelho escolhido pelo cliente (usado no fluxo de teste) ----
    def set_device(self, number: str, device: str) -> None:
        record = self._get_json(f"customer:{number}")
        record["device"] = device
        self._set_json(f"customer:{number}", record)

    def get_device(self, number: str) -> Optional[str]:
        return self._get_json(f"customer:{number}").get("device")

    # ---- cliente ----
    def get_customer(self, number: str) -> Dict[str, Any]:
        return self._get_json(f"customer:{number}")

    def save_interaction(self, req: EvolutionInbound, reply: WorkerReply) -> None:
        if not req.number:
            return
        now = datetime.now(timezone.utc).isoformat()
        key = f"customer:{req.number}"
        record = self._get_json(key)
        history = record.get("history", [])[-19:]
        history.append({
            "at": now,
            "message_id": req.messageId,
            "user_text": req.text,
            "reply_text": reply.reply_text,
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
metrics = Metrics()

# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------
INTENTS: Dict[str, List[str]] = {
    "gerar_teste": ["teste", "testar", "experimentar", "gratis", "grátis", "trial"],
    "planos": ["plano", "preço", "preco", "valor", "quanto", "mensal", "trimestral", "anual", "semestral", "seis meses", "6 meses"],
    "pix": ["pix", "reais", "real", "brl", "chave", "pagar em reais"],
    "pagamento_internacional": ["dolar", "dólar", "euro", "cartao", "cartão", "paypal", "moeda", "internacional", "canada", "canadá", "usa", "eua", "usd", "cad", "aud"],
    "comprovante": ["comprovante", "paguei", "pagamento feito", "transferi", "enviei", "recibo"],
    "suporte": ["suporte", "travando", "não funciona", "nao funciona", "caiu", "sem sinal", "tela preta", "erro", "cliente"],
    "apps": ["app", "aplicativo", "fire", "firestick", "downloader", "instalar", "instalação", "roku", "samsung", "lg", "iphone", "ipad", "android", "tv box", "vizzion", "xcloud", "stv", "smarters", "ss player", "ibo", "max player", "apple tv", "smart tv"],
    "canais": ["canais", "canal", "filmes", "series", "séries", "adultos", "futebol", "novela", "globo", "sportv", "premiere"],
    "humano": ["humano", "atendente", "emiliano", "responsável", "responsavel", "falar com alguém", "falar com alguem", "ligação", "ligacao", "audio", "áudio"],
}

# Palavras de negação: quando presentes, não devem disparar o intent associado.
NEGATIONS = ["não ", "nao ", "num ", "n quero", "não quero", "nao quero", "cancelar", "sem "]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


# Palavras que indicam aparelho (usado pra desambiguar teste + aparelho)
DEVICE_KEYWORDS = [
    "fire", "firestick", "fire stick", "apple tv", "roku", "samsung", "lg ",
    "iphone", "ipad", "android", "celular", "telefone", "tv box", "android tv",
    "google tv", "smart tv", "max player", "vizzion", "xcloud", "smarters",
]


def detect_device(text: str) -> Optional[str]:
    """Retorna o nome do aparelho reconhecido na mensagem, ou None."""
    t = normalize(text)
    if "apple tv" in t:
        return "Apple TV"
    if "fire" in t or "firestick" in t:
        return "Fire Stick"
    if "roku" in t:
        return "Roku"
    if "samsung" in t:
        return "Samsung"
    if "lg " in t or t.startswith("lg") or "lg." in t:
        return "LG"
    if "iphone" in t or "ipad" in t:
        return "iPhone/iPad"
    if "android tv" in t or "google tv" in t or "tv box" in t:
        return "Android TV/TV Box"
    if "celular" in t or "telefone" in t or ("android" in t and "tv" not in t):
        return "Celular Android"
    if "smart tv" in t:
        return "Smart TV"
    return None


def detect_intent(text: str) -> str:
    t = normalize(text)
    if not t:
        return "fallback"
    # Atalhos numéricos do menu
    if t in {"1", "01"}:
        return "gerar_teste"
    if t in {"2", "02"}:
        return "planos"
    if t in {"3", "03"}:
        return "suporte"
    if t in {"4", "04"}:
        return "pix"
    if t in {"5", "05"}:
        return "pagamento_internacional"
    if t in {"6", "06"}:
        return "humano"
    # Saudação apenas para mensagens curtas
    greeting_patterns = [
        r"^oi[!,. ]*$",
        r"^olá[!,. ]*$",
        r"^ola[!,. ]*$",
        r"^bom dia[!,. ]*$",
        r"^boa tarde[!,. ]*$",
        r"^boa noite[!,. ]*$",
    ]
    if len(t) <= 35 and any(re.search(p, t) for p in greeting_patterns):
        return "saudacao"

    # Detecta se a mensagem menciona um aparelho. Se sim, prioriza "apps"
    # em vez de "gerar_teste" — evita o loop de perguntar o aparelho de novo.
    has_device = any(k in t for k in DEVICE_KEYWORDS) or detect_device(t) is not None

    # Matching por palavra-chave com filtro de negação
    for intent, keywords in INTENTS.items():
        if intent == "gerar_teste" and has_device:
            # Cliente misturou teste + aparelho ("quero teste no fire stick"):
            # tratar como apps pra responder sobre o aparelho.
            continue
        if any(k in t for k in keywords):
            if any(neg in t for neg in NEGATIONS):
                continue
            return intent
    return "fallback"


# ---------------------------------------------------------------------------
# Helpers de conteúdo
# ---------------------------------------------------------------------------
def is_new_conversation(number: str) -> bool:
    """Detecta se é o primeiro contato desse número (sem histórico salvo)."""
    if not number:
        return True
    record = store.get_customer(number)
    history = record.get("history", []) if record else []
    return len(history) == 0


def greeting_for(req: EvolutionInbound) -> WorkerReply:
    """Saudação humana. Em conversa nova, quebra em 2 balões separados
    (cumprimento + boas-vindas) como uma pessoa faria no WhatsApp."""
    first = (req.pushName or "").split(" ")[0]
    who = f", {first}" if first else ""

    # Saudação por horário local do cliente
    tz_name = infer_timezone(req.number, req.text)
    try:
        hour = datetime.now(ZoneInfo(tz_name)).hour
    except Exception:
        hour = datetime.now(ZoneInfo("America/Toronto")).hour
    if 5 <= hour < 12:
        period = "Bom dia"
    elif 12 <= hour < 18:
        period = "Boa tarde"
    else:
        period = "Boa noite"

    # Primeiro contato: 2 mensagens separadas (humano), sem textão.
    if is_new_conversation(req.number):
        return WorkerReply(
            intent="saudacao",
            extra_messages=[f"{period}{who}! Tudo bem?"],
            reply_text=(
                "Bem-vindo à Liberou TV 😊\n"
                "Me fala rapidinho: você quer teste, planos ou suporte?"
            ),
        )

    # Cliente que já conversou antes: resposta curta, um balão só.
    return WorkerReply(
        intent="saudacao",
        reply_text=f"{period}{who}! Tudo bem?\nO que posso te ajudar hoje?",
    )


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


def device_reply(text: str, number: str = "") -> str:
    t = normalize(text)
    tz = infer_timezone(number, text)
    usa_canada = tz in {"America/New_York", "America/Toronto"} or any(
        w in t for w in ["eua", "usa", "estados unidos", "canada", "canadá"]
    )
    if "apple tv" in t:
        return "Na Apple TV usamos o Max Player.\nProcura Max Player na App Store da Apple TV e me avisa se encontrou."
    if "fire" in t or "android tv" in t or "google tv" in t or "tv box" in t:
        code = "952155 ou 5269346" if usa_canada else "441676 ou 4618458"
        app = "STV.1 Auto Update" if usa_canada else "STV Smarters"
        return f"Perfeito. Nesse aparelho usamos o Downloader.\n\nBaixa o Downloader e digita o código {code}.\nEle vai instalar o {app}."
    if "iphone" in t or "ipad" in t:
        return "No iPhone/iPad pode usar Vizzion Play ou XCloud.\nSe abrir o XCloud, coloca o provider: LiberouTV"
    if "roku" in t or "lg" in t or "samsung" in t:
        return "Nesse modelo a prioridade é Vizzion Play.\nProcura Vizzion Play na loja da TV e me avisa se encontrou."
    if "android" in t or "celular" in t or "telefone" in t:
        link = "https://sdev.cx/stvnovo.apk" if usa_canada else "https://sdev.cx/stv.apk"
        return f"No telefone Android é mais simples.\nAbre esse link nele:\n{link}"
    return "Me diz o aparelho certinho.\nÉ Fire Stick, Apple TV, Roku, LG, Samsung, Android ou iPhone?"


# ---------------------------------------------------------------------------
# Fallback por regras (usado quando não há LLM ou ele falha)
# ---------------------------------------------------------------------------
def rules_reply(req: EvolutionInbound) -> WorkerReply:
    intent = detect_intent(req.text)
    if intent != "fallback":
        store.reset_fallback(req.number)

    if intent == "saudacao":
        return greeting_for(req)

    # Fluxo de teste: prioriza detectar o aparelho, evita loop de perguntar de novo.
    await_state = store.get_state(req.number)
    device = detect_device(req.text)
    requested_test_before = bool(store.get_customer(req.number).get("requested_test"))

    # Caso A: cliente já tinha pedido teste e agora respondeu o aparelho.
    if await_state == "await_device" and device:
        store.clear_state(req.number)
        store.set_device(req.number, device)
        return WorkerReply(
            intent="gerar_teste",
            human_handoff=True,
            handoff_reason=f"cliente pediu teste para {device}; acionar ativação/RPA",
            reply_text=(
                f"Perfeito 😊 Vou gerar seu teste pra {device}.\n"
                "Só um instante que já te mando os dados."
            ),
            metadata={"device": device, "test_ready_to_generate": True},
        )

    # Caso B: cliente pede teste mencionando o aparelho de cara ("teste no fire stick").
    if intent == "gerar_teste" and device:
        store.set_device(req.number, device)
        return WorkerReply(
            intent="gerar_teste",
            human_handoff=True,
            handoff_reason=f"cliente pediu teste para {device}; acionar ativação/RPA",
            reply_text=(
                f"Perfeito 😊 Vou gerar seu teste pra {device}.\n"
                "Só um instante que já te mando os dados."
            ),
            metadata={"device": device, "test_ready_to_generate": True},
        )

    # Caso C: cliente pede teste sem dizer aparelho → pergunta e ativa estado.
    if intent == "gerar_teste":
        store.set_state(req.number, "await_device")
        return WorkerReply(
            intent=intent,
            human_handoff=False,
            reply_text="Claro 😊 Eu gero o teste por aqui.\nMe diz só em qual aparelho vai usar?",
            metadata={"await_state": "await_device"},
        )

    # Caso D: cliente respondeu o aparelho mas a frase virou intent "apps" (sem contexto de teste).
    # Se estava aguardando aparelho, registra e confirma.
    if await_state == "await_device" and intent == "apps" and device:
        store.clear_state(req.number)
        store.set_device(req.number, device)
        return WorkerReply(
            intent="gerar_teste",
            human_handoff=True,
            handoff_reason=f"cliente pediu teste para {device}; acionar ativação/RPA",
            reply_text=(
                f"Perfeito 😊 Vou gerar seu teste pra {device}.\n"
                "Só um instante que já te mando os dados."
            ),
            metadata={"device": device, "test_ready_to_generate": True},
        )
    if intent == "planos":
        return WorkerReply(intent=intent, reply_text="Claro. Você está em qual país?\nAí te passo os valores na moeda certinha.")
    if intent == "pix":
        return WorkerReply(
            intent=intent,
            reply_text=(
                f"Para finalizar seu acesso, segue os dados do Pix:\n"
                f"🇧🇷 Chave Pix (CPF): {PIX_KEY}\n"
                f"👤 Nome: Emiliano Louzada de Oliveira\n"
                f"✅ Assim que o pagamento for confirmado, seu acesso é ativado na hora!\n"
                f"📲 Me manda o comprovante aqui no WhatsApp para agilizar!\n"
                f"Obrigado pela confiança! 🙏"
            ),
        )
    if intent == "pagamento_internacional":
        return WorkerReply(
            intent=intent,
            reply_text=(
                "Esse tipo de pagamento é pelo nosso site:\nwww.liberoutv.com\n\n"
                "Entra no site, clica no seu país, toca em Acessar Agora e escolhe o plano.\n"
                "Depois me manda o comprovante aqui."
            ),
        )
    if intent == "comprovante":
        return WorkerReply(
            intent=intent,
            human_handoff=True,
            handoff_reason="cliente enviou/avisou comprovante",
            reply_text="Recebi, obrigada.\nVou encaminhar para conferência e ativação. Assim que estiver tudo certo, avisamos por aqui.",
        )
    if intent == "suporte":
        return WorkerReply(
            intent=intent,
            reply_text=(
                "Vamos começar pelo principal.\n"
                "Tira o roteador da tomada por 5 a 6 minutos, não menos.\n"
                "Depois liga de novo, fecha o app totalmente e abre novamente."
            ),
        )
    if intent == "apps":
        return WorkerReply(intent=intent, reply_text=device_reply(req.text, req.number))
    if intent == "canais":
        return WorkerReply(
            intent=intent,
            reply_text=(
                "Tem mais de 9 mil canais.\nBrasil completo, EUA, Canadá, Europa, filmes, séries, 24h e adultos.\n\n"
                "Quer que eu gere um teste pra você ver na prática?"
            ),
        )
    if intent == "humano":
        return WorkerReply(
            intent=intent,
            human_handoff=True,
            handoff_reason="cliente pediu atendimento humano",
            reply_text="Claro. Vou direcionar seu atendimento para o setor responsável.",
        )

    count = store.inc_fallback(req.number)
    if count >= 2:
        return WorkerReply(
            intent="fallback",
            human_handoff=True,
            handoff_reason="fallback repetido",
            reply_text="Certo. Vou direcionar seu atendimento para o setor responsável.",
        )
    return WorkerReply(
        intent="fallback",
        reply_text="Me explica rapidinho o que você precisa?\nÉ teste, pagamento ou suporte?",
    )


# ---------------------------------------------------------------------------
# Utilidades de LLM
# ---------------------------------------------------------------------------
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
    compact = dict(record)
    compact["history"] = compact.get("history", [])[-8:]
    compact["await_state"] = store.get_state(number)
    return compact


async def supabase_request(method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None, extra_headers: Optional[dict] = None) -> Dict[str, Any]:
    """Chamada genérica à REST API do Supabase. Retorna {} em caso de falha."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {}
    url = SUPABASE_URL.rstrip("/") + path
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.request(method, url, headers=headers, params=params, json=json_body)
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return {}
    except Exception as exc:
        log_event("supabase_error", method=method, path=path, error=str(exc)[:200])
        return {}


async def supabase_customer(number: str) -> Dict[str, Any]:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and number):
        return {}
    clean = re.sub(r"\D", "", number)
    rows = await supabase_request(
        "GET",
        f"/rest/v1/{SUPABASE_CLIENTS_TABLE}",
        params={"select": "*", SUPABASE_PHONE_COLUMN: f"eq.{clean}", "limit": "1"},
    )
    if isinstance(rows, list) and rows:
        return rows[0]
    return {}


async def supabase_upsert_customer(number: str, fields: Dict[str, Any]) -> None:
    """Write-back leve de dados do cliente no Supabase (upsert por telefone).

    Usa o header Prefer do PostgREST para resolver conflito pela coluna de telefone.
    A tabela precisa ter UNIQUE/PK na coluna de telefone para o upsert funcionar.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and number):
        return
    clean = re.sub(r"\D", "", number)
    body = {SUPABASE_PHONE_COLUMN: clean, **fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    await supabase_request(
        "POST",
        f"/rest/v1/{SUPABASE_CLIENTS_TABLE}",
        json_body=body,
        extra_headers={"Prefer": f"resolution=merge-duplicates,return=representation,on_conflict={SUPABASE_PHONE_COLUMN}"},
    )


async def supabase_insert_handoff(req: EvolutionInbound, reply: WorkerReply) -> None:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return
    await supabase_request(
        "POST",
        f"/rest/v1/{SUPABASE_HANDOFFS_TABLE}",
        json_body={
            "number": req.number,
            "name": req.pushName,
            "intent": reply.intent,
            "reason": reply.handoff_reason,
            "message": req.text,
            "status": "pendente",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def local_time_context(req: EvolutionInbound) -> Dict[str, str]:
    tz_name = infer_timezone(req.number, req.text)
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        tz_name = "America/Toronto"
        now = datetime.now(ZoneInfo(tz_name))
    hour = now.hour
    if 5 <= hour < 12:
        greet = "Bom dia"
    elif 12 <= hour < 18:
        greet = "Boa tarde"
    else:
        greet = "Boa noite"
    return {
        "timezone": tz_name,
        "local_time": now.strftime("%Y-%m-%d %H:%M"),
        "suggested_greeting": greet,
    }


# ---------------------------------------------------------------------------
# Filtro de segurança (consolidado — único sanitize_reply)
# ---------------------------------------------------------------------------
INTERNAL_LINK_PATTERNS = [
    r"https?://sstv\.center/chatbot/\S*",
    r"sstv\.center/chatbot/\S*",
    r"98d64bd0-1b1d-4343-9896-a635bbd600f5",
    r"https?://[^\s]*(?:autoreply|auto[-_]?reply|chatbot|gerar[-_]?teste)[^\s]*",
]

DECORATIVE_EMOJIS = "😊😉🙂😄😃😀😍🥰👍🙏🔥🚀📺🎬⚡💡🤝❤️❤"


def strip_decorative_emojis(text: str) -> str:
    for ch in DECORATIVE_EMOJIS:
        text = text.replace(ch, "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def recently_greeted(number: str) -> bool:
    record = store.get_customer(number) if number else {}
    history = record.get("history", [])[-5:] if record else []
    for item in reversed(history):
        r = normalize(item.get("reply_text", ""))
        if r.startswith(("oi", "bom dia", "boa tarde", "boa noite")):
            return True
    return False


def remove_repeated_greeting(text: str, req: EvolutionInbound) -> str:
    if not recently_greeted(req.number):
        return text
    stripped = re.sub(
        r"^(oi+[,! ]*|bom dia[,! ]*|boa tarde[,! ]*|boa noite[,! ]*)\n?",
        "",
        text.strip(),
        flags=re.I,
    ).strip()
    return stripped or text


def sanitize_reply(req: EvolutionInbound, reply: WorkerReply) -> WorkerReply:
    """Filtro de segurança final antes de salvar/enviar ao WhatsApp."""
    text = reply.reply_text or ""
    leaked = any(re.search(p, text, flags=re.I) for p in INTERNAL_LINK_PATTERNS)

    # BLOQUEIO DE SEGURANÇA — só age quando há LEAK REAL de link interno.
    # Antes reescrevia em todo "gerar_teste", o que causava loop. Agora respeita
    # a resposta do LLM/regras quando não há link vazado.
    if leaked:
        text = "Claro. Eu gero o teste por aqui.\nMe diz só em qual aparelho vai usar?"
        reply.intent = "gerar_teste"
        reply.human_handoff = True
        reply.handoff_reason = "IA tentou enviar link interno de teste (bloqueado pelo filtro de segurança)"
        reply.metadata["safety_filter"] = "blocked_internal_test_link"
    else:
        # Limpa qualquer padrão que tenha escapado (defesa em profundidade).
        for pattern in INTERNAL_LINK_PATTERNS:
            text = re.sub(pattern, "", text, flags=re.I).strip()

    t = normalize(req.text)
    if any(k in t for k in ["áudio", "audio", "ligação", "ligacao"]):
        text = "Não consigo atender ligação/áudio por aqui agora.\nMe manda por texto ou uma foto da tela que eu te ajudo rapidinho."
        reply.human_handoff = True
        reply.handoff_reason = reply.handoff_reason or "cliente enviou áudio/ligação"

    if reply.intent != "pix":
        text = strip_decorative_emojis(text)
    text = remove_repeated_greeting(text, req)

    if len(text) > 900 and not any(k in t for k in ["tutorial", "passo", "completo", "detalhe"]):
        text = text[:850].rsplit("\n", 1)[0].strip() + "\n\nSe quiser, continuo te guiando por partes."

    reply.reply_text = text or "Certo. Vou direcionar seu atendimento para o setor responsável."
    return reply


# ---------------------------------------------------------------------------
# LLM reply com retentativa
# ---------------------------------------------------------------------------
def build_llm_system_prompt() -> str:
    return f"""
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


def build_llm_user_payload(req: EvolutionInbound) -> str:
    user = {
        "customer": {
            "number": req.number,
            "name": req.pushName,
            "recent_memory": recent_context(req.number),
        },
        "message": req.text,
        "local_time": local_time_context(req),
        "rule_detected_intent": detect_intent(req.text),
        "conversation_state": store.get_state(req.number),
        "instruction": (
            "Responda como Carolinne com base no cérebro oficial. Seja curta, humana e útil. "
            "Use a saudação sugerida apenas se for início de conversa. Não mande textão; 1 a 4 linhas. "
            "Se precisar de humano, marque human_handoff=true."
        ),
    }
    return json.dumps(user, ensure_ascii=False)


async def call_llm_once(req: EvolutionInbound) -> WorkerReply:
    """Chamada ao LLM. Suporta Anthropic (Claude) e OpenAI como provider."""
    detected = detect_intent(req.text)
    system = build_llm_system_prompt()
    user = build_llm_user_payload(req)

    if LLM_PROVIDER == "anthropic":
        # ---- Claude / Anthropic API ----
        headers = {
            "x-api-key": LLM_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": LLM_MODEL,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        url = LLM_BASE_URL.rstrip("/") + "/messages"
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["content"][0]["text"]
    else:
        # ---- OpenAI / compatível ----
        headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": LLM_MODEL,
            "temperature": LLM_TEMPERATURE,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]

    parsed = safe_json_from_text(content)
    return WorkerReply(
        reply_text=str(parsed.get("reply_text") or "Desculpa, não consegui responder agora. Vou chamar o atendimento para ajudar."),
        intent=str(parsed.get("intent") or detected or "fallback"),
        human_handoff=bool(parsed.get("human_handoff", False)),
        handoff_reason=parsed.get("handoff_reason"),
        metadata=parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {},
    )


async def llm_reply(req: EvolutionInbound) -> WorkerReply:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY/OPENAI_API_KEY not configured")
    # Primeira tentativa
    try:
        return await call_llm_once(req)
    except Exception as first_err:
        log_event("llm_retry", number=req.number, error=str(first_err)[:200])
        # Segunda tentativa após pequeno backoff
        await asyncio.sleep(1.0)
        return await call_llm_once(req)


# ---------------------------------------------------------------------------
# Orquestração da resposta
# ---------------------------------------------------------------------------
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
            log_event("llm_failed_fallback_rules", number=req.number, error=str(exc)[:300])
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


async def reply_for(req: EvolutionInbound) -> WorkerReply:
    start = time.perf_counter()
    reply = await build_reply(req)
    reply = sanitize_reply(req, reply)
    store.save_interaction(req, reply)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    metrics.record(reply.intent, reply.metadata.get("agent_engine", "unknown"), reply.human_handoff, latency_ms)
    reply.metadata.update({"customer_saved": bool(req.number), "redis": bool(store.client), "latency_ms": latency_ms})
    log_event(
        "reply",
        number=req.number,
        intent=reply.intent,
        engine=reply.metadata.get("agent_engine"),
        handoff=reply.human_handoff,
        latency_ms=latency_ms,
    )
    return reply


# ---------------------------------------------------------------------------
# Envio Evolution + notificação humana
# ---------------------------------------------------------------------------
# Delay entre mensagens (simula digitação humana). Configurável via env.
MSG_DELAY_SECONDS = float(os.getenv("MSG_DELAY_SECONDS", "1.5"))


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


async def send_evolution_messages(number: str, messages: List[str]) -> List[Dict[str, Any]]:
    """Envia várias mensagens ao cliente com pausa entre elas.

    Simula comportamento humano:manda uma msg, espera, manda a próxima.
    A última mensagem da lista é a "principal" (sem delay após).
    """
    results = []
    for i, text in enumerate(messages):
        if i > 0:
            await asyncio.sleep(MSG_DELAY_SECONDS)
        try:
            result = await send_evolution_text(number, text)
            results.append(result)
        except Exception as exc:
            log_event("send_message_failed", number=number, index=i, error=str(exc)[:200])
            results.append({"error": str(exc)[:300]})
    return results


async def send_evolution_typing(number: str) -> None:
    """Envia indicador 'digitando' (presença) ao cliente."""
    if not (EVOLUTION_API_URL and EVOLUTION_API_KEY):
        return
    clean_number = re.sub(r"\D", "", number or "")
    if not clean_number:
        return
    url = EVOLUTION_API_URL.rstrip("/") + f"/chat/presence/{EVOLUTION_INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": clean_number, "presence": "composing"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, headers=headers, json=payload)
    except Exception as exc:
        log_event("typing_failed", number=clean_number, error=str(exc)[:150])


async def notify_human(req: EvolutionInbound, reply: WorkerReply) -> Optional[Dict[str, Any]]:
    if not reply.human_handoff or not EMILIANO_PHONE:
        return None
    await supabase_insert_handoff(req, reply)
    # Inclui o aparelho detectado quando existir (fluxo de teste).
    device = reply.metadata.get("device") or store.get_device(req.number)
    is_test = reply.intent == "gerar_teste" and reply.metadata.get("test_ready_to_generate")
    if is_test:
        alert = (
            "🧪 NOVO TESTE SOLICITADO\n"
            f"👤 Cliente: {req.pushName or 'sem nome'}\n"
            f"📱 WhatsApp: {req.number}\n"
            f"📺 Aparelho: {device or 'não informado'}\n"
            f"💬 Mensagem: {req.text}\n\n"
            f"➡️ Gere o teste no painel SSTV e envie as credenciais ao cliente."
        )
    else:
        alert = (
            "🚨 Carolinne pediu atendimento humano\n"
            f"Cliente: {req.pushName or 'sem nome'}\n"
            f"WhatsApp: {req.number}\n"
            f"Motivo: {reply.handoff_reason or 'não informado'}\n"
            f"Mensagem: {req.text}"
        )
        if device:
            alert += f"\nAparelho: {device}"
    return await send_evolution_text(EMILIANO_PHONE, alert)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def check_api_key(x_api_key: Optional[str], api_key_query: Optional[str] = None) -> None:
    provided = x_api_key or api_key_query
    if API_KEY and provided != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> Dict[str, Any]:
    """Health público e enxuto. Detalhes só com API key via /health?detailed=true."""
    if API_KEY:
        return {"status": "ok"}
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
        "evolution_instance": EVOLUTION_INSTANCE_NAME,
    }


@app.get("/health/detailed")
def health_detailed(x_api_key: Optional[str] = Header(default=None), api_key: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    check_api_key(x_api_key, api_key)
    return {
        "status": "ok",
        "service": "carolinne-worker",
        "version": app.version,
        "redis": bool(store.client),
        "agent_mode": AGENT_MODE,
        "llm_configured": bool(LLM_API_KEY),
        "llm_model": LLM_MODEL,
        "brain_loaded": bool(CAROLINNE_BRAIN),
        "direct_evolution_configured": bool(EVOLUTION_API_URL and EVOLUTION_API_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
        "supabase_table": SUPABASE_CLIENTS_TABLE,
        "evolution_instance": EVOLUTION_INSTANCE_NAME,
        "rate_limit": {"max": RATE_LIMIT_MAX, "window_s": RATE_LIMIT_WINDOW},
        "metrics": metrics.snapshot(),
    }


@app.get("/metrics")
def get_metrics(x_api_key: Optional[str] = Header(default=None), api_key: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    check_api_key(x_api_key, api_key)
    return metrics.snapshot()


@app.get("/customer/{number}")
async def get_customer_debug(
    number: str,
    x_api_key: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    check_api_key(x_api_key, api_key)
    clean = re.sub(r"\D", "", number)
    supa = await supabase_customer(clean)
    return {
        "number": clean,
        "redis": store.get_customer(clean),
        "supabase": supa,
    }


@app.get("/customers/{number}")
def get_customer(number: str, x_api_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    check_api_key(x_api_key)
    record = store.get_customer(number)
    if not record:
        raise HTTPException(status_code=404, detail="customer not found")
    return record


@app.post("/webhook/evolution", response_model=WorkerReply)
async def webhook_evolution(req: EvolutionInbound, x_api_key: Optional[str] = Header(default=None)) -> WorkerReply:
    check_api_key(x_api_key)
    allowed, count = store.rate_check(req.number)
    if not allowed:
        log_event("rate_limited", number=req.number, count=count)
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    return await reply_for(req)


@app.post("/agent/respond", response_model=WorkerReply)
async def agent_respond(req: EvolutionInbound, x_api_key: Optional[str] = Header(default=None)) -> WorkerReply:
    """Agente puro: recebe texto normalizado e devolve JSON da Carolinne."""
    check_api_key(x_api_key)
    allowed, count = store.rate_check(req.number)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    return await reply_for(req)


@app.post("/preview", response_model=WorkerReply)
async def preview(req: EvolutionInbound, x_api_key: Optional[str] = Header(default=None)) -> WorkerReply:
    """Preview de resposta. Protegido por API key."""
    check_api_key(x_api_key)
    return await reply_for(req)


def normalize_evolution_payload(body: Dict[str, Any]) -> EvolutionInbound:
    data = body.get("data") or body
    msg = data.get("message") or data.get("messages") or {}
    key = data.get("key") or msg.get("key") or {}
    raw_jid = (
        key.get("remoteJid")
        or data.get("remoteJid")
        or (data.get("key") or {}).get("remoteJid")
        or ""
    )
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


@app.post("/evolution/inbound")
async def evolution_inbound_direct(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Webhook direto da Evolution API (sem n8n).

    Aponte o webhook da Evolution para:
      https://WORKER_URL/evolution/inbound?api_key=...
    """
    check_api_key(x_api_key, api_key)
    body = await request.json()
    req = normalize_evolution_payload(body)
    if not req.text or not req.number:
        return {"ok": True, "ignored": True, "reason": "no text or number"}
    if (body.get("data") or body).get("key", {}).get("fromMe"):
        return {"ok": True, "ignored": True, "reason": "fromMe"}

    allowed, count = store.rate_check(req.number)
    if not allowed:
        log_event("rate_limited", number=req.number, count=count)
        return {"ok": True, "ignored": True, "reason": "rate_limited", "count": count}

    # Write-back leve de cliente no Supabase (assíncrono, sem bloquear resposta)
    safe_name = {"name": req.pushName} if req.pushName else {}
    if safe_name:
        await supabase_upsert_customer(req.number, safe_name)

    # Indicador "digitando" para o cliente não achar que travou
    await send_evolution_typing(req.number)

    reply = await reply_for(req)

    # Se há mensagens extras (ex.: cumprimento + boas-vindas), envia com pausa
    # entre cada uma, e a reply_text é a última. Se não, envia reply_text direto.
    all_messages = list(reply.extra_messages) + [reply.reply_text]
    if len(all_messages) > 1:
        send_result = await send_evolution_messages(req.number, all_messages)
    else:
        send_result = await send_evolution_text(req.number, reply.reply_text)

    handoff_result = None
    try:
        handoff_result = await notify_human(req, reply)
    except Exception as exc:
        handoff_result = {"error": str(exc)[:300]}

    # ---- RPA SSTV (geração automática de teste) ----
    # Se o cliente pediu teste E já tem aparelho E o RPA está ativado,
    # dispara o robô em background e envia credenciais ao cliente quando pronto.
    rpa_result = None
    if (
        reply.intent == "gerar_teste"
        and reply.metadata.get("test_ready_to_generate")
        and SSTV_RPA_ENABLED
    ):
        device = reply.metadata.get("device") or store.get_device(req.number)
        try:
            from sstv_rpa import gerar_teste_sstv, formatar_mensagem_cliente

            # Avisa o cliente que está gerando
            await send_evolution_text(
                req.number,
                "Estou gerando seu teste agora 🔧 Só um minutinho...",
            )
            log_event("rpa_started", number=req.number, device=device)

            # Roda o robô (síncrono dentro do request — pode levar 30-90s)
            result = await gerar_teste_sstv(
                device=device or "",
                cliente_nome=req.pushName or "",
                cliente_numero=req.number,
            )

            if result.success and result.username and result.password:
                # Envia as credenciais formatadas pro aparelho do cliente
                mensagens = formatar_mensagem_cliente(result, device or "")
                if mensagens:
                    await send_evolution_messages(req.number, mensagens)
                log_event("rpa_success", number=req.number, device=device)
                rpa_result = {"success": True, "username": result.username}
                # Atualiza o record do cliente
                record = store.get_customer(req.number)
                record["test_username"] = result.username
                record["test_generated_at"] = datetime.now(timezone.utc).isoformat()
                store._set_json(f"customer:{req.number}", record)
                # Avisa o Emiliano que o teste foi gerado automaticamente
                if EMILIANO_PHONE:
                    await send_evolution_text(
                        EMILIANO_PHONE,
                        f"✅ Teste gerado automaticamente\n"
                        f"Cliente: {req.pushName or 'sem nome'}\n"
                        f"WhatsApp: {req.number}\n"
                        f"Aparelho: {device or 'não informado'}\n"
                        f"Usuário: {result.username}",
                    )
            else:
                # RPA falhou — avisa o cliente e escala pro Emiliano
                log_event("rpa_failed", number=req.number, device=device, error=result.error)
                await send_evolution_text(
                    req.number,
                    "Tive um probleminha pra gerar seu teste automaticamente 😕 "
                    "Já avisei o setor e vou te enviar os dados o mais rápido possível!",
                )
                if EMILIANO_PHONE:
                    await send_evolution_text(
                        EMILIANO_PHONE,
                        f"⚠️ RPA FALHOU — gerar teste manualmente\n"
                        f"Cliente: {req.pushName or 'sem nome'}\n"
                        f"WhatsApp: {req.number}\n"
                        f"Aparelho: {device or 'não informado'}\n"
                        f"Erro: {result.error}",
                    )
                rpa_result = {"success": False, "error": result.error}
        except Exception as exc:
            log_event("rpa_error", number=req.number, error=str(exc)[:300])
            rpa_result = {"success": False, "error": str(exc)[:300]}

    return {
        "ok": True,
        "sent": True,
        "to": req.number,
        "intent": reply.intent,
        "human_handoff": reply.human_handoff,
        "messages_sent": len(all_messages),
        "send_result": send_result,
        "handoff_result": handoff_result,
        "rpa_result": rpa_result,
    }
