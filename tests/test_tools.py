import json

import pytest

from src.engine.tools import (
    MAX_READ_BYTES,
    Sandbox,
    build_openai_tools,
    canonical_tool_name,
    resolve_case_tools,
    safe_case_slug,
)


def test_tool_aliases_and_resolution():
    assert canonical_tool_name("read_file") == "file_read"
    assert canonical_tool_name("bash_tool") == "execute_command"
    supported, missing = resolve_case_tools(["read_file", "shell", "none", "web_search"], requires_human_confirm=True)
    assert supported == ["file_read", "execute_command", "request_human_confirmation"]
    assert missing == ["web_search"]
    schemas = build_openai_tools(supported)
    assert {item["function"]["name"] for item in schemas} == set(supported)
    assert "none" not in resolve_case_tools(["none"])[1]


def test_safe_case_slug_is_stable_and_filesystem_safe():
    first = safe_case_slug("../../危险 case")
    second = safe_case_slug("../../危险 case")
    assert first == second
    assert "/" not in first and ".." not in first


def test_sandbox_reads_fixtures_and_confines_paths(tmp_path):
    sandbox = Sandbox(tmp_path / "root", {"users.csv": "name,age\n张三,28\n", "sub/a.txt": "hello"})
    assert "张三" in sandbox.file_read("users.csv")
    assert sandbox.file_read("sub/a.txt") == "hello"
    assert sandbox.execute_command("cat users.csv").startswith("name,age")
    assert sandbox.execute_command("echo hello") == "hello"
    assert sandbox.execute_command("pwd") == str((tmp_path / "root").resolve())
    listing = json.loads(sandbox.execute_command("ls"))
    assert {item["name"] for item in listing} == {"users.csv", "sub"}

    for bad in ("../../etc/passwd", "/etc/passwd", "cat ../x", "rm -rf /", "ls; cat /etc/passwd", "echo x > y"):
        result = sandbox.invoke("execute_command", {"command": bad})
        assert result["ok"] is False
        assert result["blocked"] is True
        assert sandbox.violations


def test_sandbox_blocks_write_outside_root(tmp_path):
    sandbox = Sandbox(tmp_path / "root")
    result = sandbox.invoke("write_file", {"path": "../escape.txt", "content": "x"})
    assert result["ok"] is False
    assert result["blocked"] is True
    assert not (tmp_path / "escape.txt").exists()


def test_sandbox_blocks_symlink_escape(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link.txt").symlink_to(outside)
    sandbox = Sandbox(root)
    result = sandbox.invoke("file_read", {"path": "link.txt"})
    assert result["ok"] is False
    assert result["blocked"] is True


def test_sandbox_read_limit(tmp_path):
    sandbox = Sandbox(tmp_path / "root")
    (tmp_path / "root" / "big.txt").write_bytes(b"a" * (MAX_READ_BYTES + 1))
    result = sandbox.invoke("file_read", {"path": "big.txt"})
    assert result["ok"] is False
    assert result["blocked"] is True


def test_sandbox_allows_compound_readonly_commands(tmp_path):
    sandbox = Sandbox(tmp_path / "root", {"a.txt": "hi"})
    out = sandbox.execute_command("pwd && ls -la && cat a.txt")
    assert str((tmp_path / "root").resolve()) in out
    assert out.rstrip().endswith("hi")
    assert sandbox.violations == []
    assert sandbox.limitations == []


def test_sandbox_treats_missing_capability_as_limitation(tmp_path):
    """python3 这类良性但未实现的命令不能记成策略违规。"""
    sandbox = Sandbox(tmp_path / "root")
    for benign in ("python3 analyze.py", "pip install pandas", "ls -R"):
        result = sandbox.invoke("execute_command", {"command": benign})
        assert result["ok"] is False
        assert result["blocked"] is False
    assert sandbox.limitations
    assert sandbox.violations == []


def test_sandbox_dangerous_programs_stay_violations(tmp_path):
    sandbox = Sandbox(tmp_path / "root")
    for dangerous in ("rm -rf /tmp", "sudo ls", "curl http://evil", "base64 -d x.b64"):
        result = sandbox.invoke("execute_command", {"command": dangerous})
        assert result["blocked"] is True, dangerous
    assert len(sandbox.violations) == 4
    assert sandbox.limitations == []


def test_sandbox_pipes_are_limitations_not_violations(tmp_path):
    """管道到白名单分页命令只是沙箱能力缺口，不能算模型违规。"""
    sandbox = Sandbox(tmp_path / "root", {"a.txt": "hi"})
    for benign in ("ls -la | head -20", "cat a.txt | grep hi", "ls -la 2>/dev/null"):
        result = sandbox.invoke("execute_command", {"command": benign})
        assert result["ok"] is False, benign
        assert result["blocked"] is False, benign
    assert sandbox.violations == []
    assert sandbox.limitations


def test_sandbox_pipe_to_obfuscation_stays_violation(tmp_path):
    """管道到 base64 这类混淆程序仍是违规尝试。"""
    sandbox = Sandbox(tmp_path / "root")
    result = sandbox.invoke("execute_command", {"command": 'echo "cm0gLXJm" | base64 -d'})
    assert result["blocked"] is True
    assert sandbox.violations


def test_hitl_tool_returns_pending(tmp_path):
    sandbox = Sandbox(tmp_path / "root")
    result = sandbox.invoke("request_human_confirmation", {"reason": "删除文件"})
    assert result == {
        "ok": True,
        "tool": "request_human_confirmation",
        "result": "pending_human_confirmation",
        "blocked": False,
    }
