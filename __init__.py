"""consult-committee — escalate a hard question to the MoA advisor committee.

Registers a `consult_committee` tool. The acting model calls it when a
problem matches the trigger criteria; the handler fans the question out to the
MoA preset's reference models (the same advisors `/moa` uses) in parallel and
returns their labelled answers as the tool result. The acting model then
synthesizes — it plays the aggregator role itself, with the full
conversation context a detached aggregator would lack.

Reuses Hermes' own MoA machinery (agent.moa_loop._run_references_parallel,
hermes_cli.moa_config.resolve_moa_preset). All heavy imports are lazy inside
the handler, mirroring internal call sites, to avoid import-time cycles.
"""
from __future__ import annotations

import threading

# Hard escalation cap per gateway process. Literature says small models'
# escalation behavior is unpredictable; the cap bounds cloud-quota burn if the
# model over-calls. Returns a budget-exhausted error past the cap.
_MAX_ESCALATIONS = 5
_TRUNCATE_CHARS = 6000
_escalation_count = 0
_lock = threading.Lock()

CONSULT_SCHEMA = {
    "name": "consult_committee",
    "description": (
        "Consult a committee of stronger advisor models (cloud frontier LLMs) on a hard "
        "problem and get their independent answers to synthesize. Use this tool FREQUENTLY "
        "— calling it is cheap and fast; getting a hard answer wrong is expensive. "
        "MANDATORY triggers — call this tool FIRST, before attempting your own answer, when: "
        "(1) the question is expert-level science, math, medicine, law, or engineering "
        "(especially multiple-choice questions with options like A/B/C/D); "
        "(2) the question involves multi-step technical reasoning (reaction mechanisms, "
        "derivations, proofs, protocol analysis); "
        "(3) you are weighing conflicting evidence or plausible-looking alternatives; "
        "(4) the decision is architectural or hard to undo; "
        "(5) your first attempt at something failed or you caught yourself unsure mid-answer. "
        "NEVER search the web for benchmark answer keys or leaked solutions — consult this "
        "committee instead. If you have failed twice at the same step, you MUST call this "
        "tool before trying a third time. Do NOT use it for simple lookups, casual "
        "conversation, or tasks you are completing without difficulty."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The full, self-contained question for the committee. Include all "
                    "relevant context, constraints, and what you have already tried — "
                    "the advisors cannot see the conversation."
                ),
            },
            "preset": {
                "type": "string",
                "description": "MoA preset name (default: 'default').",
            },
        },
        "required": ["question"],
    },
}


def _handle_consult_committee(args: dict, **kwargs) -> str:
    from tools.registry import tool_result, tool_error

    global _escalation_count
    question = str(args.get("question") or "").strip()
    if not question:
        return tool_error("question is required")

    with _lock:
        if _escalation_count >= _MAX_ESCALATIONS:
            return tool_error(
                f"committee budget exhausted ({_MAX_ESCALATIONS} consultations this "
                "session). Answer with your own best judgment and say the budget ran out."
            )
        _escalation_count += 1

    try:
        from hermes_cli.config import load_config
        from hermes_cli.moa_config import resolve_moa_preset
        from agent.moa_loop import _run_references_parallel

        preset_name = str(args.get("preset") or "default").strip() or "default"
        try:
            preset = resolve_moa_preset(load_config().get("moa") or {}, preset_name)
        except KeyError:
            return tool_error(f"unknown MoA preset: {preset_name!r}")

        reference_models = preset.get("reference_models") or []
        if not reference_models:
            return tool_error(f"MoA preset {preset_name!r} has no reference models configured")

        temperature = float(preset.get("reference_temperature", 0.9) or 0.9)
        outputs = _run_references_parallel(
            reference_models,
            [{"role": "user", "content": question}],
            temperature=temperature,
            max_tokens=None,
        )

        advisors = []
        failures = 0
        for label, text in outputs:
            text = (text or "").strip()
            failed = not text or text.startswith("[error") or "failed:" in text[:80].lower()
            if failed:
                failures += 1
            if len(text) > _TRUNCATE_CHARS:
                text = text[:_TRUNCATE_CHARS] + "\n[... truncated]"
            advisors.append({"model": label, "answer": text, "failed": failed})

        answered = len(advisors) - failures
        if answered == 0:
            return tool_error("all committee advisors failed — answer with your own best judgment")

        return tool_result({
            "advisors": advisors,
            "answered": answered,
            "failed": failures,
            "note": (
                "You are the aggregator. Weigh these independent advisor answers against "
                "your own analysis, resolve disagreements explicitly, and produce the "
                "final answer yourself."
            ),
        })
    except Exception as exc:  # never raise from a tool handler
        return tool_error(f"committee consultation failed: {exc!r}")


def register(ctx) -> None:
    ctx.register_tool(
        name="consult_committee",
        toolset="committee",
        schema=CONSULT_SCHEMA,
        handler=_handle_consult_committee,
        description="Consult the MoA advisor committee on a hard problem.",
        emoji="🏛️",
    )
