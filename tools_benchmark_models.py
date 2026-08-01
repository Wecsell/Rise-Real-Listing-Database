# -*- coding: utf-8 -*-
"""
Бенчмарк моделей Gemini на реальных ловушках из документов застройщиков.

Зачем: выбор модели для разбора документов должен опираться на замер, а не на
поколение в названии. Набор кейсов собран из документов проекта Four Palms,
правильные ответы установлены вручную при разборе (см. Gaps карточки проекта).

Кейсы двух видов:
  trap       - есть конкретный неправильный ответ, на который ловится слабая
               модель (например, срок аренды 17 лет из штрафной статьи вместо 35 из ст.1)
  competence - базовая проверка, ловушки нет; провал означает непригодность на любом уровне

Помимо правильности проверяется ЦИТАТА: модель обязана вернуть дословную выдержку
из документа. Если выдержки нет в исходном тексте - это выдумка, и такой ответ
нельзя писать в базу, даже если он случайно верный.

Запуск:
    python tools_benchmark_models.py                # все модели по умолчанию
    python tools_benchmark_models.py --models gemini-3.6-flash,gemini-2.5-flash
    python tools_benchmark_models.py --case lease_term_penalty_trap
"""
import os
import re
import json
import time
import argparse

from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types

# Проверка цитат живёт в app/citations.py: тот же код сторожит боевую запись
# в базу (Э2, извлечение полей). Держать здесь вторую копию нельзя - разойдётся
# с проверенной версией, а именно эта функция четыре раза подряд оказывалась
# причиной мнимого «провала модели».
from app.citations import normalize, check_quotes

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tests", "fixtures", "model_traps", "cases.json")

DEFAULT_MODELS = [
    "gemini-2.5-flash",        # то, что стоит сейчас
    "gemini-3.5-flash-lite",   # кандидат на дешёвый уровень (чат)
    "gemini-3.5-flash",
    "gemini-3.6-flash",        # кандидат на рабочую лошадку (документы)
    "gemini-3.1-pro-preview",  # кандидат на юридический уровень
]

SYSTEM_PROMPT = """You extract ONE fact from a real-estate legal document.

Rules:
- Answer ONLY from the document text provided. Never use outside knowledge, never infer.
- You MUST provide `quotes`: a list of verbatim substrings copied EXACTLY from the
  document, character for character, that support your answer. Do not paraphrase and do
  not stitch separate fragments into one string - put each contiguous fragment as its own
  item in the list. If your answer rests on two documents or two rows, give one quote per source.
- If the document does not state the answer, set answer to "not stated" and quotes to [].
- Beware of numbers that appear in a different context than the one asked about.

Return strictly this JSON:
{"answer": "<short answer>", "quotes": ["<verbatim fragment>", "..."], "confidence": <0.0-1.0>}
"""


def nums(s: str):
    return set(re.findall(r"\d+", s or ""))


def classify(case_id: str, answer: str) -> str:
    """
    Возвращает 'correct' | 'trapped' | 'refused' | 'other'.

    'refused' отделён от 'other' намеренно. Цена этих исходов различается на
    порядок: неверное значение молча портит карточку и никто не заметит, а отказ
    стоит одного лишнего вопроса агенту - и по архитектуре пайплайна это штатный
    триггер эскалации, то есть желаемое поведение, а не сбой. Сваливать их в один
    столбец значит браковать модель за осторожность.
    """
    a = normalize(answer)
    n = nums(a)

    # В кейсе про разрешение "not stated" - правильный ответ по существу:
    # документ действительно не сообщает о наличии PBG. Поэтому общая проверка
    # на отказ применяется ко всем кейсам, КРОМЕ него.
    if case_id != "itr_is_not_a_building_permit":
        if a.startswith("not stated") or a in ("unknown", "n/a", "none", ""):
            return "refused"

    if case_id == "lease_term_penalty_trap":
        if "35" in n:
            return "correct"
        if "17" in n:
            return "trapped"
        return "other"

    if case_id == "lease_extension_right":
        if "guarantee" in a and "market" in a:
            return "correct"
        if "no extension" in a or a.startswith("no"):
            return "trapped"
        if "priority" in a and "market" in a:
            return "other"   # частично верно: ст.12 говорит о приоритете, но ст.11 даёт гарантию
        return "other"

    if case_id == "itr_is_not_a_building_permit":
        if a.startswith("no") or "not stated" in a:
            return "correct"
        if a.startswith("yes"):
            return "trapped"
        return "other"

    if case_id == "itr_zoning_class":
        return "correct" if "residential" in a else "other"

    if case_id == "land_area_discrepancy":
        if "600" in n and "580" in n:
            return "correct"
        if ("600" in n) ^ ("580" in n):
            return "trapped"
        return "other"

    if case_id == "simbg_permit_status":
        # Порядок проверок важен: явное отрицание выигрывает у утверждения.
        # Раньше сначала искалась подстрока "has been issued", которая целиком
        # сидит внутри "has NOT been issued" - правильный ответ засчитывался как
        # попадание в ловушку. Тот же класс бага, что "are" внутри "share"
        # в предфильтре listener.py.
        said_not_issued = any(k in a for k in
                              ["not issued", "not been issued", "perbaikan", "correction",
                               "in process", "not completed", "no pbg", "not yet",
                               "has not been", "ongoing"])
        if said_not_issued:
            return "correct"
        claims_issued = any(k in a for k in
                            ["has been issued", "slf issued", "permits are in order", "obtained"])
        if claims_issued:
            return "trapped"
        return "other"

    return "other"


