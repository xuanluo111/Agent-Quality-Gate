from src.engine.variables import extract_variables_from_results


def test_extracts_csv_headers():
    results = [{"name": "file_read", "result": "name,age,salary\n张三,28,12000\n"}]
    assert extract_variables_from_results(results) == ["age", "name", "salary"]


def test_extracts_json_keys_from_list():
    results = [{"name": "file_read", "result": '[{"amount": 1, "currency": "CNY"}]'}]
    assert extract_variables_from_results(results) == ["amount", "currency"]


def test_extracts_key_value_and_markdown_table():
    text = "订单号：A-1\n金额：100\n\n| sku | qty |\n| --- | --- |\n| A | 1 |\n"
    results = [{"name": "file_read", "result": text}]
    variables = extract_variables_from_results(results)
    assert {"订单号", "金额", "sku", "qty"} <= set(variables)


def test_ignores_non_data_tools_and_blocked_results():
    results = [
        {"name": "list_dir", "result": '[{"name": "users.csv", "type": "file", "size": 10}]'},
        {"name": "write_file", "result": "已写入 summary.txt"},
        {"name": "request_human_confirmation", "result": "pending_human_confirmation"},
        {"name": "file_read", "result": "name,age\n张三,28\n"},
        {"name": "file_read", "result": "secret,value", "blocked": True},
    ]
    assert extract_variables_from_results(results) == ["age", "name"]


def test_ignores_ls_but_keeps_cat():
    results = [
        {"name": "execute_command", "arguments": {"command": "ls -la"}, "result": '[{"name": "a", "size": 1}]'},
        {"name": "execute_command", "arguments": {"command": "cat users.csv"}, "result": "name,age\n张三,28\n"},
    ]
    assert extract_variables_from_results(results) == ["age", "name"]
