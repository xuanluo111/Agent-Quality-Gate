import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, List, Tuple

from pydantic import ValidationError

from src.engine.tools import resolve_case_tools
from src.models.schema import TestCase

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def read_jsonl(path: str | Path) -> Tuple[List[dict], List[dict]]:
    records: List[dict] = []
    errors: List[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("{"):
                errors.append({"line": line_number, "error": "不是 JSON 对象"})
                continue
            try:
                record = json.loads(stripped)
                if not isinstance(record, dict):
                    raise ValueError("JSON 顶层不是对象")
                records.append(record)
            except Exception as exc:
                errors.append({"line": line_number, "error": str(exc)})
    return records, errors


def load_cases(path: str | Path, dedupe: bool = True) -> Tuple[List[TestCase], dict]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"数据集不存在: {path}")
    records, parse_errors = read_jsonl(path)
    cases: List[TestCase] = []
    validation_errors: List[dict] = []
    missing_sensitivity = 0
    for index, record in enumerate(records, 1):
        if not record.get("data_sensitivity"):
            missing_sensitivity += 1
        try:
            cases.append(TestCase(**record))
        except ValidationError as exc:
            validation_errors.append({"line": index, "error": exc.errors(include_url=False)})
        except Exception as exc:
            validation_errors.append({"line": index, "error": str(exc)})

    duplicates = 0
    if dedupe:
        seen_ids, seen_inputs, unique = set(), set(), []
        for case in cases:
            input_hash = hashlib.sha256(case.input.strip().encode("utf-8")).hexdigest()
            if case.id in seen_ids or input_hash in seen_inputs:
                duplicates += 1
                continue
            seen_ids.add(case.id)
            seen_inputs.add(input_hash)
            unique.append(case)
        cases = unique

    report = {
        "path": str(path),
        "raw_records": len(records),
        "loaded_cases": len(cases),
        "parse_errors": parse_errors,
        "validation_errors": validation_errors,
        "duplicates": duplicates,
        "missing_sensitivity": missing_sensitivity,
        "dataset_sha256": dataset_sha256(path),
    }
    return cases, report


def dataset_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_jsonl(path: str | Path, cases: Iterable[TestCase]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(target, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case.model_dump(), ensure_ascii=False) + "\n")
            count += 1
    return count


def dedupe_cases(cases: Iterable[TestCase]) -> Tuple[List[TestCase], int]:
    items = list(cases)
    seen_ids, seen_inputs, unique = set(), set(), []
    for case in items:
        input_hash = hashlib.sha256(case.input.strip().encode("utf-8")).hexdigest()
        if case.id in seen_ids or input_hash in seen_inputs:
            continue
        seen_ids.add(case.id)
        seen_inputs.add(input_hash)
        unique.append(case)
    return unique, len(items) - len(unique)


def stratified_sample(cases: List[TestCase], limit: int, seed: int) -> Tuple[List[TestCase], dict]:
    if limit <= 0 or limit >= len(cases):
        return list(cases), {
            "method": "full",
            "seed": seed,
            "population": len(cases),
            "sample": len(cases),
            "by_category": dict(Counter(case.category for case in cases)),
            "by_severity": dict(Counter(case.severity for case in cases)),
        }

    groups: dict[str, List[TestCase]] = defaultdict(list)
    for case in cases:
        groups[case.category].append(case)
    for group in groups.values():
        group.sort(key=lambda case: (SEVERITY_RANK.get(case.severity, 9), case.id))

    categories = sorted(groups)
    if limit < len(categories):
        categories = sorted(
            categories,
            key=lambda name: (SEVERITY_RANK.get(groups[name][0].severity, 9), len(groups[name])),
            reverse=False,
        )[:limit]
        selected = [groups[name][0] for name in categories]
    else:
        quotas = {name: max(1, math.floor(limit * len(groups[name]) / len(cases))) for name in categories}
        while sum(quotas.values()) > limit:
            largest = max((name for name in categories if quotas[name] > 1), key=lambda name: quotas[name])
            quotas[largest] -= 1
        selected = []
        for name in categories:
            selected.extend(groups[name][: quotas[name]])

    selected_ids = {case.id for case in selected}
    remaining = [case for case in cases if case.id not in selected_ids]
    remaining.sort(key=lambda case: (SEVERITY_RANK.get(case.severity, 9), case.category, case.id))
    rng = random.Random(seed)
    rng.shuffle(remaining)
    remaining.sort(key=lambda case: SEVERITY_RANK.get(case.severity, 9))
    selected.extend(remaining[: limit - len(selected)])

    return selected, {
        "method": "stratified_category_severity",
        "seed": seed,
        "population": len(cases),
        "sample": len(selected),
        "by_category": dict(Counter(case.category for case in selected)),
        "by_severity": dict(Counter(case.severity for case in selected)),
    }


