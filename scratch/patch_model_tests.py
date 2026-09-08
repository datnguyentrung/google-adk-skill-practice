from pathlib import Path
p = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\tests\test_model_call_control.py")
s = p.read_text(encoding="utf-8-sig")
s = s.replace("    StageLocalModelCallExhausted,\n)", "    StageLocalModelCallExhausted,\n    StructuredModelOutputError,\n)")
append = '''\n\ndef test_missing_structured_output_is_retryable():\n    runner = _FakeRunner(result=None)\n    executor = AdkStructuredCallExecutor(\n        model="gemini-test", rpm_budget=0, runner_factory=lambda **_kwargs: runner\n    )\n    with pytest.raises(StructuredModelOutputError) as caught:\n        executor.run(operation="mapping", instruction="Return JSON.", output_schema={"type": "object"})\n    assert caught.value.retryable is True\n    assert caught.value.error_kind == "llm_output"\n    assert runner.closed is True\n'''
if "test_missing_structured_output_is_retryable" not in s:
    s += append
p.write_text(s, encoding="utf-8")
print("patched tests")