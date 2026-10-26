"""Minimal client for any OpenAI-compatible chat server (llama.cpp's llama-server, vLLM, hosted APIs)."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request

log = logging.getLogger(__name__)


def parse_reply(content: str) -> dict:
    """The model's answer → a JSON object. Tolerates reasoning tags and text around the JSON."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    # The first object only: DeepSeek sometimes writes two in a row. ValueError when there is no JSON.
    reply = json.JSONDecoder().raw_decode(content[content.find("{"):])[0]
    if not isinstance(reply, dict):
        raise ValueError("the reply is not a JSON object")
    return reply


class LLM:
    def __init__(self, cfg: dict):
        self.url = cfg["base_url"].rstrip("/")
        self.model = cfg.get("model", "")
        self.key = cfg.get("api_key") or os.environ.get("LLM_API_KEY", "")  # the site's setting wins over .env
        self.temperature = cfg.get("temperature", 0.5)  # 0 made a small model repeat itself word for word
        self.timeout = cfg.get("timeout_s", 20)
        self.json_mode = cfg.get("json_mode", True)
        self.extra = cfg.get("extra", {})  # provider options, e.g. DeepSeek: {"reasoning_effort": "low"}
        if cfg.get("thinking") in ("off", "on"):  # DeepSeek's switch; "auto" sends nothing (other servers)
            self.extra = {"thinking": {"type": "disabled" if cfg["thinking"] == "off" else "enabled"}, **self.extra}
        # When this service stops answering (quota, overload, outage, refused key), the next one of `fallbacks` is
        # used: [{base_url, model, api_key or api_key_env}], each taking the rest of this config. A failing one rests
        # rest_s seconds, twice as long each time in a row (10 min at most).
        self.backups = []
        for b in cfg.get("fallbacks", []):
            backup = LLM({**{k: v for k, v in cfg.items() if k != "fallbacks"}, **b})
            if "api_key" in b or "api_key_env" in b:  # otherwise: another model with the same key
                backup.key = b.get("api_key") or os.environ.get(b.get("api_key_env", ""), "")
            self.backups.append(backup)
        self.rest_s, self.resting_until, self.fails = cfg.get("rest_s", 60), 0.0, 0

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

    def request_chat(self, messages: list) -> str:
        chain = [self, *self.backups]
        # Resting services are skipped; when all of them rest, the one back soonest is tried anyway.
        ready = [p for p in chain if p.resting_until <= time.monotonic()] or [min(chain, key=lambda p: p.resting_until)]
        for p in ready:
            try:
                text = p._chat_once(messages)
                p.fails = 0
                return text
            except Exception as error:
                p.fails += 1
                p.resting_until = time.monotonic() + min(p.rest_s * 2 ** (p.fails - 1), 600)
                if p is ready[-1]:
                    raise
                log.warning("%s (%s) not answering, next one used: %s", p.model, p.url, str(error)[:150])

    def _chat_once(self, messages: list) -> str:
        body = {"model": self.model, "temperature": self.temperature, "messages": messages, **self.extra}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        # Gemini leaves "content" out of an empty answer (live, on a short "Oui."), which the transcription allows.
        return self._request("/chat/completions", body)["choices"][0]["message"].get("content") or ""

    def chat(self, system: str, user: str) -> dict:
        return parse_reply(self.request_chat([{"role": "system", "content": system}, {"role": "user", "content": user}]))
