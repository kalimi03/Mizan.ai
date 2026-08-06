"""Shared language detection - used by Translator, RAG-online, Calculator, and
Chatbot. Moved out of features/chatbot/langgraph_chatbot.py so those services
don't need a runtime/import dependency on the Chatbot package.
"""

import re


def detect_language(text: str) -> str:
    return "ar" if re.search(r"[؀-ۿ]", text) else "en"
