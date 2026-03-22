# TinyCTX — TODO

## Ops / management
- [ ] **Web UI** — not a chat interface; an admin/stats panel.
      Likely a single-page aiohttp route serving a small HTML+JS dashboard.
      Should surface: uptime, per-session queue depth + turn count, memory index
      stats (file count, chunk count, last sync), active bridges, active modules,
      token budget gauges per session. Could reuse the existing `/v1/health` data
      plus a new `/v1/stats` endpoint.

- [ ] **Webhooks** — inbound webhook bridge (`bridges/webhook/`).
      Expose a `POST /webhook/{id}` endpoint; route payload into a configured
      session as an `InboundMessage`. Useful for external triggers (GitHub, n8n,
      Zapier, etc.). Auth via a per-hook secret in config. Optionally support
      outbound webhooks on `AgentTextFinal` events (call a URL with the reply).