def run_case(client, model: str, case: dict) -> dict:
    started = time.time()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=f"DOCUMENT:\n{case['text']}\n\nQUESTION: {case['question']}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        raw = (resp.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?|```$", "", raw).strip()
        # Часть моделей возвращает шаблон из промпта и следом реальный ответ,
        # то есть два JSON-объекта подряд. json.loads на такой строке падает с
        # "Extra data" - берём первый валидный объект, а не всю строку целиком.
        start = raw.find("{")
        data, _ = json.JSONDecoder().raw_decode(raw[start:] if start >= 0 else raw)
        answer = str(data.get("answer", ""))
        quotes = data.get("quotes", data.get("quote", []))
        # "not stated" без цитат - это предписанное промптом поведение, а не выдумка.
        if normalize(answer) in ("not stated", "not stated.") and not quotes:
            cite = "n/a"
        else:
            cite = check_quotes(quotes, case["text"])
        return {
            "ok": True,
            "answer": answer,
            "quotes": quotes,
            "confidence": data.get("confidence"),
            "verdict": classify(case["id"], answer),
            "cite": cite,
            "elapsed": round(time.time() - started, 2),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:160],
                "verdict": "error", "cite": "bad",
                "elapsed": round(time.time() - started, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--case", default=None, help="прогнать только один кейс по id")
    ap.add_argument("--repeat", type=int, default=1,
                    help="повторов на кейс; >1 показывает разброс ответов. "
                         "temperature=0 не гарантирует детерминизм, поэтому "
                         "одиночный прогон не отличает устойчивое поведение модели от шума")
    ap.add_argument("--out", default="benchmark_results.json")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY не задан в .env")
    client = genai.Client(api_key=api_key)

    with open(CASES_PATH, encoding="utf-8") as fh:
        cases = json.load(fh)
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            raise SystemExit(f"кейс {args.case} не найден")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = {}

    for model in models:
        print(f"\n=== {model}")
        results[model] = {}
        for case in cases:
            runs = [run_case(client, model, case) for _ in range(args.repeat)]
            r = runs[0]
            if args.repeat > 1:
                verdicts = [x["verdict"] for x in runs]
                # Устойчивость важнее одного удачного прогона: модель, дающая
                # разные ответы на один и тот же вход, непригодна независимо
                # от того, какой из них правильный.
                r = dict(r, runs=verdicts,
                         stable=len(set(verdicts)) == 1,
                         correct_rate=round(verdicts.count("correct") / len(verdicts), 2))
            results[model][case["id"]] = r
            mark = {"correct": "OK  ", "trapped": "TRAP", "refused": "SKIP",
                    "other": "??  ", "error": "ERR "}[r["verdict"]]
            q = {"ok": "cite:ok     ", "spliced": "cite:spliced",
                 "bad": "cite:FAKE   ", "n/a": "cite:none   "}[r["cite"]]
            detail = r.get("error") or r.get("answer", "")[:70]
            if args.repeat > 1:
                flag = "STABLE  " if r["stable"] else "UNSTABLE"
                print(f"  {mark} {q} {flag} {r['correct_rate']:.0%} {case['id']:32} {detail[:45]}")
            else:
                print(f"  {mark} {q} {case['id']:32} {detail}")

    # Сводка
    print("\n" + "=" * 90)
    print("  trapped/other = порча данных (дорого)   refused = вопрос агенту (дёшево)")
    header = (f"{'model':26} {'correct':>8} {'TRAPPED':>8} {'other':>6} {'refused':>8} "
              f"{'cite ok':>8} {'FAKE':>5} {'avg s':>7}")
    print(header)
    print("-" * 90)
    summary = {}
    for model in models:
        rs = list(results[model].values())
        c = sum(1 for r in rs if r["verdict"] == "correct")
        t = sum(1 for r in rs if r["verdict"] == "trapped")
        ref = sum(1 for r in rs if r["verdict"] == "refused")
        o = sum(1 for r in rs if r["verdict"] in ("other", "error"))
        qok = sum(1 for r in rs if r["cite"] in ("ok", "n/a"))
        qsp = sum(1 for r in rs if r["cite"] == "spliced")
        qbad = sum(1 for r in rs if r["cite"] == "bad")
        avg = round(sum(r["elapsed"] for r in rs) / max(len(rs), 1), 2)
        summary[model] = {"correct": c, "trapped": t, "refused": ref, "other": o,
                          "cite_ok": qok, "cite_spliced": qsp, "cite_fabricated": qbad,
                          "total": len(rs), "avg_sec": avg}
        print(f"{model:26} {c:>4}/{len(rs):<3} {t:>8} {o:>6} {ref:>8} "
              f"{qok:>8} {qbad:>5} {avg:>7}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, ensure_ascii=False, indent=2)
    print(f"\nПодробности: {args.out}")


if __name__ == "__main__":
    main()
