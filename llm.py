"""Optional LLM fallback: answers only from config/lave.yaml. Off unless LLM_FALLBACK=1."""
import os

import yaml

SYSTEM = """Eres el asistente de WhatsApp de una lavandería. Responde SOLO con la información de este YAML,
en español de México, cálido y breve (máx. 3 líneas, emoji ligero). Si la respuesta no está en el YAML,
o el dato dice TODO_CONFIRM, responde exactamente: NO_SE

{faq}"""


def llm_answer(text, c):
    if os.getenv("LLM_FALLBACK") != "1":
        return None
    import anthropic
    try:
        r = anthropic.Anthropic().beta.messages.create(
            model=os.getenv("LLM_MODEL", "claude-opus-5-5"),
            max_tokens=1024,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            cache_control={"type": "ephemeral"},
            system=SYSTEM.format(faq=yaml.safe_dump(c, allow_unicode=True)),
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.APIError as e:  # LLM is optional: any failure falls back to the menu
        print(f"[llm error] {e}", flush=True)
        return None
    if r.stop_reason == "refusal":
        return None
    out = "".join(b.text for b in r.content if b.type == "text").strip()
    return None if not out or "NO_SE" in out else out
