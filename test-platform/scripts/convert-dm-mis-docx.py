#!/usr/bin/env python3
"""Convert the DM_MIS v2.9 Word document into benchmark JSONL files."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def docx_lines(path: Path) -> list[str]:
    with ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))

    def para_text(el) -> str:
        parts: list[str] = []
        for node in el.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t":
                parts.append(node.text or "")
            elif tag == "tab":
                parts.append("\t")
            elif tag in {"br", "cr"}:
                parts.append("\n")
        return "".join(parts)

    lines: list[str] = []
    for pnode in root.findall(".//w:p", NS):
        for line in para_text(pnode).splitlines():
            line = re.sub(r"[ \t]+", " ", line.strip())
            if line:
                lines.append(line)
    return lines


def explicit_section(line: str) -> str | None:
    low = line.lower().strip()
    if low.startswith("--легк"):
        return "Simple"
    if low.startswith("--сред"):
        return "Medium"
    if low.startswith("--слож"):
        return "Hard"
    return None


def is_sql_start(line: str) -> bool:
    return re.match(r"^(select|with)\b", line, re.I) is not None


def starts_cyrillic(line: str) -> bool:
    return re.match(r"^[А-Яа-яЁё]", line) is not None


def looks_like_sql_continuation(line: str) -> bool:
    return re.match(
        r'^(from|where|and|or|on|inner|left|right|full|cross|join|group|order|having|limit|union|case|when|then|else|end|count|sum|avg|min|max|round|row_number|rank|partition|over|cast|date_add|date_sub|add_months|regexp|coalesce|nvl|year|month|day|datediff|months_between|between|as\b|[),]|`|"|[A-Z_][A-Z0-9_\.]*\b)',
        line,
        re.I,
    ) is not None


def clean_question(question: str) -> str:
    question = re.sub(r"\s+", " ", question).strip()
    if "Вопрос:" in question:
        question = question.split("Вопрос:", 1)[1].strip()
    question = re.sub(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", r"\3-\2-\1", question)
    return question


def normalize_sql(sql: str) -> str:
    sql = "\n".join(line.rstrip() for line in sql.strip().splitlines()).strip()
    sql = re.sub(r"\bAS\s+'([^']+)'", r"AS `\1`", sql, flags=re.I)
    sql = re.sub(r'"([^"\n]+)"', r"`\1`", sql)
    sql = re.sub(r"'(\d{4})(\d{2})(\d{2})'", r"'\1-\2-\3'", sql)
    return sql.rstrip(";").strip() + ";"


def infer_difficulty(row: dict) -> str:
    if row["difficulty"] != "Unknown":
        return row["difficulty"]
    sql = row["sql"].lower()
    question = row["question"].lower()
    joins = len(re.findall(r"\bjoin\b", sql))
    ctes = 1 if re.match(r"^with\b", sql.strip(), re.I) else 0
    windows = len(re.findall(r"\bover\s*\(", sql))
    groups = len(re.findall(r"\bgroup\s+by\b", sql))
    tables = len(set(re.findall(r"\b(?:core_tmp|dm_mis)\.([a-z0-9_]+)", sql)))
    lines = len([line for line in sql.splitlines() if line.strip()])
    hard_terms = any(term in question for term in (
        "группам риска",
        "график",
        "динамик",
        "распределение остатков",
        "больше миллиарда",
        "тариф",
        "демография",
        "портфел",
        "кредитн",
    ))
    if ctes or windows or joins >= 3 or tables >= 4 or lines >= 18 or hard_terms:
        return "Hard"
    if joins >= 1 or groups or tables >= 2 or lines >= 8:
        return "Medium"
    return "Simple"


def parse_pairs(lines: list[str]) -> list[dict]:
    pairs: list[dict] = []
    current = "Simple"
    q_section = current
    question_lines: list[str] = []
    sql_lines: list[str] = []
    state = "question"
    skip_toc = False

    def flush() -> None:
        nonlocal question_lines, sql_lines, state
        if question_lines and sql_lines:
            row = {
                "difficulty": q_section,
                "question": clean_question(" ".join(question_lines)),
                "sql": normalize_sql("\n".join(sql_lines)),
            }
            row["difficulty"] = infer_difficulty(row)
            pairs.append(row)
        question_lines = []
        sql_lines = []
        state = "question"

    for line in lines:
        if skip_toc:
            if line == "1 РКО":
                skip_toc = False
                current = "Unknown"
            continue
        if line == "Оглавление":
            flush()
            skip_toc = True
            continue
        explicit = explicit_section(line)
        if explicit:
            flush()
            current = explicit
            continue
        if line == "--ENTITY" or line in {"1 РКО", "2 Счета", "3 Брокерка", "4 Кредиты ЮЛ"}:
            flush()
            current = "Unknown"
            continue
        if state == "question":
            if line == "ОСТАТКИ и КЛИЕНТЫ:":
                continue
            if is_sql_start(line):
                if question_lines:
                    sql_lines = [line]
                    state = "sql"
                continue
            if not question_lines:
                q_section = current
            question_lines.append(line)
        elif starts_cyrillic(line) and not line.startswith("--") and not looks_like_sql_continuation(line):
            flush()
            q_section = current
            question_lines = [line]
        else:
            sql_lines.append(line)

    flush()
    return pairs


def write_benchmark(path: Path, rows: list[dict], title: str) -> None:
    lines = []
    for difficulty in ("Simple", "Medium", "Hard"):
        subset = [(idx, row) for idx, row in enumerate(rows, 1) if row["difficulty"] == difficulty]
        if not subset:
            continue
        prefix = {"Simple": "S", "Medium": "M", "Hard": "H"}[difficulty]
        for local_idx, (idx, row) in enumerate(subset, 1):
            lines.append(json.dumps({
                "benchmark_id": f"{prefix}{local_idx}",
                "case_id": f"dm_mis_{idx}",
                "difficulty": difficulty,
                "question": row["question"],
                "normal_phrasing": "",
                "conditions": "",
                "gold_sql": row["sql"],
                "source": "DM_MIS запросы_v2.9 (1).docx",
                "subset": title,
            }, ensure_ascii=False))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docx", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    args = parser.parse_args()

    pairs = parse_pairs(docx_lines(args.docx))
    if len(pairs) != 54:
        raise SystemExit(f"expected 54 pairs, got {len(pairs)}")
    write_benchmark(args.out_dir / "BENCHMARK_dm_mis_impala_1.jsonl", pairs[:1], "short 1")
    write_benchmark(args.out_dir / "BENCHMARK_dm_mis_impala_3.jsonl", pairs[:3], "short 3")
    write_benchmark(args.out_dir / "BENCHMARK_dm_mis_impala_10.jsonl", pairs[:10], "short 10")
    write_benchmark(args.out_dir / "BENCHMARK_dm_mis_impala.jsonl", pairs, "all v2.9")
    dist = {name: sum(1 for row in pairs if row["difficulty"] == name) for name in ("Simple", "Medium", "Hard")}
    print({"written": len(pairs), "difficulty": dist})


if __name__ == "__main__":
    main()
