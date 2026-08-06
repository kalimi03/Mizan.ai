// Mizan.ai frontend — backend service base URLs.
//
// Each Mizan feature is its own backend service, on its own port in local
// dev (see docker-compose.yml). Only the ports the frontend actually calls
// directly are listed here — doc-extraction (8001) is called service-to-
// service by Calculator/Comparator, never directly from the browser.
//
// To point this frontend at a different environment (staging, prod), edit
// this file — nothing else in the frontend references a URL directly.

const MIZAN_CONFIG = {
  chatbot: "http://localhost:8000",
  calculator: "http://localhost:8002",
  translator: "http://localhost:8003",
  converter: "http://localhost:8004",
  explainer: "http://localhost:8005",
  editor: "http://localhost:8006",
  comparator: "http://localhost:8007",
};
