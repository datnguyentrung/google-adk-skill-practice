from pathlib import Path
p = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\app\services\ingestion\model_call_control.py")
s = p.read_text(encoding="utf-8")
old = '''        if result is not None:
            return json.loads(result) if isinstance(result, str) else result
        if final_text.strip():
            return json.loads(final_text)
        raise RuntimeError(f"ADK agent returned no structured output for {operation}")
'''
new = '''        try:
            if result is not None:
                return json.loads(result) if isinstance(result, str) else result
            if final_text.strip():
                return json.loads(final_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StructuredModelOutputError(
                f"ADK agent returned invalid structured output for {operation}"
            ) from exc
        raise StructuredModelOutputError(
            f"ADK agent returned no structured output for {operation}"
        )
'''
assert old in s
p.write_text(s.replace(old, new), encoding="utf-8")
print("patched structured output handling")