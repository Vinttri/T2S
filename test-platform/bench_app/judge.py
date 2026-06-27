"""LLM judge stages.

Two related modes live here:

1. ``bench-answers/v1`` -> ``bench-judged-levels/v1``:
   a run first collects raw connector answers and execution evidence without any
   L0-L4 score, then an OpenAI-compatible model assigns the final level.
2. ``bench-result/v1`` -> ``bench-judged/v1``:
   legacy semantic review of an already-scored run.

Config (OpenAI-compatible gateway), via env:
    LLM_BASE_URL   e.g. http://host:9000/v1
    LLM_API_KEY
    LLM_MODEL      e.g. llmgateway/light_model
    LLM_AUTH_HEADER  default: Authorization; set none/off to send no auth header
    LLM_AUTH_SCHEME  default: Bearer; set none/off to send the raw key
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from leaderboard.redaction import redact_text, safe_exception
from bench_app.http_client import httpx_verify

LEVELS_DOC = (
    "L4 — результат точно совпал с эталоном (задача решена верно).\n"
    "L3 — SQL исполнился, но строки/набор колонок расходятся с эталоном (логическая ошибка).\n"
    "L2 — эталонный (gold) SQL упал на этой БД — проблема кейса/окружения, не модели.\n"
    "L1 — SQL сгенерирован, но не исполняется (синтаксис/нет таблицы/типы).\n"
    "L0 — модель не вернула SQL вовсе."
)


class LevelJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["L0", "L1", "L2", "L3", "L4"]
    reason: str = Field(min_length=1, max_length=1000)
    error_category: Literal[
        "no_sql",
        "api_error",
        "execution_error",
        "gold_error",
        "wrong_result",
        "correct",
    ]
    confidence: float = Field(ge=0.0, le=1.0)


def _auth_enabled(header: str | None) -> bool:
    return str(header or "").strip().lower() not in {"", "none", "off", "disabled", "0", "false", "no"}


def _auth_value(api_key: str, scheme: str | None) -> str:
    scheme_text = str(scheme or "").strip()
    if scheme_text.lower() in {"", "none", "off", "disabled", "0", "false", "no"}:
        return api_key
    return f"{scheme_text} {api_key}"


def _llm_headers(cfg: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    auth_header = cfg.get("auth_header") or "Authorization"
    if _auth_enabled(auth_header) and cfg.get("api_key"):
        headers[auth_header] = _auth_value(cfg["api_key"], cfg.get("auth_scheme"))
    return headers


def llm_config(overrides: dict | None = None) -> dict | None:
    overrides = overrides or {}
    base = overrides.get("base_url") or os.getenv("LLM_BASE_URL")
    key = overrides.get("api_key") or os.getenv("LLM_API_KEY")
    model = overrides.get("model") or os.getenv("LLM_MODEL")
    auth_header = overrides.get("auth_header") or os.getenv("LLM_AUTH_HEADER", "Authorization")
    auth_scheme = overrides.get("auth_scheme") or os.getenv("LLM_AUTH_SCHEME", "Bearer")
    if base and model and (key or not _auth_enabled(auth_header)):
        return {
            "base_url": base,
            "api_key": key or "",
            "model": model,
            "auth_header": auth_header,
            "auth_scheme": auth_scheme,
        }
    return None


def _fmt_result(res, limit=15):
    if not res:
        return "(нет результата)"
    if res.get("error"):
        return f"ОШИБКА: {redact_text(res['error'])}"
    cols = res.get("columns", []) or []
    rows = (res.get("rows") or [])[:limit]
    head = " | ".join(map(str, cols))
    body = "\n".join(" | ".join("NULL" if v is None else str(v) for v in r) for r in rows)
    extra = f"\n… всего {res.get('row_count')} строк" if (res.get("row_count") or 0) > limit else ""
    return f"{head}\n{body}{extra}".strip() or "(0 строк)"


def build_prompt(case: dict) -> str:
    return (
        "Ты оцениваешь ответ Text-to-SQL модели по шкале уровней ошибок L1..L4. "
        "Сравни SQL и результат кандидата с эталоном (gold) и вопросом.\n\n"
        f"ШКАЛА:\n{LEVELS_DOC}\n\n"
        f"ВОПРОС:\n{case.get('question')}\n\n"
        f"GOLD SQL:\n{case.get('gold_sql')}\n\n"
        f"GOLD ОТВЕТ:\n{_fmt_result(case.get('gold_result'))}\n\n"
        f"SQL КАНДИДАТА:\n{case.get('predicted_sql') or '(нет SQL)'}\n\n"
        f"ОТВЕТ КАНДИДАТА:\n{_fmt_result(case.get('agent_result'))}\n\n"
        f"Автоматический уровень (execution-match): L{case.get('level')}.\n\n"
        "Верни ТОЛЬКО JSON-объект:\n"
        '{"assessed_level":"L1|L2|L3|L4","error_category":"<кратко: тип ошибки или \'нет ошибки\'>",'
        '"explanation":"<1-2 предложения почему>","agrees_with_auto":true|false}'
    )


def build_level_prompt(case: dict) -> str:
    return (
        "Ты финальный судья Text-to-SQL бенчмарка. Нужно выставить ровно один уровень L0-L4 "
        "по вопросу, gold SQL, ответу модели и результатам исполнения. Не придумывай данные: "
        "опирайся только на предоставленные SQL, ошибки и таблицы результата.\n\n"
        f"ШКАЛА:\n{LEVELS_DOC}\n\n"
        f"CASE ID: {case.get('case_id')}\n"
        f"СЛОЖНОСТЬ: {case.get('difficulty')}\n\n"
        f"ВОПРОС:\n{case.get('question')}\n\n"
        f"GOLD SQL:\n{case.get('gold_sql')}\n\n"
        f"GOLD ОТВЕТ:\n{_fmt_result(case.get('gold_result'))}\n\n"
        f"SQL МОДЕЛИ:\n{case.get('predicted_sql') or '(нет SQL)'}\n\n"
        f"ОТВЕТ МОДЕЛИ:\n{_fmt_result(case.get('agent_result'))}\n\n"
        f"ОШИБКА API/SQL:\n{redact_text(case.get('error') or '(нет)')}\n\n"
        "Правила:\n"
        "- Если SQL модели отсутствует, ставь L0.\n"
        "- Если SQL есть, но agent_result содержит ошибку исполнения, ставь L1.\n"
        "- Если gold_result содержит ошибку исполнения, ставь L2.\n"
        "- Если оба SQL исполнились, но результат модели отличается от gold, ставь L3.\n"
        "- Если результат модели эквивалентен gold для вопроса, ставь L4.\n\n"
        "Верни ТОЛЬКО JSON-объект без markdown. Схема строгая: ровно 4 поля, без лишних полей.\n"
        '{"level":"L0|L1|L2|L3|L4","reason":"<1-2 коротких предложения>",'
        '"error_category":"no_sql|api_error|execution_error|gold_error|wrong_result|correct",'
        '"confidence":0.0}\n'
        "confidence должен быть числом от 0.0 до 1.0."
    )


def build_level_repair_prompt(case: dict, raw: str, validation_error: str) -> str:
    return (
        f"{build_level_prompt(case)}\n\n"
        "Предыдущий ответ не прошел строгую валидацию. Исправь ответ и верни только валидный JSON.\n\n"
        f"НЕВАЛИДНЫЙ ОТВЕТ:\n{(raw or '')[:2000]}\n\n"
        f"ОШИБКА ВАЛИДАЦИИ:\n{validation_error[:1200]}"
    )


def _normalise_level(value) -> int | None:
    if isinstance(value, int) and 0 <= value <= 4:
        return value
    s = str(value or "").strip().upper()
    if re.fullmatch(r"L?[0-4]", s):
        return int(s[-1])
    return None


def _extract_json_object(text: str) -> dict:
    decoder = json.JSONDecoder()
    text = text or ""
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("JSON object not found")


def _validate_level_judge_output(text: str) -> tuple[LevelJudgeOutput | None, str | None]:
    try:
        obj = _extract_json_object(text)
        return LevelJudgeOutput.model_validate(obj), None
    except (ValueError, ValidationError) as exc:
        return None, safe_exception(exc, limit=1000)


async def _call_chat_completion(client, cfg, prompt: str, timeout: float) -> str:
    resp = await client.post(
        f"{cfg['base_url'].rstrip('/')}/chat/completions",
        headers=_llm_headers(cfg),
        json={"model": cfg["model"], "temperature": 0.0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def check_llm_connection(cfg: dict, timeout: float = 3600.0, client=None) -> dict:
    """Make a tiny OpenAI-compatible chat/completions call using env-derived cfg."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(verify=httpx_verify("LLM_SSL_VERIFY", "BENCH_APP_LLM_SSL_VERIFY"))
    t0 = time.time()
    try:
        content = await _call_chat_completion(
            client,
            cfg,
            "Проверка подключения. Ответь ровно одной короткой строкой: BENCH_LLM_OK",
            timeout,
        )
        return {
            "ok": True,
            "base_url": redact_text(cfg.get("base_url")),
            "model": cfg.get("model"),
            "elapsed_s": round(time.time() - t0, 2),
            "content": (content or "")[:1000],
        }
    finally:
        if own_client:
            await client.aclose()


