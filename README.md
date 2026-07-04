# consult-committee — a Hermes plugin

**Give your local model a "phone a friend" button.** This plugin registers a
`consult_committee` tool that lets the acting model escalate a hard question to
your configured [Mixture-of-Agents](https://hermes-agent.nousresearch.com/docs/user-guide/features/mixture-of-agents)
advisor committee — mid-run, on its own judgment — instead of you having to
type `/moa` yourself.

Hermes upstream deliberately keeps `/moa` out of the model's tool list (the
slash command marks one *user* turn as MoA-enabled). That's the right default —
but it means the model can never ask for help *during* a long agent run.
This plugin fills that gap ([#38952](https://github.com/NousResearch/hermes-agent/issues/38952)
discusses the need) without touching Hermes core: it's a plain user plugin.

## How it works

```
you ──► local model (fast, cheap, default)
              │
              ├── easy task ──► answers directly
              │
              └── hard task ──► calls consult_committee(question)
                                   │  fans out to your MoA preset's
                                   │  reference models, in parallel
                                   ▼
                          advisors' answers returned as tool result
                                   │
              local model synthesizes ◄┘   (it IS the aggregator —
                                            with full conversation context)
```

Design choices, grounded in the MoA paper and 2026 escalation literature:

- **The model synthesizes, not a detached aggregator.** The tool returns the
  advisors' labelled answers; the calling model weighs them against its own
  analysis. Mirrors upstream MoA's "aggregator = acting model" design, saves an
  LLM call, and the caller has conversation context a detached aggregator lacks.
- **One required parameter** (`question`) — maximizes tool-call reliability on
  small local models.
- **Concrete triggers in the description, not "if unsure"** — verbalized
  uncertainty is the miscalibrated signal in small models; observable criteria
  (expert-level question, two failed attempts) work better.
- **Hard budget cap** — max 5 consultations per gateway process, then the tool
  returns a budget-exhausted error. Bounds cloud-quota burn if the model
  over-calls.
- **Graceful partial results** — a failed advisor (auth down, quota out) is
  reported as failed; the rest still answer.

## Install

```bash
git clone https://github.com/070freebird070-ctrl/hermes-consult-committee \
    ~/.hermes/plugins/consult-committee
hermes plugins enable consult-committee
```

Then expose the toolset — add `committee` to your enabled toolsets in
`~/.hermes/config.yaml`:

```yaml
toolsets:
  - hermes-cli
  - committee
```

Restart your gateway (`hermes gateway restart`). Verify with
`HERMES_PLUGINS_DEBUG=1 hermes plugins list`.

Requires a working MoA preset (`moa.presets.default.reference_models`) — the
plugin fans out to whatever advisors you configured. No keys or model names
live in the plugin itself.

## Known limitation: small models under-call help (measured)

The 2026 escalation literature is unanimous: small models are overconfident
(75–92% of tested conditions) and **won't reach for a help tool on their own**
as often as they should. We reproduced this live: a 35B-class local model called
the tool reliably when nudged ("use the committee"), answered hard questions
correctly through it — but went 0-for-4 on self-escalation. It could even
*describe* the tool and its triggers perfectly when asked. Knowing ≠ doing.

Two findings from testing worth passing on:

- **Famous puzzles are useless as escalation tests.** Well-known trap questions
  (Tuesday-boy probability, Alice's-sisters, etc.) are in the training data —
  the model solves them solo, correctly, and never feels the need to escalate.
  Test with genuinely expert or novel questions.
- **A system-prompt rule fixes it — verified.** After adding the rule below to
  the model's persona file, the same model self-escalated **unprompted** on an
  expert math integral (fresh session, zero hints) and again on a hard C++20
  lock-free concurrency question in normal chat, weighing the advisors'
  answers against its own analysis in the final response.

Levers, strongest first:

1. **System-prompt rule** (recommended, verified above) — add to your
   persona/system prompt:

   > Phone a friend BEFORE you solo hard stuff. For any expert-level question
   > (science, math, engineering, architecture), anything with real stakes, or
   > any answer you feel even a little unsure about — call the
   > consult_committee tool FIRST, then blend its advice into your answer. Do
   > not answer from your own head and hope.

   Note: most agents bake the system prompt in at **session start** — running
   sessions keep the old prompt, so test in a fresh session.

2. **Tool description** — already ships with concrete triggers + a mandatory
   retry tripwire ("failed twice → MUST consult").
3. **User nudge** — "check with the committee" in your message always works.

Measure your own model's escalation rate before trusting it: escalation
behavior is model-specific and unpredictable (see
[Act or Escalate, arXiv:2604.08588](https://arxiv.org/abs/2604.08588)).

## Prior art / credits

- [Amp's oracle tool](https://ampcode.com/news/oracle) — consult-a-stronger-model as a tool
- [zen-mcp-server](https://github.com/BeehiveInnovations/zen-mcp-server) — multi-model consensus tools
- [Together AI MoA paper](https://arxiv.org/abs/2406.04692) — aggregator-need-not-be-strongest
- [42-evey/hermes-plugins](https://github.com/42-evey/hermes-plugins) — plugin structure reference

## License

MIT
