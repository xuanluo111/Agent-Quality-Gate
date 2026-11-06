"""探针集与 curated 主数据集的一致性校验。

`cases/attack_probe.jsonl` 是 curated 里 4 条用例的**子集副本**，用于低成本真机
验证叠加策略。它与主数据集并存，两边各自改就会漂移——探针集验的其实是旧版本
用例，结论无效。所以这里锁住：探针集的每一条必须与 curated 里同 id 的条目
**字节级一致**，且 id 集合固定。
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURATED = ROOT / "cases" / "curated_cases.jsonl"
PROBE = ROOT / "cases" / "attack_probe.jsonl"
MATRIX = ROOT / "models.attack_probe.json"

EXPECTED_IDS = {
    "mut-sudo-gradual-001",
    "mut-v2-sudo-stack-001",
    "mut-v2-sudo-urgent-001",
    "mut-v2-canary-stack-001",
}


def _rows(path: Path) -> dict:
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[row["id"]] = line.rstrip("\n")
    return rows


def test_probe_set_contains_expected_cases():
    rows = _rows(PROBE)
    assert set(rows) == EXPECTED_IDS


def test_probe_set_matches_curated_byte_for_byte():
    """探针集必须与 curated 同 id 条目字节一致：否则探针跑的是旧版用例。"""
    curated = _rows(CURATED)
    for case_id, raw in _rows(PROBE).items():
        assert case_id in curated, f"{case_id} 在 curated 里已不存在，探针集该重建"
        assert raw == curated[case_id], f"{case_id} 与 curated 不一致，探针集已漂移"


def test_probe_matrix_points_at_probe_dataset():
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert matrix["defaults"]["dataset"] == "cases/attack_probe.jsonl"
    # limit=0 = 全跑该数据集（不抽样）；repeat=1 = 不做 pass^k 重复，省成本
    assert matrix["defaults"]["limit"] == 0
    assert matrix["defaults"]["repeat"] == 1
    for spec in matrix["models"]:
        # 每个模型都得声明取哪个 key，否则子进程拿不到凭据
        assert spec.get("api_key_env")


def test_probe_matrix_declares_current_core_generation():
    """探针集必须声明自己属于当前语料代际。

    探针集是 core 语料的**视图**，文件指纹不同。不声明的话，台账会把它当成
    “下一代数据集”：伪造一次代际切换，并把跨代劣化护栏的基准指到子集文件上。
    语料重建后 epoch 会变，所以这条断言能挡住“声明过期”。
    """
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    declared = matrix["defaults"].get("dataset_generation")
    assert declared, "探针矩阵必须声明 dataset_generation"
    core_epoch = hashlib.sha256((ROOT / "cases" / "core_suite.jsonl").read_bytes()).hexdigest()[:16]
    assert declared == core_epoch, (
        f"探针矩阵声明的代际 {declared} 与当前 core 语料代际 {core_epoch} 不一致，"
        "语料重建后要同步更新 models.attack_probe.json"
    )