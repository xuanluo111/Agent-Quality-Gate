import json

input_file = "cases/stress.jsonl"
output_file = "cases/stress_cleaned.jsonl"

# 严重等级标准化映射
severity_map = {
    "严重": "critical", "高": "high", "中": "medium", "低": "low",
    "critical": "critical", "high": "high", "medium": "medium", "low": "low"
}

# 类别标准化映射
category_map = {
    "权限边界": "permission_boundary",
    "工具误用": "tool_misuse",
    "提示注入": "prompt_injection",
    "幻觉": "hallucination"
}

cleaned_cases = []
id_counter = {}

with open(input_file, "r", encoding="utf-8") as f:
    for line_num, line in enumerate(f, 1):
        line = line.strip()
        # 跳过残缺行
        if not line.startswith("{") or not line.endswith("}"):
            print(f"⚠️ 跳过第{line_num}行（格式残缺）")
            continue
        try:
            obj = json.loads(line)
            
            # 1. 标准化严重等级
            raw_severity = str(obj.get("severity", "medium")).strip()
            obj["severity"] = severity_map.get(raw_severity, "medium")

            # 2. 标准化类别
            raw_category = str(obj.get("category", "")).strip()
            obj["category"] = category_map.get(raw_category, raw_category)

            # 3. 修复重复ID
            base_id = str(obj.get("id", "unknown")).replace("_", "-").strip()
            if base_id in id_counter:
                id_counter[base_id] += 1
                obj["id"] = f"{base_id}-{id_counter[base_id]}"
            else:
                id_counter[base_id] = 1
                obj["id"] = base_id

            cleaned_cases.append(json.dumps(obj, ensure_ascii=False))
        except json.JSONDecodeError as e:
            print(f"❌ 第{line_num}行 JSON 解析失败，跳过: {e}")

# 写入清洗后的文件
with open(output_file, "w", encoding="utf-8") as f:
    for line in cleaned_cases:
        f.write(line + "\n")

print(f"\n✅ 清洗完成！有效用例: {len(cleaned_cases)} 条")
print(f"📂 已保存到: {output_file}")