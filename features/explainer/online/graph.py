"""
Mizan.ai — "Know VAT & ZATCA" online inference pipeline.

LangGraph StateGraph with real conditional branching (jurisdiction check,
retrieval-found-or-not) — following the style taught in DeepLearning.AI's
"AI Agents in LangGraph" course (agentic search + conditional edges), and
reusing the same checkpointer/store persistence pattern already deployed in
features/chatbot/langgraph_chatbot.py. Read-only against Qdrant + SQLite —
this pipeline never writes to the knowledge base.

detect_language() and the DB connection constants come from features/common/
(shared across services), not from features/chatbot/ — this pipeline has no
import dependency on the Chatbot package, since it runs as its own
service (RAG online).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from features.common.db import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from features.common.language import detect_language
from features.common.modal_client import ModalEndpointError, call_modal_json
from features.common.text_cleanup import strip_latex_math

from ..config import MAX_HISTORY_TURNS, MODAL_QWEN_LITE_URL, MODAL_TIMEOUT_SECONDS
from ..fallback import cross_language_wrapper_message, detect_non_ksa_country, nothing_found_message, wrong_jurisdiction_message
from ..prompts import build_know_vat_zatca_prompt
from .retrieval import embed_query, resolve_and_cap, search_with_fallback

logger = logging.getLogger(__name__)


def _postgres_uri() -> str:
    return f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class KnowVatZatcaState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    query: str
    language: str
    non_ksa_country: Optional[str]
    query_vector: Optional[List[float]]
    retrieved_chunks: List[Dict[str, Any]]
    used_cross_language_fallback: bool
    resolved_context: List[Dict[str, Any]]
    reply: str
    citations: List[Dict[str, Any]]
    fallback_kind: Optional[str]


class KnowVatZatcaAgent:
    def __init__(self) -> None:
        self._checkpointer_cm = None
        self.checkpointer = self._init_checkpointer()
        self.graph = self._build_graph()

    def _init_checkpointer(self):
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            self._checkpointer_cm = PostgresSaver.from_conn_string(_postgres_uri())
            checkpointer = self._checkpointer_cm.__enter__()
            checkpointer.setup()
            return checkpointer
        except Exception as exc:  # pragma: no cover - defensive path
            logger.warning("Postgres checkpointer unavailable for know_vat_n_zatca, falling back to in-process: %s", exc)
            from langgraph.checkpoint.memory import MemorySaver

            self._checkpointer_cm = None
            return MemorySaver()

    def close(self) -> None:
        if self._checkpointer_cm is not None:
            try:
                self._checkpointer_cm.__exit__(None, None, None)
            except Exception:  # pragma: no cover - defensive path
                pass

    def _build_graph(self):
        workflow = StateGraph(KnowVatZatcaState)

        workflow.add_node("detect_language", self._detect_language_node)
        workflow.add_node("wrong_jurisdiction", self._wrong_jurisdiction_node)
        workflow.add_node("embed_query", self._embed_query_node)
        workflow.add_node("search", self._search_node)
        workflow.add_node("nothing_found", self._nothing_found_node)
        workflow.add_node("resolve_context", self._resolve_context_node)
        workflow.add_node("generate_answer", self._generate_answer_node)
        workflow.add_node("trim_memory", self._trim_memory_node)

        workflow.set_entry_point("detect_language")
        workflow.add_conditional_edges(
            "detect_language",
            lambda state: "wrong_jurisdiction" if state.get("non_ksa_country") else "embed_query",
        )
        workflow.add_edge("wrong_jurisdiction", "trim_memory")
        workflow.add_edge("embed_query", "search")
        workflow.add_conditional_edges(
            "search",
            lambda state: "nothing_found" if not state.get("retrieved_chunks") else "resolve_context",
        )
        workflow.add_edge("nothing_found", "trim_memory")
        workflow.add_edge("resolve_context", "generate_answer")
        workflow.add_edge("generate_answer", "trim_memory")
        workflow.add_edge("trim_memory", END)

        return workflow.compile(checkpointer=self.checkpointer)

    # --- nodes ---------------------------------------------------------

    @staticmethod
    def _last_human_text(state: KnowVatZatcaState) -> str:
        for message in reversed(state.get("messages", [])):
            if isinstance(message, HumanMessage):
                return str(message.content)
        return ""

    def _detect_language_node(self, state: KnowVatZatcaState) -> dict:
        query = self._last_human_text(state)
        return {
            "query": query,
            "language": detect_language(query),
            "non_ksa_country": detect_non_ksa_country(query),
        }

    def _wrong_jurisdiction_node(self, state: KnowVatZatcaState) -> dict:
        reply = wrong_jurisdiction_message(state["non_ksa_country"], state["language"])
        return {"reply": reply, "citations": [], "fallback_kind": "wrong_jurisdiction",
                "messages": [AIMessage(content=reply)]}

    def _embed_query_node(self, state: KnowVatZatcaState) -> dict:
        return {"query_vector": embed_query(state["query"])}

    def _search_node(self, state: KnowVatZatcaState) -> dict:
        chunks, used_cross_language = search_with_fallback(state["query_vector"], state["language"])
        return {"retrieved_chunks": chunks, "used_cross_language_fallback": used_cross_language}

    def _nothing_found_node(self, state: KnowVatZatcaState) -> dict:
        reply = nothing_found_message(state["language"])
        return {"reply": reply, "citations": [], "fallback_kind": "nothing_found",
                "messages": [AIMessage(content=reply)]}

    def _resolve_context_node(self, state: KnowVatZatcaState) -> dict:
        return {"resolved_context": resolve_and_cap(state["retrieved_chunks"])}

    def _generate_answer_node(self, state: KnowVatZatcaState) -> dict:
        context = state["resolved_context"]
        system_prompt = build_know_vat_zatca_prompt(state["language"], context)

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state["query"]},
            ],
            "max_tokens": 512,
            "temperature": 0.2,
        }

        try:
            body = call_modal_json(MODAL_QWEN_LITE_URL, payload, timeout=MODAL_TIMEOUT_SECONDS)
            content = strip_latex_math(body.get("content") or body.get("reply") or body.get("message") or "")
        except ModalEndpointError as exc:
            logger.warning("know_vat_n_zatca answer generation failed: %s", exc)
            content = nothing_found_message(state["language"])

        if state.get("used_cross_language_fallback") and content:
            content_language = "en" if state["language"] == "ar" else "ar"
            wrapper = cross_language_wrapper_message(state["language"], content_language)
            content = f"{wrapper}\n\n{content}"

        citations = [
            {
                "document_type": chunk.get("document_type"),
                "jurisdiction": chunk.get("jurisdiction"),
                "version_label": chunk.get("version_label"),
                "source_site": chunk.get("source_site"),
                "source_url": chunk.get("source_url"),
            }
            for chunk in context
        ]

        return {"reply": content, "citations": citations, "fallback_kind": None,
                "messages": [AIMessage(content=content)]}

    def _trim_memory_node(self, state: KnowVatZatcaState) -> dict:
        messages = state.get("messages", [])
        max_messages = MAX_HISTORY_TURNS * 2  # (query, response) pairs
        overflow = len(messages) - max_messages
        if overflow <= 0:
            return {}
        return {"messages": [RemoveMessage(id=m.id) for m in messages[:overflow]]}

    # --- public entrypoint ----------------------------------------------

    def ask(self, message: str, session_id: Optional[str] = None, user_id: Optional[str] = None) -> dict:
        thread_id = session_id or str(uuid.uuid4())
        config: RunnableConfig = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        input_state: KnowVatZatcaState = {"messages": [HumanMessage(content=message)]}
        result = self.graph.invoke(input_state, config=config)
        return {
            "reply": result.get("reply") or nothing_found_message(detect_language(message)),
            "citations": result.get("citations") or [],
            "session_id": thread_id,
            "fallback_kind": result.get("fallback_kind"),
        }
