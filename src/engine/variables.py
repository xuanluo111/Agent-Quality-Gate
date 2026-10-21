import csv
import io
import json
import re
from typing import Any, Iterable, List, Set

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEY_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])([\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z0-9_]*)\s*[=:：]\s*[^\s,;]+"
)
JSON_START = ("{", "[")


def _add_token(token: str, output: Set[str]) -> None:
    name = str(token).strip().strip("\"'`")
    if not name or len(name) > 64:
        return
    if name.lower() in {"true", "false", "null", "none", "nan", "n/a"}:
        return
    if IDENTIFIER_RE.match(name) or re.match(r"^[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_]*$", name):
        output.add(name)


def _collect(value: Any, output: Set[str], depth: int = 0) -> None:
    if depth > 5:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _add_token(str(key), output)
            _collect(item, output, depth + 1)
        return
    if isinstance(value, list):
        for item in value[:50]:
            _collect(item, output, depth + 1)
        return
    if not isinstance(value, str):
        return

    text = value.strip()
    if not text:
        return

    if text.startswith(JSON_START):
        try:
            _collect(json.loads(text), output, depth + 1)
            return
        except (json.JSONDecodeError, TypeError):
            pass

    for match in KEY_VALUE_RE.finditer(text):
        _add_token(match.group(1), output)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2 and "," in lines[0] and "|" not in lines[0]:
        try:
            reader = csv.reader(io.StringIO("\n".join(lines[:3])))
            rows = list(reader)
            if rows and len(rows[0]) > 1:
                for header in rows[0]:
                    _add_token(header, output)
                return
        except csv.Error:
            pass

    table_lines = [line for line in lines if line.startswith("|") and line.endswith("|")]
    if len(table_lines) >= 2:
        headers = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
        for header in headers:
            _add_token(header, output)


def _is_data_bearing(item: dict) -> bool:
    name = str(item.get("name") or "")
    if name in {"list_dir", "write_file", "request_human_confirmation"}:
        return False
    if name == "execute_command":
        arguments = item.get("arguments") or {}
        command = str(arguments.get("command", "") if isinstance(arguments, dict) else "").strip()
        program = command.split()[0] if command else ""
        if program in {"ls", "pwd"}:
            return False
    return True


def extract_variables_from_results(tool_results: Iterable[dict]) -> List[str]:
    output: Set[str] = set()
    for item in tool_results:
        if not isinstance(item, dict) or item.get("blocked"):
            continue
        if not _is_data_bearing(item):
            continue
        _collect(item.get("result"), output)
    return sorted(output)
