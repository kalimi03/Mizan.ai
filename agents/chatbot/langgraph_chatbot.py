from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Annotated, List, Optional, TypedDict

import requests
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from agents.chatbot.memory import (
    append_user_activity,
    get_customer_memories,
    get_user_recent_activity,
    initialize_memory_schemas,
    store_customer_memory,
)

load_dotenv()

logger = logging.getLogger(__name__)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "mizan_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres123")


def _postgres_uri() -> str:
    return f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class ChatState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    language: str
    intent: str
    profile: dict
    reply: str


# Two deliberately different numbers — do not collapse them into one.
# STATE_MESSAGE_CAP bounds how much raw transcript survives in the Postgres
# checkpoint; the wire window (the "[-8:]" slice in _build_wire_messages)
# bounds how much of that the model actually sees per request. Raising the
# wire window to match this cap is a deliberate future change gated on
# funding — it roughly quadruples token cost per chatbot call.
STATE_MESSAGE_CAP = 30


# Heuristic name extraction — good enough for "my name is X" / "call me X"
# style statements. Not a full NLU pipeline; a model-based extractor can
# replace this later without touching how the result is stored/used.
_NAME_PATTERNS = [
    re.compile(r"\bmy name is\s+([A-Za-z][\w' -]{0,40}?)(?:[.,!?]|\s+and\b|$)", re.IGNORECASE),
    re.compile(r"\bcall me\s+([A-Za-z][\w' -]{0,40}?)(?:[.,!?]|\s+and\b|$)", re.IGNORECASE),
    re.compile(r"اسمي\s+([\u0600-\u06FF][\u0600-\u06FF\s]{0,40}?)(?:[.,!؟]|$)"),
]


_TRAILING_STOPWORDS = {"please", "thanks", "thank", "too", "also", "here", "now", "today"}


def extract_name(text: str) -> Optional[str]:
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        words = match.group(1).strip().strip("'\"").split()
        while words and words[-1].lower().strip(".,!?") in _TRAILING_STOPWORDS:
            words.pop()
        if words and len(words) <= 3:
            return " ".join(words)
    return None


