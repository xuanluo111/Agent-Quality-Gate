"""pack_originality 的测试：空答拦截、凭证拦截、成包内容。

这个脚本的产出是「原创性」这一审核项的唯一物证，属于提交材料，
所以它自己的正确性也要能被验证（否则包里少一页、少一题都没人发现）。
"""

from __future__ import annotations

import zipfile

from scripts import pack_originality as po

DRAFT_OK = """# 底稿

### 段 1｜体系

- **问 1.1**　一句话怎么答？
  答：就是拿一堆带禁用动作的用例去压它，看它手会不会伸出去。

- **问 1.2**　为什么不交给大模型打分？
  答：不确定。

### 段 2｜逻辑

- **问 2.1**　过几道关？
  答：四道。
"""

DRAFT_EMPTY = """# 底稿

- **问 1.1**　一句话怎么答？
  答：

- **问 1.2**　为什么不交给大模型打分？
  答：不确定。
"""


def test_parse_qa_keeps_order_and_answers():
    items = po.parse_qa(DRAFT_OK)
    assert [it["qid"] for it in items] == ["1.1", "1.2", "2.1"]
    assert items[0]["answer"].startswith("就是拿一堆")
    # 「不确定」也是回答，不能被当成空答
    assert items[1]["answer"] == "不确定。"


def test_unanswered_lists_only_blank_ones():
    assert po.unanswered(po.parse_qa(DRAFT_OK)) == []
    assert po.unanswered(po.parse_qa(DRAFT_EMPTY)) == ["1.1"]


def test_find_suspects_catches_real_looking_secrets():
    text = "顺手贴了 sk-abcdefghijklmnop1234 和 /Users/someuser/Desktop 的路径"
    labels = [label for label, _ in po.find_suspects(text)]
    assert "OpenAI 风格密钥" in labels
    assert "本地绝对路径" in labels


def test_find_suspects_ignores_placeholder_examples():
    """底稿里本来就写着 `sk-…` 这种示意，不能被当成真凭证拦下来。"""
    text = "提交材料里出现的 `sk-…` / `prod-secret-…` 均为构造的假凭证。"
    assert po.find_suspects(text) == []


