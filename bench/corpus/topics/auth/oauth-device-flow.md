---
type: topic
id: TOP-114
title: CLI authentication uses OAuth's device-code flow
area: auth
project: driftwood
current: L1
code_refs:
  - src/auth/oauth.py
tags: [driftwood, auth, oauth]
links:
  - link: L1
    date: 2025-02-20
    status: active
    kind: adopted
    ruling:
      text: "The CLI authenticates via OAuth's device-code flow: it displays a short code and a URL, the user confirms in any browser (including on a different device), and the CLI polls until the device is authorized."
      authority: agent-inference
      source: "design note 2025-02-20"
    rationale:
      text: "The CLI often runs on a headless machine (a server, a container, over SSH) with no local browser and no way to receive a redirect callback, which rules out the ordinary authorization-code-with-redirect flow. Device-code authentication needs no embedded client secret and no local listener port, and it lets the user complete the browser step from their phone if the CLI's own machine has no browser at all."
      authority: agent-inference
    alternatives:
      - {option: "embed a long-lived API key the user pastes in", rejected_because: "a pasted long-lived key is harder to revoke per-device and easy to leak into shell history or a dotfile committed by mistake", authority: agent-inference}
    recorded_by: agent
    recorded_at: 2025-02-20
---
# CLI authentication

This is specifically how the CLI first proves who the user is. What
happens to the resulting tokens afterward -- when they get refreshed, where
they're kept at rest -- is covered by TOP-115 and TOP-116.

Not this topic: refreshing an already-issued token (TOP-115) or where a
token is stored once issued (TOP-116).
