# -*- coding: utf-8 -*-
"""
Бенчмарк моделей на СКАНАХ (vision), в пару к текстовому tools_benchmark_models.py.

Зачем отдельно: текстовый прогон измеряет только рассуждение. На сканах поверх
него ложится ошибка распознавания, а вместе с ней исчезает главный дешёвый
предохранитель - проверка цитаты. Сверять дословную выдержку не с чем: у скана
нет исходного текста. Поэтому здесь три режима проверки цитаты:
    verified     - у документа есть текстовый слой, цитата сверена с ним
    unverifiable - чистый скан, проверить невозможно в принципе
    (bad)        - слой есть и цитаты в нём нет, то есть выдумка

Документы не хранятся в репозитории: это чужие юридические документы реального
проекта. Скрипт скачивает их по публичным ссылкам, рендерит, прогоняет и удаляет.
Паспорт и KTP из папки не трогаются вовсе.

Запуск:
    python tools_benchmark_vision.py
    python tools_benchmark_vision.py --models gemini-3.5-flash --dpi 150
"""
import os
import re
import json
import time
import shutil
import argparse
import tempfile
import urllib.request

from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types

from tools_benchmark_models import classify, check_quotes, normalize, CASES_PATH

DRIVE = {
    "lease": "1VC8Wny9A_uEK8HzYC3yxY8Xmx_s5zQ03",   # мастер-лиз, скан, текстового слоя нет
    "itr":   "1KJtQ4LmVBIt_QuR92jRp6MGywH3DhKOQ",   # ITR, текстовый слой ЕСТЬ (контроль)
    "simbg": "1O_Hvu6Bfemb2yWC0n_T57cwATv_rihgZ",   # скриншот системы разрешений, jpeg
}

# Какие страницы каждого документа нужны под какой кейс.
# Номера страниц соответствуют тем, где лежит ответ (проверено при ручном разборе).
VISION_CASES = {
    "lease_term_penalty_trap":      {"doc": "lease", "pages": [4, 5]},
    "lease_extension_right":        {"doc": "lease", "pages": [8]},
    "itr_is_not_a_building_permit": {"doc": "itr",   "pages": [1]},
    "itr_zoning_class":             {"doc": "itr",   "pages": [1]},
    "simbg_permit_status":          {"doc": "simbg", "pages": None},
}

DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.1-pro-preview",
]

SYSTEM_PROMPT = """You extract ONE fact from a scanned real-estate legal document.

Rules:
- Answer ONLY from what is visible in the images. Never use outside knowledge, never infer.
- You MUST provide `quotes`: a list of fragments copied EXACTLY as they appear in the
  document, character for character. Each contiguous fragment is its own list item.
- If the document does not state the answer, set answer to "not stated" and quotes to [].
- Parts of the document are blacked out. Never guess what is under a redaction.
- Beware of numbers that appear in a different context than the one asked about.

Return strictly this JSON:
{"answer": "<short answer>", "quotes": ["<verbatim fragment>", "..."], "confidence": <0.0-1.0>}
"""


def download(fid: str, path: str):
    url = f"https://drive.google.com/uc?export=download&id={fid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(path, "wb") as fh:
        fh.write(data)
    return data


def prepare_images(workdir: str, dpi: int):
    """Скачивает документы и рендерит нужные страницы. Возвращает {doc: {page: bytes}}."""
    import fitz
    import pypdf

    out = {}
    text_layers = {}

    for doc, fid in DRIVE.items():
        needed = sorted({p for c in VISION_CASES.values()
                         if c["doc"] == doc and c["pages"] for p in c["pages"]})
        if doc == "simbg":
            path = os.path.join(workdir, "simbg.jpeg")
            data = download(fid, path)
            out[doc] = {0: data}
            text_layers[doc] = None          # скриншот, текстового слоя нет
            print(f"  {doc}: jpeg, {len(data)//1024} KB")
            continue

        path = os.path.join(workdir, f"{doc}.pdf")
        download(fid, path)

        reader = pypdf.PdfReader(path)
        raw = "\n".join((p.extract_text() or "") for p in reader.pages)
        text_layers[doc] = raw if len(raw.strip()) > 100 else None

        pdf = fitz.open(path)
        pages = {}
        for pno in needed:
            pix = pdf[pno - 1].get_pixmap(dpi=dpi)
            pages[pno] = pix.tobytes("png")
        pdf.close()
        out[doc] = pages
        layer = "есть" if text_layers[doc] else "НЕТ (чистый скан)"
        print(f"  {doc}: {len(pages)} стр. отрендерено, текстовый слой {layer}")

    return out, text_layers


