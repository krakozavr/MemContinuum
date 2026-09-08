---
type: topic
id: TOP-120
title: A command-line flag beats an environment variable beats the config file
area: cli
project: driftwood
current: L1
code_refs:
  - src/cli/config.py
tags: [driftwood, cli, config]
links:
  - link: L1
    date: 2025-02-01
    status: active
    kind: adopted
    ruling:
      text: "When the same setting is given in more than one place, a command-line flag overrides an environment variable, which overrides the config file's own value."
      authority: agent-inference
      source: "design note 2025-02-01"
    rationale:
      text: "This follows the ordering most CLI users already expect from other tools: the most specific, most temporary override (a flag typed for this one invocation) should win over a broader, longer-lived one (an environment variable set for a whole shell session), which in turn should win over the most permanent, least specific setting (the config file on disk)."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-02-01
---
# Config precedence order

This topic is only about ORDER when settings conflict, not about the
syntax the config file itself is written in (TOP-119, TOML).

Not this topic: the config file's format (TOP-119).
