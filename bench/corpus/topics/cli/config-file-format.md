---
type: topic
id: TOP-119
title: The config file format is TOML
area: cli
project: driftwood
current: L2
code_refs:
  - src/cli/config.py
tags: [driftwood, cli, config]
links:
  - link: L2
    date: 2025-05-05
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "The CLI's configuration file format is TOML, replacing the YAML format used at launch."
      authority: agent-inference
      source: "bug triage session 2025-05-05"
    rationale:
      text: "YAML's implicit type coercion silently turned a bare version-looking value into a float in a user's config, changing its meaning with no error and no warning. TOML has an explicit, small type grammar (strings are always quoted) with no equivalent implicit-coercion surprise, at the cost of being slightly more verbose to hand-write."
      authority: agent-inference
    evidence:
      - "support ticket: a config value intended as the literal text '3.10' was parsed by the YAML loader as the number 3.1, silently changing the setting it controlled"
    recorded_by: agent
    recorded_at: 2025-05-05
  - link: L1
    date: 2025-02-01
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "The CLI's configuration file format is YAML."
      authority: agent-inference
      source: "design note 2025-02-01"
    rationale:
      text: "YAML was already familiar to most users from other developer tools and needed no format-specific documentation to get started with."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-02-01
---
# Config file format

This topic is about the SYNTAX the config file is written in. Which value
wins when the file, an environment variable and a command-line flag
disagree about the same setting is a separate question (TOP-120).

Not this topic: precedence between the file, environment variables and
flags (TOP-120).