async def _judge_level_case(client, cfg, case, timeout, max_retries: int = 0, retry_delay: float = 0.0):
    attempts_total = max(0, int(max_retries or 0)) + 1
    last = None
    for attempt in range(1, attempts_total + 1):
        try:
            content = await _call_chat_completion(client, cfg, build_level_prompt(case), timeout)
            parsed, validation_error = _validate_level_judge_output(content)
            repair_attempted = False
            raw_response = redact_text(content[:4000])
            if parsed is None:
                repair_attempted = True
                repair_content = await _call_chat_completion(
                    client,
                    cfg,
                    build_level_repair_prompt(case, content, validation_error or "unknown validation error"),
                    timeout,
                )
                repaired, repair_error = _validate_level_judge_output(repair_content)
                raw_response = redact_text((content[:1800] + "\n\n--- repair attempt ---\n" + repair_content[:1800])[:4000])
                parsed, validation_error = repaired, repair_error
            if parsed is not None:
                level = _normalise_level(parsed.level)
                return {
                    "level": level,
                    "raw_level": parsed.level,
                    "reason": parsed.reason,
                    "error_category": parsed.error_category,
                    "confidence": parsed.confidence,
                    "raw_response": raw_response,
                    "validation_error": None,
                    "repair_attempted": repair_attempted,
                    "attempts": attempt,
                }
            last = {
                "level": None,
                "raw_level": None,
                "reason": validation_error or "invalid judge output",
                "error_category": "judge_error",
                "confidence": None,
                "raw_response": raw_response,
                "validation_error": validation_error,
                "repair_attempted": repair_attempted,
                "attempts": attempt,
            }
        except Exception as exc:  # noqa: BLE001
            last = {"level": None, "raw_level": None, "reason": safe_exception(exc, extra_secrets=[cfg.get("api_key")], limit=200),
                    "error_category": "judge_error", "confidence": None, "raw_response": None,
                    "validation_error": None, "repair_attempted": False, "attempts": attempt}
        if attempt < attempts_total and retry_delay and retry_delay > 0:
            await asyncio.sleep(float(retry_delay))
    return last or {"level": None, "raw_level": None, "reason": "judge failed",
                    "error_category": "judge_error", "confidence": None, "raw_response": None,
                    "validation_error": None, "repair_attempted": False, "attempts": attempts_total}


