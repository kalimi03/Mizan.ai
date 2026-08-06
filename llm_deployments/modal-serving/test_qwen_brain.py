"""
Manual smoke tests for QwenBrain (/generate).

Confirm the URL against the Modal dashboard before running — it's derived
from the class name, so it changes whenever the class is renamed:
    Apps -> mizan-models -> QwenBrain -> (URL shown at the top)

Run: python test_qwen_brain.py
"""

import json
import time

import requests

QWEN_BRAIN_URL = "https://kalimi03--mizan-models-qwenbrain-generate.modal.run"

CALCULATE_VAT_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate_vat",
        "description": "Calculate VAT for a given invoice amount in SAR",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Invoice amount in SAR"}
            },
            "required": ["amount"],
        },
    },
}


def call(payload: dict) -> dict:
    start = time.time()
    resp = requests.post(QWEN_BRAIN_URL, json=payload, timeout=300)
    elapsed = time.time() - start
    print(f"Status: {resp.status_code}  ({elapsed:.1f}s)")
    resp.raise_for_status()
    return resp.json()


def test_plain_request():
    """No tools passed — should behave like a normal chat model."""
    print("\n=== Test 1: plain conversational request (no tools) ===")
    result = call({
        "messages": [
            {"role": "user", "content": "In one sentence, what is ZATCA Phase 2 e-invoicing?"}
        ],
        "max_tokens": 128,
        "temperature": 0.3,
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["tool_calls"] == [], "expected no tool_calls when tools weren't offered"
    assert result["content"].strip(), "expected non-empty plain-text content"
    print("PASS: plain-text response, tool_calls is empty as expected.")


def test_arabic():
    """Arabic input and output — this is billed as an Arabic-first platform."""
    print("\n=== Test 2: Arabic input/output ===")
    result = call({
        "messages": [
            {"role": "user", "content": "ما هي المرحلة الثانية من الفوترة الإلكترونية في هيئة الزكاة والضريبة والجمارك؟ أجب بجملة واحدة."}
        ],
        "max_tokens": 200,
        "temperature": 0.3,
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["content"].strip(), "expected non-empty content"
    # Eyeball this one manually: does the response actually read as coherent
    # Arabic, or is it garbled / mixed-script / mistokenized?
    print("Manually verify the content above reads as coherent Arabic.")


def test_tool_call_initial():
    """First turn: model should decide to call calculate_vat and emit a
    structured tool_calls entry (not raw <tool_call> text in content)."""
    print("\n=== Test 3a: initial tool call ===")
    result = call({
        "messages": [
            {"role": "user", "content": "What is the VAT rate for a SAR 1000 invoice? Use the calculate_vat tool."}
        ],
        "max_tokens": 256,
        "temperature": 0.1,
        "tools": [CALCULATE_VAT_TOOL],
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["tool_calls"], "expected the model to call calculate_vat"
    call_info = result["tool_calls"][0]
    assert call_info["name"] == "calculate_vat"
    assert call_info["arguments"].get("amount") == 1000
    print("PASS: model correctly called calculate_vat(amount=1000).")
    return call_info


def test_tool_call_followup(call_info: dict):
    """Second turn: feed the tool's result back and confirm the model uses
    it to produce a final natural-language answer (not another tool call).

    NOTE: GenerateRequest's ChatMessage only has {role, content} — no
    tool_call_id / structured tool_calls field for the assistant turn. We
    reconstruct Qwen's expected <tool_call> tag format manually here so the
    model has the same context it would have generated itself.
    """
    print("\n=== Test 3b: follow-up with tool result ===")
    vat_amount = call_info["arguments"]["amount"] * 0.15  # 15% SAR VAT rate

    assistant_tool_call_content = (
        f'<tool_call>\n{json.dumps({"name": call_info["name"], "arguments": call_info["arguments"]})}\n</tool_call>'
    )

    result = call({
        "messages": [
            {"role": "user", "content": "What is the VAT rate for a SAR 1000 invoice? Use the calculate_vat tool."},
            {"role": "assistant", "content": assistant_tool_call_content},
            {"role": "tool", "content": json.dumps({"vat_amount": vat_amount, "currency": "SAR"})},
        ],
        "max_tokens": 256,
        "temperature": 0.1,
        "tools": [CALCULATE_VAT_TOOL],
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert result["content"].strip(), "expected a final natural-language answer"
    print("Manually verify the content above correctly references the VAT amount (150 SAR).")


if __name__ == "__main__":
    test_plain_request()
    test_arabic()
    call_info = test_tool_call_initial()
    test_tool_call_followup(call_info)
    print("\nAll tests completed.")
