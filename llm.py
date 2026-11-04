"""Optional LLM fallback: answers only from config/lave.yaml. Off unless LLM_FALLBACK=1.
LLM_BACKEND=anthropic (default, ANTHROPIC_API_KEY) or bedrock (AWS credentials, Bedrock Converse API)."""
import os

import yaml

SYSTEM = """Eres el asistente de WhatsApp de una lavandería. Responde SOLO con la información de este YAML,
en español de México, cálido y breve (máx. 3 líneas, emoji ligero). Si la respuesta no está en el YAML,
o el dato dice TODO_CONFIRM, responde exactamente: NO_SE

{faq}"""


def llm_answer(text, c):
    if os.getenv("LLM_FALLBACK") != "1":
        return None
    system = SYSTEM.format(faq=yaml.safe_dump(c, allow_unicode=True))
    out = (_bedrock if os.getenv("LLM_BACKEND") == "bedrock" else _anthropic)(system, text)
    return None if not out or "NO_SE" in out else out


def _anthropic(system, text):
    import anthropic
    try:
        r = anthropic.Anthropic().beta.messages.create(
            model=os.getenv("LLM_MODEL", "claude-opus-5-5"),
            max_tokens=1024,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            cache_control={"type": "ephemeral"},
            system=system,
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.APIError as e:  # LLM is optional: any failure falls back to the menu
        print(f"[llm error] {e}", flush=True)
        return None
    if r.stop_reason == "refusal":
        return None
    return "".join(b.text for b in r.content if b.type == "text").strip()


def _bedrock(system, text):
    import boto3
    try:
        r = boto3.client("bedrock-runtime").converse(
            modelId=os.getenv("LLM_MODEL", "anthropic.claude-opus-5-5"),
            system=[{"text": system}, {"cachePoint": {"type": "default"}}],  # the YAML prefix is the same every call
            messages=[{"role": "user", "content": [{"text": text}]}],
            inferenceConfig={"maxTokens": 512},  # 3-line answers; thinking shares this budget
        )
    except Exception as e:  # botocore ClientError, throttling, timeouts: fall back to the menu
        print(f"[llm error] {e}", flush=True)
        return None
    if r["stopReason"] != "end_turn":  # max_tokens (half an answer), content_filtered, guardrail: menu instead
        return None
    return "".join(b.get("text", "") for b in r["output"]["message"]["content"]).strip()
