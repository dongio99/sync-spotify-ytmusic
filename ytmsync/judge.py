"""Giudice IA per i casi dubbi: Claude con ricerca web e risposta JSON strutturata."""

from __future__ import annotations

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel

from .models import Track

log = logging.getLogger(__name__)

SYSTEM = """Decidi se due voci di catalogo musicale corrispondono allo stesso brano.

"Stesso brano" significa: stessa canzone dello stesso artista principale, nella stessa versione sostanziale.
- Rimasterizzazioni, edizioni deluxe, versioni "radio edit"/"single", videoclip ufficiali e audio ufficiali
  dello stesso pezzo contano come lo STESSO brano.
- Versioni live, remix, cover, acustiche, strumentali, "sped up"/"slowed", karaoke e brani diversi con lo
  stesso titolo contano come brani DIVERSI, a meno che anche il riferimento sia quella stessa versione.
- I titoli possono essere in lingue, traslitterazioni o formattazioni diverse; il canale YouTube può
  essere un'etichetta o un nome "VEVO"/"- Topic" invece dell'artista.
- La durata è un indizio, non una prova (i videoclip hanno spesso intro più lunghe).

Usa la ricerca web solo se i dati forniti non bastano a decidere. Rispondi solo con il JSON richiesto."""


class Verdict(BaseModel):
    match_index: int  # indice del candidato uguale al riferimento, -1 se nessuno
    confidence: Literal["high", "medium", "low"]
    reason: str


class JudgeUnavailable(Exception):
    """Il giudice non è utilizzabile per problemi di account/chiave (serve un intervento)."""


class AIJudge:
    def __init__(self, api_key: str, model: str, max_web_searches: int, max_calls: int, cache: dict):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=120)
        self.model = model
        self.max_web_searches = max_web_searches
        self.max_calls = max_calls
        self.cache = cache  # condiviso con lo stato: "sorgente||candidato" -> True/False
        self.calls = 0
        self.errors = 0  # errori temporanei (rete, sovraccarico) in questa esecuzione
        self.unavailable: str | None = None  # motivo, se l'account/chiave non funziona

    @staticmethod
    def _ck(source: Track, cand: Track) -> str:
        return f"{source.id}||{cand.id}"

    def __call__(self, source: Track, candidates: list[Track]) -> int | None:
        # Prima la cache: se un candidato era già stato confermato, o se tutti erano già stati esclusi
        known = [self.cache.get(self._ck(source, c)) for c in candidates]
        if True in known:
            return known.index(True)
        if all(k is False for k in known):
            return -1
        todo = [c for c, k in zip(candidates, known) if k is None]

        if self.unavailable or self.calls >= self.max_calls:
            return None
        self.calls += 1

        lines = [f"RIFERIMENTO ({source.platform}): {source.describe()}", "", "CANDIDATI:"]
        lines += [f"[{i}] ({c.platform}): {c.describe()}" for i, c in enumerate(todo)]
        lines += ["", "Quale candidato è lo stesso brano del riferimento? match_index = -1 se nessuno."]

        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM,
                messages=[{"role": "user", "content": "\n".join(lines)}],
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": self.max_web_searches}],
                output_format=Verdict,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            self.unavailable = f"chiave Anthropic non valida o senza permessi ({e.status_code})"
            log.error("Giudice IA: %s", self.unavailable)
            return None
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if "credit" in msg or "billing" in msg:
                self.unavailable = "credito Anthropic esaurito"
            self.errors += 1
            log.error("Giudice IA, richiesta rifiutata: %s", e)
            return None
        except anthropic.APIError as e:
            # Rete, 429 e 5xx sono già stati ritentati dall'SDK: rimandiamo alla prossima esecuzione
            self.errors += 1
            log.warning("Giudice IA non raggiungibile, rimando: %s", e)
            return None
        except Exception as e:  # noqa: BLE001 - es. JSON non conforme: meglio rimandare che fermare il sync
            self.errors += 1
            log.warning("Risposta del giudice IA non utilizzabile, rimando: %s", e)
            return None

        if resp.stop_reason not in ("end_turn", "stop_sequence") or resp.parsed_output is None:
            log.warning("Giudice IA senza verdetto (stop_reason=%s), rimando", resp.stop_reason)
            return None

        v: Verdict = resp.parsed_output
        idx = v.match_index if 0 <= v.match_index < len(todo) else -1
        log.info(
            "IA: %s -> %s (%s) %s",
            source.describe(),
            todo[idx].describe() if idx >= 0 else "nessuno",
            v.confidence,
            v.reason,
        )
        for i, c in enumerate(todo):
            self.cache[self._ck(source, c)] = i == idx
        return candidates.index(todo[idx]) if idx >= 0 else -1
