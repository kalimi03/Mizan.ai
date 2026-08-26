// Mizan.ai frontend — production backend service base URLs.
//
// Baked into the frontend image in place of config.js (see Dockerfile).
// All services are reached through the same Ingress as the frontend
// itself (mizanai.org), each behind its own already-unique /api/... path
// prefix (see k8s-gcp/ingress.yaml) -- so every base URL here is just the
// same origin.

const MIZAN_CONFIG = {
  chatbot: "",
  calculator: "",
  translator: "",
  converter: "",
  explainer: "",
  editor: "",
  comparator: "",
};
