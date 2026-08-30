import importlib


def _reload_prompt():
    import prompt

    importlib.reload(prompt)
    return prompt


def test_parse_llm_json_repairs_missing_commas_and_trailing_commas():
    prompt = _reload_prompt()
    raw = """```json
{
  "a": 1
  "b": 2,
  "arr": [
    {"x": 1}
    {"y": 2}
  ],
}
```"""
    data = prompt._parse_llm_json(raw, label="test")
    assert data["a"] == 1
    assert data["b"] == 2
    assert isinstance(data["arr"], list)
    assert len(data["arr"]) == 2


def test_parse_llm_json_strips_comments_and_extra_tail_text():
    prompt = _reload_prompt()
    raw = """{
  "ok": true, // comment
  "name": "chen"
}
已经写好了"""
    data = prompt._parse_llm_json(raw, label="test")
    assert data["ok"] is True
    assert data["name"] == "chen"


def test_parse_llm_json_falls_back_to_python_literal():
    prompt = _reload_prompt()
    raw = """{
  'ok': true,
  'count': 3
}"""
    data = prompt._parse_llm_json(raw, label="test")
    assert data["ok"] is True
    assert data["count"] == 3
