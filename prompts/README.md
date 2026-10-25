# LLM prompt

`system_fr.txt` is the system prompt the bot sends to the LLM (`[llm] prompt_file` in the config).

It is in French on purpose: the bot talks to French-speaking players, and small local models follow instructions better in the language of the conversation. If your server speaks another language, translate it (keep the JSON formats and the section names ACTIONS, CONTEXTE, EN ATTENTE, TRANSCRIPTION, which the code fills in).

How the bot uses it (`tsbot/router.py`):
- The LLM is only called when someone says a wake word (`[llm] wake_words`) or answers a pending confirmation.
- `system` message: this file. `user` message: the actions declared by plugins, their reference data (CONTEXTE), the action waiting for confirmation (EN ATTENTE) and the last 20 lines of conversation.
- `temperature: 0` and, by default, `response_format: {"type": "json_object"}`, which makes llama.cpp produce valid JSON.
- Every answer is checked by the code: unknown action, bad arguments or unknown person → nothing happens.

Degraded mode is not in the prompt: an LLM that does not answer does not read its prompt. The code handles it (3 failures in a row → the bot says so once and stops acting, then checks every 5 minutes).
