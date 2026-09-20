"""OpenAI-compatible passthrough proxy to the upstream provider.

Ported from OdyssAI-X `_proxy_chat_completion` (scripts/api.py:5020-5243),
OpenAI protocol only (both SE providers speak it). The request body is relayed
as-is — `reasoning_effort`, `thinking`, `tools`, any extra field the client
sends reaches the upstream untouched. Streaming is a verbatim SSE byte relay,
so upstream usage fields stay transparent to the client.
"""

from __future__ import annotations

import json
import sys
from typing import AsyncIterator

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import accounts, metering
from .providers import PROVIDERS, api_base, provider_key, thinking_field

# OpenRouter attribution headers (ignored by other providers).
_ATTRIBUTION = {
    "http-referer": "https://odyssai.eu",
    "x-title": "CoeOS",
}


def _upstream_headers(cfg: dict, pid: str) -> dict:
    headers = {"content-type": "application/json", **_ATTRIBUTION}
    key = provider_key(cfg, pid)
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers


def _prepare_body(body: dict, upstream: str, pid: str | None = None) -> dict:
    """Copy the client body, retarget `model`, normalise thinking flags.
    Everything else passes through verbatim."""
    fwd = dict(body)
    fwd["model"] = upstream
    fwd.pop("session_id", None)  # internal field some clients attach

    # `enable_thinking` is OdyssAI-X's canonical name; most cloud upstreams use
    # `thinking`. An explicit client value wins; we only translate the field
    # name, never inject a default (api.py:5064-5084, minus the server-wide
    # default which is an engine setting SE doesn't have).
    et = fwd.pop("enable_thinking", None)
    t = fwd.get("thinking", None)
    if isinstance(t, dict):
        think_on = None  # structured config — leave untouched
    elif isinstance(t, bool):
        think_on = t
    elif et is not None:
        think_on = bool(et)
    else:
        think_on = None
    if think_on is not None:
        # Chaque provider a SON nom pour ce drapeau : `thinking` sur les
        # passerelles cloud, `enable_thinking` sur OdyssAI-X. Envoyer le mauvais
        # nom ne leve aucune erreur — le modele raisonne simplement quand meme,
        # et brule tout son budget avant d'avoir repondu.
        field = thinking_field(pid)
        # MiniMax's OpenAI-compatible API validates `thinking` as an OBJECT
        # ({"type":"enabled"|"disabled"}), not a bare boolean.
        if field == "thinking" and "minimax" in str(upstream).lower():
            fwd[field] = {"type": "enabled" if think_on else "disabled"}
        else:
            fwd[field] = think_on
        # OpenRouter ignores `thinking` and uses its unified `reasoning` param
        # — verified live on z-ai/*: {"enabled": false} is the only variant
        # that both suppresses reasoning AND frees the token budget (exclude
        # hides it but still burns max_tokens). Translate the client's intent;
        # an explicit client `reasoning` field wins.
        if pid == "openrouter":
            fwd.setdefault("reasoning", {"enabled": think_on})

    # OpenAI spec: streaming responses omit `usage` unless the client opts in.
    # Always opt in so clients can render token counts (api.py:5085-5098).
    if fwd.get("stream"):
        opts = fwd.get("stream_options")
        opts = dict(opts) if isinstance(opts, dict) else {}
        opts.setdefault("include_usage", True)
        fwd["stream_options"] = opts
    return fwd


# Certains modèles imposent le raisonnement et REFUSENT qu'on le coupe
# (qwen3.8-max, o3 : « Reasoning is mandatory for this endpoint and cannot be
# disabled »). Un client qui envoie un no-think exprime une PRÉFÉRENCE, pas un
# contrat : échouer la requête est le pire choix. On rejoue sans le paramètre et
# on le DIT dans un en-tête. Jamais de bascule vers un autre modèle — ce serait
# le fallback silencieux que CoeOS refuse par principe.
_MANDATORY_REASONING = ("reasoning is mandatory", "cannot be disabled",
                        "reasoning cannot be disabled")
THINKING_FORCED_HEADER = "x-coeos-thinking"
# En streaming les en-tetes partent avant le rejeu : l'info ne peut plus y aller,
# elle reste dans le journal. Le chemin non-streaming, lui, porte l'en-tete.


def _reasoning_refused(status: int, text: str) -> bool:
    if status != 400:
        return False
    low = (text or "").lower()
    return "reasoning" in low and any(m in low for m in _MANDATORY_REASONING)


def _drop_reasoning(fwd: dict) -> dict:
    out = {k: v for k, v in fwd.items() if k not in ("reasoning", "thinking")}
    return out