def test_main_refuses_when_answers_are_blank(tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text(DRAFT_EMPTY, encoding="utf-8")
    out = tmp_path / "out.zip"

    code = po.main(["--draft", str(draft), "--out", str(out)])

    assert code == 2
    assert not out.exists()


def test_main_refuses_secrets_but_never_prints_them(tmp_path, capsys):
    draft = tmp_path / "draft.md"
    draft.write_text(
        DRAFT_OK.replace("答：四道。", "答：四道，我的 key 是 sk-abcdefghijklmnop9999"),
        encoding="utf-8",
    )
    out = tmp_path / "out.zip"

    code = po.main(["--draft", str(draft), "--out", str(out)])

    assert code == 3
    assert not out.exists()
    err = capsys.readouterr().err
    assert "OpenAI 风格密钥" in err
    # 报错只说类别与前 12 字符，绝不回显完整凭证
    assert "sk-abcdefghijklmnop9999" not in err


def test_main_builds_zip_with_proof_and_draft(tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text(DRAFT_OK, encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("# 报告\n", encoding="utf-8")
    out = tmp_path / "out.zip"

    code = po.main(
        ["--draft", str(draft), "--report", str(report), "--out", str(out)]
    )

    assert code == 0
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert names == ["00-时间戳证明.md", "01-正文问答底稿.md"]
        proof = zf.read("00-时间戳证明.md").decode("utf-8")
        # 题数与分段统计要真的进包，否则审核方看到的是一张空表
        assert "合计 3 题，已答 3 题" in proof
        assert "| 段 1 | 2 | 2 |" in proof
        assert "| 段 2 | 1 | 1 |" in proof
        assert zf.read("01-正文问答底稿.md").decode("utf-8") == DRAFT_OK


# ---------- 待拍板 #11：外发前身份预检 ----------

FAKE_USER = "testid-user"
FAKE_HOME = "/Users/testid-user"


def fake_identity(monkeypatch):
    monkeypatch.setenv("USER", FAKE_USER)
    monkeypatch.setenv("LOGNAME", FAKE_USER)
    monkeypatch.setenv("HOME", FAKE_HOME)


def test_scan_text_for_identity_finds_user_and_home():
    tokens = po.identity_tokens(FAKE_USER, FAKE_HOME)
    assert po.scan_text_for_identity(f"路径 {FAKE_HOME}/x 与用户 {FAKE_USER}", tokens) == \
        sorted([FAKE_USER, FAKE_HOME])
    assert po.scan_text_for_identity("干净文本 someuser", tokens) == []
    # 空 token 集（CI 通用身份）不得把任何文件报成命中
    assert po.scan_text_for_identity("anything", po.identity_tokens("runner", "/home/runner")) == []


def test_iter_scan_files_skips_binary_and_expands_dirs(tmp_path):
    (tmp_path / "a.md").write_text(f"{FAKE_USER}\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"\0" + FAKE_USER.encode())
    (sub / "c.txt").write_text("clean\n", encoding="utf-8")
    files = po.iter_scan_files([tmp_path])
    names = sorted(f.name for f in files)
    assert names == ["a.md", "c.txt"]  # 二进制被 NUL 嗅探跳过，目录被递归展开


def test_main_refuses_pack_when_draft_contains_identity(tmp_path, monkeypatch, capsys):
    fake_identity(monkeypatch)
    draft = tmp_path / "draft.md"
    draft.write_text(DRAFT_OK + f"\n备注：发给 {FAKE_USER}\n", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("# 报告\n", encoding="utf-8")
    out = tmp_path / "out.zip"

    code = po.main(["--draft", str(draft), "--report", str(report), "--out", str(out)])

    assert code == 4
    assert not out.exists()
    assert FAKE_USER in capsys.readouterr().err


def test_main_scan_paths_hit_in_archive_refuses_pack(tmp_path, monkeypatch):
    """包里去的文件都干净，但点名的本机归档含身份——同样拒绝成包（#11 的扩面动机）。"""
    fake_identity(monkeypatch)
    draft = tmp_path / "draft.md"
    draft.write_text(DRAFT_OK, encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("# 报告\n", encoding="utf-8")
    archive = tmp_path / ".reports-archive-x"
    archive.mkdir()
    (archive / "note.md").write_text(f"归属 {FAKE_HOME}\n", encoding="utf-8")
    out = tmp_path / "out.zip"

    code = po.main(["--draft", str(draft), "--report", str(report), "--out", str(out),
                    "--scan-paths", str(archive)])

    assert code == 4 and not out.exists()


def test_main_scan_only_reports_without_packing(tmp_path, monkeypatch):
    fake_identity(monkeypatch)
    archive = tmp_path / "ws"
    archive.mkdir()
    (archive / "f.md").write_text(f"{FAKE_USER}\n", encoding="utf-8")
    out = tmp_path / "out.zip"

    code = po.main(["--draft", str(tmp_path / "missing.md"),
                    "--scan-only", "--scan-paths", str(archive), "--out", str(out)])

    assert code == 4
    assert not out.exists()  # scan-only 永远不成包，哪怕目录干净
    clean = tmp_path / "clean.md"
    clean.write_text("没事\n", encoding="utf-8")
    assert po.main(["--draft", str(clean), "--scan-only"]) == 0


def test_main_identity_gate_is_noop_without_real_identity(tmp_path, monkeypatch):
    """CI 里的通用身份（runner）不该把任何包拦下来：tokens 为空则预检直接放行。"""
    monkeypatch.setenv("USER", "runner")
    monkeypatch.setenv("LOGNAME", "runner")
    monkeypatch.setenv("HOME", "/home/runner")
    draft = tmp_path / "draft.md"
    draft.write_text(DRAFT_OK, encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("# 报告\n", encoding="utf-8")
    out = tmp_path / "out.zip"

    assert po.main(["--draft", str(draft), "--report", str(report), "--out", str(out)]) == 0
    assert out.exists()
