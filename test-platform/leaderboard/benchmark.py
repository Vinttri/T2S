from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BenchmarkCase:
    benchmark_id: str
    case_id: str
    difficulty: str
    question: str
    normal_phrasing: str
    conditions: str
    gold_sql: str


SECTION_RE = re.compile(r"^##\s+([A-Za-z-]+)", re.MULTILINE)
CASE_RE = re.compile(
    r"^###\s+([SMHE]\d+)\.\s+`([^`]+)`(?P<body>.*?)(?=^###\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _normalise_sql(sql: str) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    return f"{sql};" if sql else ""


def _normalise_conditions(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _case_from_obj(obj: dict, idx: int) -> BenchmarkCase:
    question = obj.get("question") or obj.get("natural") or obj.get("question_natural") or ""
    case_id = obj.get("case_id") or obj.get("id") or f"case_{idx}"
    benchmark_id = obj.get("benchmark_id") or obj.get("benchmark") or str(idx)
    return BenchmarkCase(
        benchmark_id=str(benchmark_id),
        case_id=str(case_id),
        difficulty=str(obj.get("difficulty") or "Unknown"),
        question=str(question),
        normal_phrasing=str(obj.get("normal_phrasing") or obj.get("normal_query") or obj.get("normal") or ""),
        conditions=_normalise_conditions(obj.get("conditions")),
        gold_sql=_normalise_sql(str(obj.get("gold_sql") or obj.get("sql") or "")),
    )


def _conditions_to_json(value: str) -> object:
    text = (value or "").strip()
    if not text:
        return ""
    if text[0] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _validated_case_from_obj(obj: object, idx: int, *, source: str) -> BenchmarkCase:
    if not isinstance(obj, dict):
        raise ValueError(f"invalid {source} item {idx}: expected object")
    case = _case_from_obj(obj, idx)
    if not case.question:
        raise ValueError(f"invalid {source} item {idx}: missing question")
    if not case.gold_sql:
        raise ValueError(f"invalid {source} item {idx}: missing gold_sql")
    return case


def benchmark_case_to_json(case: BenchmarkCase) -> dict:
    return {
        "benchmark_id": case.benchmark_id,
        "case_id": case.case_id,
        "difficulty": case.difficulty,
        "question": case.question,
        "normal_phrasing": case.normal_phrasing,
        "conditions": _conditions_to_json(case.conditions),
        "gold_sql": case.gold_sql,
    }


def benchmark_cases_to_jsonl(cases: list[BenchmarkCase]) -> str:
    return "\n".join(
        json.dumps(benchmark_case_to_json(case), ensure_ascii=False)
        for case in cases
    ) + ("\n" if cases else "")


def parse_benchmark_jsonl(path) -> list[BenchmarkCase]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_benchmark_jsonl_text(text)


def parse_benchmark_jsonl_text(text: str) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for idx, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL line {idx}: {exc.msg}") from exc
        cases.append(_validated_case_from_obj(obj, idx, source="JSONL line"))
    return cases


def parse_legacy_benchmark_markdown(path) -> list[BenchmarkCase]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_legacy_benchmark_markdown_text(text)


def parse_legacy_benchmark_markdown_text(text: str) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    section_spans = list(SECTION_RE.finditer(text))
    for case_match in CASE_RE.finditer(text):
        body = case_match.group("body")
        difficulty = "Unknown"
        for section in section_spans:
            if section.start() < case_match.start():
                difficulty = section.group(1)
        natural = _capture(body, r"\*\*Question \(natural\):\*\*\s*(.*)")
        normal = _capture(body, r"\*\*Normal phrasing:\*\*\s*(.*)")
        conditions = _capture(body, r"\*\*Conditions:\*\*\s*`([^`]+)`")
        gold = _capture(body, r"\*\*Gold SQL:\*\*\s*```sql\s*(.*?)```", flags=re.DOTALL)
        cases.append(
            BenchmarkCase(
                benchmark_id=case_match.group(1),
                case_id=case_match.group(2),
                difficulty=difficulty,
                question=natural,
                normal_phrasing=normal,
                conditions=conditions,
                gold_sql=_normalise_sql(gold),
            )
        )
    return cases


def parse_benchmark_text(text: str, *, source_name: str = "") -> list[BenchmarkCase]:
    suffix = Path(source_name or "").suffix.lower()
    if suffix in ("", ".jsonl", ".ndjson"):
        return parse_benchmark_jsonl_text(text)
    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cases") or []
        if not isinstance(data, list):
            raise ValueError("invalid JSON benchmark: expected list or {cases: [...]}")
        return [_validated_case_from_obj(obj, idx, source="JSON benchmark") for idx, obj in enumerate(data, 1)]
    if suffix in (".md", ".markdown"):
        raise ValueError("benchmark datasets must be JSONL; Markdown is legacy import-only")
    return parse_benchmark_jsonl_text(text)


def parse_benchmark_file(path) -> list[BenchmarkCase]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        return parse_benchmark_jsonl(path)
    if suffix == ".json":
        return parse_benchmark_text(path.read_text(encoding="utf-8"), source_name=str(path))
    if suffix in (".md", ".markdown"):
        raise ValueError("benchmark datasets must be JSONL; Markdown is legacy import-only")
    return parse_benchmark_jsonl(path)


def _capture(text: str, pattern: str, *, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""