async def proxy_chat(cfg: dict, pid: str, upstream: str, body: dict,
                     decision_headers: dict | None = None):
    """Relay an OpenAI chat completion to `pid`'s upstream as `upstream`.

    Streaming: verbatim SSE byte relay. Non-streaming: parse JSON, return with
    the upstream status code. Decision headers (x-coeos-axis/model/provider)
    ride on the response so agents can observe the routing.
    """
    # L'adresse vient de la CONFIG : celle d'un provider local appartient au
    # client, on ne la connait pas a l'avance.
    url = f"{api_base(cfg, pid).rstrip('/')}/chat/completions"
    headers = _upstream_headers(cfg, pid)
    fwd = _prepare_body(body, upstream, pid)
    extra = dict(decision_headers or {})
    # G-9 / #120 — `x_coeos_decision` header : payload JSON
    # autoritatif du verdict de routage (cf. `coeos-decision.ts`
    # côté client pour le format exact). Le client (Guardian audit
    # G-4) lit ce header pour écrire `guardian_decisions` AVANT
    # de streamer au client final.
    if extra.get("x_coeos_class") and extra.get("x_coeos_profile"):
        try:
            extra["x-coeos-decision"] = json.dumps({
                "class_requested": extra.get("x_coeos_class"),
                "class_served": extra.get("x_coeos_class"),  # pas de repli en V1
                "provider": pid,
                "model": upstream,
                "axis": extra.get("axis"),
                "decider_used": extra.get("x_coeos_decider_used") == "1",
                "profile": extra.get("x_coeos_profile"),
                "rules_hash": None,  # TODO: hash de la table de règles de l'opérateur
            }, separators=(",", ":"))
        except Exception:
            # Ne JAMAIS laisser une sérialisation JSON planter le chat.
            pass
    # On ne rejoue que si c'est NOUS qui avons traduit un no-think en `reasoning`.
    # Un client qui pose `reasoning` explicitement exprime un contrat : on le
    # respecte, y compris son échec.
    may_retry = "reasoning" not in body and "reasoning" in fwd

    # Metrologie (S1.4) : compte et decision captes MAINTENANT — le generateur
    # de streaming peut s'executer hors du contexte de la requete.
    _acc = accounts.current_account.get()
    _dec = metering.current_decision.get() or extra

    if fwd.get("stream"):
        async def gen() -> AsyncIterator[bytes]:
            usage: dict | None = None
            timeout = httpx.Timeout(60.0, read=None)  # no read timeout for SSE
            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    sent = fwd
                    async with client.stream("POST", url, headers=headers, json=sent) as r:
                        if r.status_code >= 400:
                            txt = (await r.aread()).decode("utf-8", "ignore")
                            if may_retry and _reasoning_refused(r.status_code, txt):
                                sent = _drop_reasoning(sent)
                                sys.stderr.write(
                                    f"[coeos] {upstream} impose le raisonnement — "
                                    "rejeu sans le paramètre\n")
                                async with client.stream("POST", url, headers=headers,
                                                         json=sent) as r2:
                                    if r2.status_code < 400:
                                        async for chunk in r2.aiter_bytes():
                                            if chunk:
                                                usage = metering.last_usage_from_sse(chunk, usage)
                                                yield chunk
                                        return
                                    txt = (await r2.aread()).decode("utf-8", "ignore")
                                    r = r2
                            err = {"error": {"message": txt[:300], "code": r.status_code,
                                             "provider": pid}}
                            yield ("data: " + json.dumps(err) + "\n\n").encode()
                            return
                        async for chunk in r.aiter_bytes():
                            if chunk:
                                usage = metering.last_usage_from_sse(chunk, usage)
                                yield chunk
                except Exception as e:
                    err = {"error": {"message": str(e)[:300], "provider": pid}}
                    yield ("data: " + json.dumps(err) + "\n\n").encode()
                finally:
                    metering.record(_acc, _dec, usage, upstream, "openai")

        return StreamingResponse(gen(), media_type="text/event-stream", headers=extra)

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            r = await client.post(url, headers=headers, json=fwd)
        except Exception as e:
            raise HTTPException(502, f"upstream {pid} unreachable: {e}")
        if may_retry and _reasoning_refused(r.status_code, r.text):
            sys.stderr.write(f"[coeos] {upstream} impose le raisonnement — "
                             "rejeu sans le paramètre\n")
            try:
                r = await client.post(url, headers=headers, json=_drop_reasoning(fwd))
            except Exception as e:
                raise HTTPException(502, f"upstream {pid} unreachable: {e}")
            if r.status_code < 400:
                extra[THINKING_FORCED_HEADER] = "forced-on"
        try:
            payload = r.json()
        except Exception:
            raise HTTPException(502, f"upstream {pid} returned non-JSON (status {r.status_code})")
        if r.status_code < 400:
            metering.record(_acc, _dec, payload.get("usage"), upstream, "openai")
        return JSONResponse(payload, status_code=r.status_code, headers=extra)


