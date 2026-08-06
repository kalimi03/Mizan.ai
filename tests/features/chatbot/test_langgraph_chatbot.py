import os

from features.chatbot.langgraph_chatbot import ChatbotAgent, classify_intent, detect_language


def test_classify_intent_for_calculator_queries():
    assert classify_intent("calculate zatca compliance") == "calculator"
    assert classify_intent("what is mizan ai") == "product_info"
    assert classify_intent("how are you") == "general"


def test_detect_language_for_arabic_text():
    assert detect_language("مرحبا بك") == "ar"


def test_generate_reply_uses_fallback_for_local_testing():
    agent = ChatbotAgent(model_url=None)
    reply = agent.generate_reply("What is Mizan.ai?")
    assert "Mizan.ai" in reply


def test_chatbot_uses_alternate_qwen_lite_env_var(monkeypatch):
    monkeypatch.setenv("QWEN_LITE_URL", "https://example.test/qwen-lite")
    agent = ChatbotAgent(model_url=None)
    assert agent.model_url == "https://example.test/qwen-lite"


def test_generate_reply_persists_short_term_memory_for_session():
    agent = ChatbotAgent(model_url=None)

    agent.generate_reply("I am working on VAT compliance in Saudi Arabia.", session_id="session-memory")
    history = agent.get_session_history("session-memory")

    assert len(history) >= 2
    assert any(item.get("content") == "I am working on VAT compliance in Saudi Arabia." for item in history)
