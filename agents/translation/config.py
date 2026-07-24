"""
Mizan.ai — "Translate" (Feature B) config.

v1 scope only: paragraph-in, paragraph-out translation between English and
Arabic. Document upload (DOCX/PDF/text file), formatting-preserving
reassembly, and freeform translation instructions ("keep font size X") are
explicitly deferred — the frontend will show those as visible-but-disabled
placeholders, but there is nothing here for them to call yet.
"""

import os

MODAL_TRANSLATE_URL = (
    os.getenv("MIZAN_TRANSLATE_URL")
    or os.getenv("MODAL_TRANSLATE_URL")
)

MODAL_TIMEOUT_SECONDS = int(os.getenv("MIZAN_CHATBOT_TIMEOUT_SECONDS", "600"))

# Hard cap on pasted-paragraph length. The Modal Translator endpoint takes a
# single text blob with no internal chunking (max_new_tokens=1024 on
# generation) -- there is no document-splitting step in this v1, so a
# request longer than a real "paragraph" would either truncate silently on
# the model side or produce degraded output. Reject early instead.
MAX_TEXT_LENGTH = 2000
