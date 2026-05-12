"""AI Gateway client using OpenAI-compatible chat completions API."""
import json
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict:
    """Extract a JSON object from LLM response text."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract valid JSON from LLM response: {text[:200]}")


class AIGatewayClient:
    """Client for the SS&C AI Gateway (Tarvos) OpenAI-compatible API."""

    def __init__(self, url: Optional[str], api_key: Optional[str]):
        self.url = url.rstrip("/") if url else None
        self.api_key = api_key
        self.available = bool(url and api_key)

    async def check_connection(self) -> bool:
        if not self.available:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self.url}/v1/models",
                    headers={"X-API-Key": self.api_key},
                )
                return resp.status_code == 200
        except Exception as e:
            logger.warning("AI Gateway connection check failed: %s", e)
            return False

