"""Auto-translation: detect FR / ES / PT questions, map programme terms to English for
retrieval, and add a note in the student's language. Translating the full answer needs AI."""
from __future__ import annotations

import re
import unicodedata

STOP = {
    "fr": set("le la les des de du est une un pour que qui comment pourquoi je j mon ma vous nous avec dans quel quelle "
              "quels quelles sur combien quand faut il elle ce cette ces mes sont pas au aux et ou en d l qu dois "
              "puis faire on".split()),
    "es": set("el la los las es una un para que qué cómo como por mi tengo usted con en cuál cuáles sobre cuántos "
              "cuándo hay debo del al son no de y o se lo puedo hacer".split()),
    "pt": set("o a os as é uma um para que como por meu minha você com em qual quais sobre quantos quando devo "
              "do da dos das são não ao de e ou se posso fazer".split()),
}
DISTINCT = {
    "fr": set("est vous nous quelle quelles quels combien quand faut cette mes sont pourquoi comment dois".split()),
    "es": set("cómo usted cuál cuáles cuántos cuándo debo hay tengo qué".split()),
    "pt": set("você qual quais quantos quando devo são não meu minha é".split()),
}

GLOSSARY: dict[str, dict[str, str]] = {
    "fr": {"taux de présence": "show rate", "taux de réponse": "reply rate", "relance": "follow-up", "relances": "follow-ups",
           "rendez-vous": "booked call", "appel": "call", "appels": "calls", "prix": "price", "webinaire": "webinar",
           "publicité": "ad", "publicités": "ads", "créatif": "creative", "créatifs": "creatives", "règles": "rules",
           "règle": "rule", "rappel": "reminder", "rappels": "reminders", "combien": "how many", "quand": "when",
           "dôme du tonnerre": "thunder dome", "objection": "objection", "offre": "offer", "niche": "niche",
           "message privé": "DM", "messages privés": "DMs", "coût par lead": "cost per lead", "clôture": "close",
           "proposition de valeur": "USP", "formule": "formula", "absent": "no-show", "absents": "no-shows",
           "envoyer": "send", "avant": "before", "après": "after", "meilleur": "best", "client": "client",
           "ventes": "sales", "partenaire": "partner", "cher": "expensive", "trop": "too", "gagnant": "winner"},
    "es": {"tasa de asistencia": "show rate", "tasa de respuesta": "reply rate", "seguimiento": "follow-up",
           "llamada": "call", "llamadas": "calls", "precio": "price", "seminario web": "webinar", "webinar": "webinar",
           "anuncio": "ad", "anuncios": "ads", "creativo": "creative", "creativos": "creatives", "reglas": "rules",
           "regla": "rule", "recordatorio": "reminder", "recordatorios": "reminders", "cuántos": "how many",
           "cuándo": "when", "cupula del trueno": "thunder dome", "oferta": "offer", "nicho": "niche",
           "mensaje directo": "DM", "costo por lead": "cost per lead", "fórmula": "formula", "inasistencia": "no-show",
           "enviar": "send", "antes": "before", "después": "after", "mejor": "best", "cliente": "client",
           "ventas": "sales", "pareja": "partner", "socio": "partner", "caro": "expensive", "ganador": "winner"},
    "pt": {"taxa de comparecimento": "show rate", "taxa de resposta": "reply rate", "acompanhamento": "follow-up",
           "chamada": "call", "chamadas": "calls", "ligação": "call", "preço": "price", "anúncio": "ad", "anúncios": "ads",
           "criativo": "creative", "criativos": "creatives", "regras": "rules", "regra": "rule", "lembrete": "reminder",
           "lembretes": "reminders", "quantos": "how many", "quando": "when", "oferta": "offer", "nicho": "niche",
           "mensagem direta": "DM", "custo por lead": "cost per lead", "fórmula": "formula", "falta": "no-show",
           "enviar": "send", "antes": "before", "depois": "after", "melhor": "best", "cliente": "client",
           "vendas": "sales", "parceiro": "partner", "caro": "expensive", "vencedor": "winner"},
}

NOTE = {
    "fr": "Réponse en anglais (extraits du cours). La traduction complète nécessite l'IA.",
    "es": "Respuesta en inglés (extractos del curso). La traducción completa necesita la IA.",
    "pt": "Resposta em inglês (trechos do curso). A tradução completa precisa da IA.",
}
NOT_COVERED = {
    "en": "The course material doesn't cover this.",
    "fr": "Le contenu du cours ne couvre pas ce sujet.",
    "es": "El material del curso no cubre este tema.",
    "pt": "O material do curso não cobre este assunto.",
}
LANG_NAMES = {"en": "English", "fr": "French", "es": "Spanish", "pt": "Portuguese"}

_WORD = re.compile(r"[\wÀ-ÿ']+", re.U)


def detect(text: str) -> str:
    words = [w.lower() for w in _WORD.findall(text or "")]
    if not words:
        return "en"
    scores = {}
    for lang, sw in STOP.items():
        s = sum(1 for w in words if w in sw) + 2 * sum(1 for w in words if w in DISTINCT[lang])
        if any(ch in text for ch in {"fr": "éèêàçù", "es": "ñ¿¡á", "pt": "ãõç"}[lang]):
            s += 2
        scores[lang] = s
    lang, best = max(scores.items(), key=lambda kv: kv[1])
    en_hits = sum(1 for w in words if w in {"the", "what", "how", "is", "are", "do", "i", "my", "should", "when", "can"})
    if best >= 3 and best > en_hits * 1.5:
        return lang
    return "en"


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def to_english_terms(text: str, lang: str) -> str:
    """Map programme terms to English (longest phrases first). Unknown words are kept, which
    preserves English loan words like "Thunder Dome" or "DM"."""
    if lang == "en":
        return text
    out = text.lower()
    gl = GLOSSARY.get(lang, {})
    for k in sorted(gl, key=len, reverse=True):
        out = re.sub(r"(?<!\w)" + re.escape(k) + r"(?!\w)", gl[k], out)
        ka = _strip_accents(k)
        if ka != k:
            out = re.sub(r"(?<!\w)" + re.escape(ka) + r"(?!\w)", gl[k], out)
    words = [w for w in _WORD.findall(out) if w not in STOP.get(lang, set())]
    return " ".join(words)