def run_case(client, model, case, images, text_layer):
    started = time.time()
    parts = []
    for img in images:
        mime = "image/jpeg" if img[:2] == b"\xff\xd8" else "image/png"
        parts.append(types.Part.from_bytes(data=img, mime_type=mime))
    parts.append(f"QUESTION: {case['question']}")

    try:
        resp = client.models.generate_content(
            model=model,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        raw = (resp.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?|```$", "", raw).strip()
        start = raw.find("{")
        data, _ = json.JSONDecoder().raw_decode(raw[start:] if start >= 0 else raw)

        answer = str(data.get("answer", ""))
        quotes = data.get("quotes", data.get("quote", []))

        if normalize(answer).startswith("not stated") and not quotes:
            cite = "none"
        elif text_layer is None:
            cite = "unverifiable"
        else:
            cite = {"ok": "verified", "spliced": "spliced",
                    "bad": "FABRICATED"}[check_quotes(quotes, text_layer)]

        return {"ok": True, "answer": answer, "quotes": quotes,
                "verdict": classify(case["id"], answer), "cite": cite,
                "elapsed": round(time.time() - started, 2)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "verdict": "error",
                "cite": "err", "elapsed": round(time.time() - started, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--dpi", type=int, default=130)
    ap.add_argument("--out", default="benchmark_vision_results.json")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY не задан в .env")
    client = genai.Client(api_key=api_key)

    with open(CASES_PATH, encoding="utf-8") as fh:
        all_cases = {c["id"]: c for c in json.load(fh)}

    workdir = tempfile.mkdtemp(prefix="visionbench_")
    results = {}
    try:
        print("Готовим документы (скачиваются и удаляются, в репозитории не хранятся):")
        imgs, layers = prepare_images(workdir, args.dpi)

        models = [m.strip() for m in args.models.split(",") if m.strip()]
        for model in models:
            print(f"\n=== {model}")
            results[model] = {}
            for cid, spec in VISION_CASES.items():
                case = all_cases[cid]
                doc = spec["doc"]
                pages = spec["pages"] or [0]
                blobs = [imgs[doc][p] for p in pages]
                r = run_case(client, model, case, blobs, layers[doc])
                results[model][cid] = r
                mark = {"correct": "OK  ", "trapped": "TRAP", "other": "??  ",
                        "error": "ERR "}[r["verdict"]]
                detail = r.get("error") or r.get("answer", "")[:60]
                print(f"  {mark} {r['cite']:12} {cid:32} {detail}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"\nВременные файлы удалены: {workdir}")

    print("\n" + "=" * 86)
    print(f"{'model':26} {'correct':>8} {'trapped':>8} {'other':>6} "
          f"{'FABRICATED':>11} {'unverifiable':>13} {'avg s':>7}")
    print("-" * 86)
    summary = {}
    for model in results:
        rs = list(results[model].values())
        c = sum(1 for r in rs if r["verdict"] == "correct")
        t = sum(1 for r in rs if r["verdict"] == "trapped")
        o = sum(1 for r in rs if r["verdict"] in ("other", "error"))
        fab = sum(1 for r in rs if r["cite"] == "FABRICATED")
        unv = sum(1 for r in rs if r["cite"] == "unverifiable")
        avg = round(sum(r["elapsed"] for r in rs) / max(len(rs), 1), 2)
        summary[model] = {"correct": c, "trapped": t, "other": o,
                          "fabricated": fab, "unverifiable": unv,
                          "total": len(rs), "avg_sec": avg}
        print(f"{model:26} {c:>4}/{len(rs):<3} {t:>8} {o:>6} {fab:>11} {unv:>13} {avg:>7}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, ensure_ascii=False, indent=2)
    print(f"\nПодробности: {args.out}")


if __name__ == "__main__":
    main()
