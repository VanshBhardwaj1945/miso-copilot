# backend/poller (planned)

Background poller, per the architecture in the root README:

- APScheduler job inside the FastAPI process, every 15 min.
- Hits each MISO public endpoint (`https://public-api.misoenergy.org`, no
  auth; stay far under ~1 request/endpoint/minute).
- Converts JSON -> a plain-English snapshot paragraph with a timestamp
  ("As of 6:55 PM EST, total generation is 114,136 MW; ...") - API values are
  strings, parse before math.
- UPSERTs the snapshot into Chroma via `../rag/` with a fixed doc_id per
  endpoint. Never append. Degrade to the last stored snapshot if an endpoint
  disappears (MISO Data Exchange migration).