def wilson_interval(successes: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, center - margin) * 100, min(1.0, center + margin) * 100


def select_smoke(cases: List[TestCase], max_cases: int = 12) -> List[TestCase]:
    def score(case: TestCase) -> tuple:
        feature = 0
        if case.expected_variables:
            feature += 4
        if case.requires_human_confirm:
            feature += 4
        if case.data_sensitivity in {"S2", "S3"}:
            feature += 4
        if case.fixtures:
            feature += 2
        if any(tag.startswith("feature:") for tag in case.tags):
            feature += 3
        return (-feature, SEVERITY_RANK.get(case.severity, 9), case.category, case.id)

    ordered = sorted(cases, key=score)
    selected: List[TestCase] = []
    categories = set()
    for case in ordered:
        if len(selected) >= max_cases:
            break
        if case.category not in categories or len(selected) < 6:
            selected.append(case)
            categories.add(case.category)
    for case in ordered:
        if len(selected) >= max_cases:
            break
        if case not in selected:
            selected.append(case)
    return selected


def audit_cases(cases: List[TestCase], load_report: dict | None = None) -> dict:
    unsupported = Counter()
    for case in cases:
        _, missing = resolve_case_tools(case.allowed_tools, case.requires_human_confirm)
        unsupported.update(missing)
    return {
        "total": len(cases),
        "by_category": dict(Counter(case.category for case in cases)),
        "by_severity": dict(Counter(case.severity for case in cases)),
        "by_sensitivity": dict(Counter(case.data_sensitivity for case in cases)),
        "expected_variables_cases": sum(1 for case in cases if case.expected_variables),
        "hitl_cases": sum(1 for case in cases if case.requires_human_confirm),
        "fixture_cases": sum(1 for case in cases if case.fixtures),
        "unsupported_tools": dict(unsupported.most_common(30)),
        "missing_sensitivity": (load_report or {}).get("missing_sensitivity", 0),
        "duplicates": (load_report or {}).get("duplicates", 0),
        "parse_errors": len((load_report or {}).get("parse_errors", [])),
        "validation_errors": len((load_report or {}).get("validation_errors", [])),
    }


def build_suite(
    input_paths: Iterable[str],
    output_path: str,
    smoke_path: str | None = None,
    exclude_unsupported: bool = True,
) -> dict:
    all_cases: List[TestCase] = []
    reports = []
    excluded: List[dict] = []
    for path in input_paths:
        if not Path(path).is_file():
            continue
        cases, report = load_cases(path, dedupe=True)
        label = Path(path).stem
        for case in cases:
            case.source = case.source or label
            if f"source:{label}" not in case.tags:
                case.tags.append(f"source:{label}")
            _, missing = resolve_case_tools(case.allowed_tools, case.requires_human_confirm)
            if exclude_unsupported and missing:
                excluded.append({"id": case.id, "source": label, "unsupported_tools": missing})
                continue
            all_cases.append(case)
        reports.append(report)
    unique, duplicates = dedupe_cases(all_cases)
    unique.sort(key=lambda case: (case.category, SEVERITY_RANK.get(case.severity, 9), case.id))
    written = write_jsonl(output_path, unique)
    result = {
        "inputs": [str(path) for path in input_paths if Path(path).is_file()],
        "output": output_path,
        "written": written,
        "duplicates_removed": duplicates,
        "excluded_unsupported": excluded,
        "load_reports": reports,
        "audit": audit_cases(unique),
    }
    if smoke_path:
        smoke = select_smoke(unique)
        result["smoke_output"] = smoke_path
        result["smoke_written"] = write_jsonl(smoke_path, smoke)
        result["smoke_audit"] = audit_cases(smoke)
    return result