async def judge_answers(answers: dict, cfg: dict, *, timeout: float = 3600, concurrency: int = 1,
                        judged_at: float | None = None, max_retries: int = 0,
                        retry_delay: float = 0.0) -> dict:
    """Take a bench-answers/v1 doc and return bench-judged-levels/v1."""
    cases = answers.get("cases") or []
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
    async with httpx.AsyncClient(verify=httpx_verify("LLM_SSL_VERIFY", "BENCH_APP_LLM_SSL_VERIFY")) as client:
        async def run(case):
            async with sem:
                return await _judge_level_case(client, cfg, case, timeout, max_retries, retry_delay)
        assessments = await asyncio.gather(*(run(c) for c in cases))

    judged_cases, by_level = [], {f"L{i}": 0 for i in range(5)}
    invalid = 0
    for c, a in zip(cases, assessments):
        level = a.get("level")
        if level is None:
            invalid += 1
        else:
            by_level[f"L{level}"] += 1
        judged_cases.append({**c, "level": level, "matched": level == 4,
                             "reason": a.get("reason"), "assessment": a})

    return {
        **answers,
        "schema": "bench-judged-levels/v1",
        "judge": {"model": cfg["model"], "judged_at": judged_at},
        "cases": judged_cases,
        "judge_summary": {
            "levels": by_level,
            "invalid": invalid,
            "cases_judged": len(cases),
        },
    }


async def _judge_case(client, cfg, case, timeout):
    try:
        resp = await client.post(
            f"{cfg['base_url'].rstrip('/')}/chat/completions",
            headers=_llm_headers(cfg),
            json={"model": cfg["model"], "temperature": 0.0,
                  "messages": [{"role": "user", "content": build_prompt(case)}]},
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return {"assessed_level": None, "error_category": "unparsed", "explanation": content[:200], "agrees_with_auto": None}
    except Exception as exc:  # noqa: BLE001
        return {"assessed_level": None, "error_category": "judge_error",
                "explanation": safe_exception(exc, extra_secrets=[cfg.get("api_key")], limit=200), "agrees_with_auto": None}


async def judge_result(result: dict, cfg: dict, *, timeout: float = 3600, concurrency: int = 1,
                       judged_at: float | None = None) -> dict:
    """Take a bench-result/v1 doc, return a bench-judged/v1 doc."""
    cases = result.get("cases") or []
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
    async with httpx.AsyncClient(verify=httpx_verify("LLM_SSL_VERIFY", "BENCH_APP_LLM_SSL_VERIFY")) as client:
        async def run(case):
            async with sem:
                return await _judge_case(client, cfg, case, timeout)
        assessments = await asyncio.gather(*(run(c) for c in cases))

    judged_cases, by_level, agree = [], {f"L{i}": 0 for i in range(5)}, 0
    for c, a in zip(cases, assessments):
        judged_cases.append({**c, "assessment": a})
        lvl = a.get("assessed_level")
        if lvl in by_level:
            by_level[lvl] += 1
        if a.get("agrees_with_auto") is True:
            agree += 1

    return {
        **result,
        "schema": "bench-judged/v1",
        "judge": {"model": cfg["model"], "judged_at": judged_at},
        "cases": judged_cases,
        "judge_summary": {
            "assessed_levels": by_level,
            "agreement_with_auto": round(agree / len(cases), 3) if cases else None,
            "cases_judged": len(cases),
        },
    }
