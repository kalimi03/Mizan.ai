from features.chatbot.generic_chatbot import ChatbotAgent, classify_intent, build_chat_prompt


def test_classify_intent_for_calculator_queries():
    assert classify_intent("calculate zatca compliance") == "calculator"
    assert classify_intent("what is mizan ai") == "product_info"
    assert classify_intent("how are you") == "general"


def test_build_prompt_includes_mizan_scope_and_language_behavior():
    prompt = build_chat_prompt(
        message="Explain Mizan.ai",
        history=[],
        language="en",
        session_id="session-1",
    )

    assert "Mizan.ai" in prompt
    assert "ZATCA" in prompt
    assert "same language" in prompt.lower()