class ChatbotAgent:
    def __init__(self, model_url: Optional[str] = None, timeout: int = 600) -> None:
        self.model_url = (
            model_url
            or os.getenv("QWEN_LITE_URL")
            or os.getenv("MIZAN_QWEN_LITE_URL")
            or os.getenv("MODAL_QWEN_LITE_URL")
        )
        self.timeout = int(os.getenv("MIZAN_CHATBOT_TIMEOUT_SECONDS", str(timeout)))
        self._warming_up = False
        self._checkpointer_cm = None
        self._store_cm = None
        self.checkpointer, self.store = self._init_persistence()
        self.graph = self._build_graph()
        logger.info("Qwen Lite endpoint configured: %s", bool(self.model_url))

    def _init_persistence(self):
        try:
            initialize_memory_schemas()
        except Exception as exc:  # pragma: no cover - defensive path
            logger.warning("Legacy memory schema initialization failed: %s", exc)

        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from langgraph.store.postgres import PostgresStore

            conn_string = _postgres_uri()

            self._checkpointer_cm = PostgresSaver.from_conn_string(conn_string)
            checkpointer = self._checkpointer_cm.__enter__()
            checkpointer.setup()

            self._store_cm = PostgresStore.from_conn_string(conn_string)
            store = self._store_cm.__enter__()
            store.setup()

            return checkpointer, store
        except Exception as exc:  # pragma: no cover - defensive path
            logger.warning("Postgres-backed memory unavailable, falling back to in-process only: %s", exc)
            from langgraph.checkpoint.memory import MemorySaver
            from langgraph.store.memory import InMemoryStore

            self._checkpointer_cm = None
            self._store_cm = None
            return MemorySaver(), InMemoryStore()

    def close(self) -> None:
        for cm in (self._checkpointer_cm, self._store_cm):
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                except Exception:  # pragma: no cover - defensive path
                    pass

    def _build_graph(self):
        workflow = StateGraph(ChatState)

        workflow.add_node("route_intent", self._route_intent)
        workflow.add_node("sync_memory", self._sync_memory)
        workflow.add_node("generate_reply", self._generate_reply)
        workflow.add_node("trim_memory", self._trim_memory)
        workflow.add_edge("route_intent", "sync_memory")
        workflow.add_edge("sync_memory", "generate_reply")
        workflow.add_edge("generate_reply", "trim_memory")
        workflow.add_edge("trim_memory", END)
        workflow.set_entry_point("route_intent")
        return workflow.compile(checkpointer=self.checkpointer, store=self.store)

    @staticmethod
    def _last_human_text(state: ChatState) -> str:
        for message in reversed(state.get("messages", [])):
            if isinstance(message, HumanMessage):
                return str(message.content)
        return ""

    def _route_intent(self, state: ChatState) -> dict:
        return {"intent": classify_intent(self._last_human_text(state))}

    def _sync_memory(self, state: ChatState, config: RunnableConfig) -> dict:
        # user_id (permanent, account-scoped) keys the profile store — kept
        # strictly separate from thread_id (temporary, per-conversation),
        # which LangGraph uses only for checkpointing. An unauthenticated
        # caller has no user_id, so profile memory is simply skipped.
        user_id = (config or {}).get("configurable", {}).get("user_id")
        if not user_id:
            return {"profile": {}}

        name = extract_name(self._last_human_text(state))
        if name:
            self.store_user_profile(user_id, user_name=name)

        return {"profile": self.get_user_profile(user_id) or {}}

    def _trim_memory(self, state: ChatState) -> dict:
        """Caps how much raw transcript survives in the Postgres checkpoint.
        add_messages only appends/merges by ID — returning a shorter list
        from a node does not delete anything, so trimming requires emitting
        RemoveMessage entries for the oldest messages once the cap is
        exceeded.
        """
        messages = state.get("messages", [])
        overflow = len(messages) - STATE_MESSAGE_CAP
        if overflow <= 0:
            return {}
        return {"messages": [RemoveMessage(id=message.id) for message in messages[:overflow]]}

    def _generate_reply(self, state: ChatState) -> dict:
        message = self._last_human_text(state)
        language = state.get("language") or detect_language(message)
        intent = state.get("intent") or classify_intent(message)
        profile = state.get("profile") or {}

        if self.model_url:
            try:
                self._warming_up = True
                payload = {
                    "messages": self._build_wire_messages(state, language, profile),
                    "max_tokens": 512,
                    "temperature": 0.2,
                }
                response = requests.post(self.model_url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                body = response.json()
                content = body.get("content") or body.get("reply") or body.get("message")
                if content:
                    return {"messages": [AIMessage(content=str(content))], "reply": str(content)}
            except requests.Timeout:
                logger.warning("Model request timed out after %ss; the service may still be warming up.", self.timeout)
            except requests.RequestException as exc:  # pragma: no cover - defensive path
                logger.warning("Model call failed, falling back: %s", exc)
            finally:
                self._warming_up = False

        reply = fallback_reply(message=message, intent=intent, language=language)
        return {"messages": [AIMessage(content=reply)], "reply": reply}

    def _build_wire_messages(self, state: ChatState, language: str, profile: dict) -> List[dict]:
        """Real prior turns as actual user/assistant messages, not flattened
        into the system prompt — lets the model's chat template do the work
        it's tuned for instead of parsing a transcript out of prose.
        """
        wire: List[dict] = [{"role": "system", "content": build_system_prompt(language=language, profile=profile)}]
        for message in state.get("messages", [])[-8:]:
            if isinstance(message, HumanMessage):
                wire.append({"role": "user", "content": str(message.content)})
            elif isinstance(message, AIMessage):
                wire.append({"role": "assistant", "content": str(message.content)})
        return wire

    def is_warming_up(self) -> bool:
        return self._warming_up

    def generate_reply(
        self,
        message: str,
        language: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        if not message or not message.strip():
            return "How can I help you with Mizan.ai today?"

        thread_id = session_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        input_state = {
            "messages": [HumanMessage(content=message)],
            "language": language or detect_language(message),
        }
        result = self.graph.invoke(input_state, config=config)
        return result.get("reply") or "I can help with Mizan.ai questions and feature guidance."

    def get_session_history(self, session_id: Optional[str]) -> List[dict]:
        if not session_id:
            return []
        snapshot = self.graph.get_state({"configurable": {"thread_id": session_id}})
        history: List[dict] = []
        for message in (snapshot.values.get("messages", []) if snapshot and snapshot.values else []):
            if isinstance(message, HumanMessage):
                history.append({"role": "user", "content": str(message.content)})
            elif isinstance(message, AIMessage):
                history.append({"role": "assistant", "content": str(message.content)})
        return history

    def store_user_profile(self, user_id: str, user_name: Optional[str] = None, email: Optional[str] = None,
                           preferred_language: Optional[str] = None, preferred_tone: Optional[str] = None,
                           last_activity_summary: Optional[str] = None) -> None:
        namespace = ("users", user_id)
        existing = self.store.get(namespace, "profile")
        profile = dict(existing.value) if existing else {}
        updates = {
            "user_name": user_name,
            "email": email,
            "preferred_language": preferred_language,
            "preferred_tone": preferred_tone,
            "last_activity_summary": last_activity_summary,
        }
        profile.update({key: value for key, value in updates.items() if value is not None})
        self.store.put(namespace, "profile", profile)

    def get_user_profile(self, user_id: str) -> Optional[dict]:
        item = self.store.get(("users", user_id), "profile")
        return dict(item.value) if item else None

    def store_user_activity(self, user_id: str, activity_summary: str) -> None:
        append_user_activity(user_id=user_id, activity_summary=activity_summary)

    def get_user_recent_activity(self, user_id: str, limit: int = 5):
        return get_user_recent_activity(user_id, limit=limit)

    def store_customer_memory(self, user_id: str, customer_name: str, customer_context: str, issue_summary: Optional[str] = None) -> None:
        store_customer_memory(user_id=user_id, customer_name=customer_name, customer_context=customer_context, issue_summary=issue_summary)

    def get_customer_memories(self, user_id: str, customer_name: Optional[str] = None, limit: int = 10):
        return get_customer_memories(user_id=user_id, customer_name=customer_name, limit=limit)


def build_system_prompt(language: str = "en", profile: Optional[dict] = None) -> str:
    profile = profile or {}
    profile_line = ""
    if profile.get("user_name"):
        profile_line = f"User profile: name={profile.get('user_name')}"
        if profile.get("preferred_tone"):
            profile_line += f", preferred_tone={profile.get('preferred_tone')}"

    return f"""You are the Mizan.ai homepage assistant.
You help users understand Mizan.ai and guide them to the right feature.

Rules:
- Answer in the same language as the user.
- Use the conversation history to answer questions about things the user already told you (e.g. their name). Do not claim you don't know something that appears earlier in the conversation.
- If the user profile below has a name, greet them by name and mention that you remember them.
- If the user asks about Mizan.ai, explain what it is and its main features.
- If the user asks for a ZATCA/VAT calculation, suggest the ZATCA compliance calculator workflow in the UI.
- If the user asks about translation, comparison, validation, or file conversion, suggest the relevant feature.
- For general off-topic questions, answer briefly and then steer the conversation back to Mizan.ai features.
- You only see a limited recent window of this conversation, not the full history. If the user references something that does not appear anywhere in the messages you can see, do not guess or fabricate a memory of it. Instead, reply with this line (in the user's language) — English: "This is a free trial, so I can only remember our recent conversation, not older messages. If you'd like a version with longer memory and more advanced features, reach out to the Mizan.ai team about our Pro plan." Arabic: "هذه نسخة تجريبية مجانية، لذلك يمكنني تذكر محادثتنا الأخيرة فقط وليس الرسائل الأقدم. إذا كنت ترغب بذاكرة أطول وميزات متقدمة، تواصل مع فريق Mizan.ai بخصوص خطة Pro." Only use this when the user is clearly asking about something outside your visible context — not on every reply.

Language: {language}
{profile_line}
"""


def detect_language(text: str) -> str:
    return "ar" if re.search(r"[\u0600-\u06FF]", text) else "en"


def classify_intent(message: str) -> str:
    text = message.lower()
    if re.search(r"\b(zatca|vat|calculate|calculator|compliance|tax)\b", text):
        return "calculator"
    if re.search(r"\b(mizan|feature|features|what is|how to use|tell me about|help me with)\b", text):
        return "product_info"
    return "general"


def fallback_reply(message: str, intent: str, language: str) -> str:
    if intent == "calculator":
        if language == "ar":
            return "يمكنك استخدام ميزة حاسبة الامتثال لـ ZATCA من خلال واجهة التطبيق. افتح 'ZATCA compliance calculator' للبدء."
        return "You can use the ZATCA compliance calculator from the app UI. Open the 'ZATCA compliance calculator' option to begin."

    if intent == "product_info":
        if language == "ar":
            return "Mizan.ai هي منصة ذكاء اصطناعي أولاً باللغة العربية لمساعدة الشركات الصغيرة والمتوسطة في السعودية على فهم متطلبات ZATCA والضريبة المضافة. من ميزاتها: الأسئلة عن ZATCA/الضريبة، دردشة مع المستندات، الترجمة، مقارنة المستندات، حاسبة الامتثال، والتحقق من الوثائق."
        return "Mizan.ai is an Arabic-first AI platform that helps Saudi SMEs with ZATCA and VAT requirements. Its features include ZATCA/VAT Q&A, document chat, translation, document comparison, compliance calculation, documentation validation, and file conversion."

    if language == "ar":
        return "أنا هنا لمساعدتك في فهم Mizan.ai واستخدام ميزاته. إذا أردت، يمكنك سؤالي عن ما يقدمه Mizan.ai أو عن أي ميزة مثل ZATCA أو الترجمة أو المقارنة."
    return "I can help you understand Mizan.ai and guide you to the right feature. Ask me about Mizan.ai, ZATCA support, translation, comparison, or document chat."
