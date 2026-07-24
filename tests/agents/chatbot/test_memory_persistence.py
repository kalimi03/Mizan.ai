import os

from agents.chatbot.langgraph_chatbot import ChatbotAgent


def test_manual_memory_helpers_can_store_and_read_user_context():
    agent = ChatbotAgent(model_url=None)
    user_id = "manual-user"

    agent.store_user_profile(
        user_id=user_id,
        user_name="Mohammed",
        email="mohammed@example.com",
        preferred_language="ar",
        preferred_tone="friendly",
        last_activity_summary="Reviewed VAT compliance notes",
    )
    agent.store_user_activity(user_id=user_id, activity_summary="Worked on VAT compliance")

    profile = agent.get_user_profile(user_id)
    recent = agent.get_user_recent_activity(user_id, limit=3)

    assert profile is not None
    assert profile["user_name"] == "Mohammed"
    assert any(item["activity_summary"] == "Worked on VAT compliance" for item in recent)


def test_manual_memory_helpers_can_store_customer_context():
    agent = ChatbotAgent(model_url=None)
    user_id = "manual-customer-user"

    agent.store_customer_memory(
        user_id=user_id,
        customer_name="Acme",
        customer_context="Customer reported invoice mismatch last week",
        issue_summary="Invoice mismatch",
    )

    memories = agent.get_customer_memories(user_id=user_id, customer_name="Acme")
    assert len(memories) >= 1
    assert any("invoice mismatch" in item["customer_context"].lower() for item in memories)
