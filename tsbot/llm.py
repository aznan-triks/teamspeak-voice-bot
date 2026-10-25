"""Minimal client for any OpenAI-compatible chat server (llama.cpp's llama-server, vLLM, hosted APIs)."""
from __future__ import annotations

import json
import os
import re
import urllib.request


def parse_reply(content: str) -> dict:
    """The model's answer → a JSON object. Tolerates reasoning tags and text around the JSON."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    reply = json.loads(content[content.find("{"): content.rfind("}") + 1])  # ValueError when there is no JSON
    if not isinstance(reply, dict):
        raise ValueError("the reply is not a JSON object")
    return reply


class LLM:
    def __init__(self, cfg: dict):
        self.url = cfg["base_url"].rstrip("/")
        self.model = cfg.get("model", "")
        self.key = os.environ.get("LLM_API_KEY") or cfg.get("api_key", "")
        self.timeout = cfg.get("timeout_s", 20)
        self.json_mode = cfg.get("json_mode", True)

    def _request(self, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        data = json.dumps(body).encode() if body is not None else None
        with urllib.request.urlopen(urllib.request.Request(self.url + path, data, headers), timeout=self.timeout) as r:
            return json.load(r)

    def ping(self) -> bool:
        try:
            self._request("/models")
            return True
        except Exception:
            return False

    def chat(self, system: str, user: str) -> dict:
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return parse_reply(self._request("/chat/completions", body)["choices"][0]["message"]["content"])