async def unary_upstream_json(cfg: dict, pid: str, upstream: str,
                              body: dict) -> tuple[int, dict]:
    """Non-streaming upstream call returning (status, payload). Used by the
    Anthropic surface, which needs the parsed OpenAI response to translate it
    rather than a passthrough Response."""
    # L'adresse vient de la CONFIG : celle d'un provider local appartient au
    # client, on ne la connait pas a l'avance.
    url = f"{api_base(cfg, pid).rstrip('/')}/chat/completions"
    fwd = _prepare_body(body, upstream, pid)
    fwd["stream"] = False
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            r = await client.post(url, headers=_upstream_headers(cfg, pid), json=fwd)
        except Exception as e:
            raise HTTPException(502, f"upstream {pid} unreachable: {e}")
        try:
            payload = r.json()
        except Exception:
            raise HTTPException(502, f"upstream {pid} returned non-JSON (status {r.status_code})")
        if r.status_code < 400:
            metering.record(accounts.current_account.get(),
                            metering.current_decision.get(),
                            payload.get("usage"), upstream, "anthropic")
        return r.status_code, payload


async def stream_upstream_chunks(cfg: dict, pid: str, upstream: str, body: dict):
    """Streaming upstream call yielding PARSED OpenAI chunk dicts (skipping
    keep-alive comments), for surfaces that transcode rather than relay.
    Yields {"error": {...}} once and stops on upstream/transport errors."""
    # L'adresse vient de la CONFIG : celle d'un provider local appartient au
    # client, on ne la connait pas a l'avance.
    url = f"{api_base(cfg, pid).rstrip('/')}/chat/completions"
    fwd = _prepare_body(body, upstream, pid)
    fwd["stream"] = True
    opts = fwd.get("stream_options")
    opts = dict(opts) if isinstance(opts, dict) else {}
    opts.setdefault("include_usage", True)
    fwd["stream_options"] = opts
    _acc = accounts.current_account.get()
    _dec = metering.current_decision.get()
    usage: dict | None = None
    timeout = httpx.Timeout(60.0, read=None)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, headers=_upstream_headers(cfg, pid),
                                     json=fwd) as r:
                if r.status_code >= 400:
                    txt = (await r.aread()).decode("utf-8", "ignore")
                    yield {"error": {"message": txt[:300], "code": r.status_code,
                                     "provider": pid}}
                    return
                buf = ""
                async for text in r.aiter_text():
                    buf += text
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue  # SSE comments / event names / blanks
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return
                        try:
                            chunk = json.loads(payload)
                        except Exception:
                            continue
                        if isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]
                        yield chunk
        except Exception as e:
            yield {"error": {"message": str(e)[:300], "provider": pid}}
        finally:
            metering.record(_acc, _dec, usage, upstream, "anthropic")


async def unary_upstream_text(cfg: dict, pid: str, upstream: str,
                              messages: list[dict], max_tokens: int = 600) -> str:
    """Small non-streaming call used by the decider. Returns the assistant
    text ("" on any failure — callers fall back to the default axis).

    Reasoning-first upstreams are the trap here (verified live): some ignore
    `thinking: false`, burn the whole budget inside the `reasoning` field and
    return an EMPTY content — the AXIS line never arrives. Three guards:
    OpenRouter's unified `reasoning: {enabled: false}` hint (ignored by
    upstreams that don't know it), a budget that survives a thinking block
    anyway, and parsing `reasoning` too (content last, so a real final answer
    always wins in the last-match parsing rules)."""
    # L'adresse vient de la CONFIG : celle d'un provider local appartient au
    # client, on ne la connait pas a l'avance.
    url = f"{api_base(cfg, pid).rstrip('/')}/chat/completions"
    body = {"model": upstream, "messages": messages, "max_tokens": max_tokens,
            "stream": False, "thinking": False,
            "reasoning": {"enabled": False}}
    if "minimax" in str(upstream).lower():
        body["thinking"] = {"type": "disabled"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=_upstream_headers(cfg, pid), json=body)
            if r.status_code >= 400:
                return ""
            payload = r.json()
            # Le decider consomme aussi — visible en metrologie sous "_decider"
            # (un compte BYOK paie SES classifications, autant qu'il le voie).
            metering.record(accounts.current_account.get(), {"axis": "_decider"},
                            payload.get("usage"), upstream, "openai")
            choices = payload.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
            content = msg.get("content") or ""
            return f"{reasoning}\n{content}".strip()
    except Exception:
        return ""
