"""The management site's server side: login by TeamSpeak code, roles, and the JSON API behind the pages.

It never touches the host: it reads the archive (read only), writes the site settings file, and asks
the running bot everything else through its local control API.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import hmac
import importlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from tsbot.voice import DEFAULT_PHRASES

log = logging.getLogger(__name__)

SECRET = "secret"  # settings field type: never sent back to the browser
MASK = "••••••••"

# Every setting the site can change: (section, key, type, label, help). Read by the settings page.
FIELDS = [
    ("teamspeak", "address", "str", "Adresse du serveur TeamSpeak", "Adresse IP ou nom, avec « :port » si ce n'est pas 9987."),
    ("teamspeak", "server_password", SECRET, "Mot de passe du serveur", "Laisse vide si le serveur n'en a pas. Jamais affiché."),
    ("teamspeak", "channel_password", SECRET, "Mot de passe du salon", "Seulement si le salon du bot en a un. Jamais affiché."),
    ("teamspeak", "nickname", "str", "Pseudo du bot", "Nom affiché dans TeamSpeak. Garde « (enregistrement) » ou équivalent : les gens doivent savoir que le bot écoute."),
    ("teamspeak", "channel", "str", "Salon", "Chemin du salon, par exemple « Jeux/Soirée ». Vide = salon par défaut du serveur."),
    ("teamspeak", "max_utterance_s", "int", "Découpe des longues prises de parole (s)", "Au-delà, une prise de parole est coupée en morceaux de cette durée."),
    ("stt", "model", "choice:nemo-canary-1b-v2,nemo-parakeet-tdt-0.6b-v3", "Moteur de transcription", "Canary : langue imposée, garde « euh », « ouais » et les rires en français. Parakeet : un peu plus rapide, mais devine la langue et transforme souvent les sons courts en anglais."),
    ("stt", "engine", "choice:local,gemini", "Où transcrire", "local : sur le NUC, rien ne sort de la maison. gemini : Google transcrit, en connaissant les pseudos et les noms des joueurs (bien meilleur sur les noms et l'argot), mais TOUT ce qui se dit dans le salon part chez Google. Si Gemini échoue, le NUC prend le relais."),
    ("stt", "gemini_model", "str", "Modèle Gemini pour la transcription", "Par exemple gemini-flash-latest."),
    ("stt", "api_key", SECRET, "Clé API Gemini pour la transcription", "Clé Google AI Studio (aistudio.google.com). Vide = la même que celle des réponses."),
    ("stt", "language", "str", "Langue parlée", "Code de langue imposé à Canary, par exemple fr. Ignoré par Parakeet."),
    ("stt", "min_utterance_ms", "int", "Durée minimale d'une phrase (ms)", "Les sons plus courts (toux, clics) sont ignorés."),
    ("llm", "enabled", "bool", "Le bot répond et agit", "Désactivé : il transcrit et archive seulement."),
    ("llm", "wake_words", "list", "Mots d'éveil", "Mots qui réveillent le bot (son nom, au début de la phrase). Teste-les : la transcription doit les écrire juste. Un par ligne."),
    ("llm", "personality", "text", "Personnalité", "Comment le bot parle et se comporte : son caractère, son humour, ce qu'il aime. Il suit ce texte pour le ton, jamais pour enfreindre ses règles."),
    ("llm", "follow_up_s", "int", "Temps pour poursuivre la conversation (s)", "Après une réponse du bot, on peut continuer à lui parler sans redire son nom pendant ce temps. 0 = toujours dire son nom."),
    ("llm", "confirm_timeout_s", "int", "Temps pour dire « oui » (s)", "Délai après la question du bot, avant d'abandonner l'action."),
    ("llm", "warm_up", "bool", "Préchauffage au démarrage", "Le LLM lit ses consignes une fois au démarrage : la première question est bien plus rapide."),
    ("llm", "base_url", "str", "Adresse du serveur LLM", "Serveur compatible OpenAI, par exemple http://127.0.0.1:8091/v1."),
    ("llm", "model", "str", "Nom du modèle", "Envoyé au serveur ; llama-server l'ignore. Pour Gemini : par exemple gemini-flash-latest."),
    ("llm", "api_key", SECRET, "Clé API du serveur LLM", "Vide pour le modèle local du NUC. Pour Gemini : clé Google AI Studio, avec l'adresse https://generativelanguage.googleapis.com/v1beta/openai — les questions posées au bot et la conversation récente partent alors chez Google."),
    ("llm", "timeout_s", "int", "Délai maximal du LLM (s)", "Au-delà, la demande compte comme un échec."),
    ("llm", "max_failures", "int", "Échecs avant mode dégradé", "Nombre d'échecs du LLM d'affilée avant que le bot arrête d'agir (il continue d'écouter)."),
    ("llm", "probe_interval_s", "int", "Nouvel essai en mode dégradé (s)", "Le bot revérifie le LLM à cet intervalle."),
    ("voice", "engine", "choice:phonon,piper", "Voix", "Phonon : la plus naturelle (bascule seule sur Piper si indisponible). Piper : plus robotique, toujours disponible."),
    ("voice", "phonon_voice", "voice", "Voix Phonon", "La voix du bot. Écoute-la dans « Faire parler » après avoir appliqué. « default » = voix féminine d'origine."),
    ("voice", "piper_voice", "str", "Voix Piper", "Par exemple fr_FR-siwis-medium, fr_FR-tom-medium, fr_FR-upmc-medium."),
    ("voice.phrases", "degraded", "str", "Phrase : mode dégradé", "Dite quand le LLM ne répond plus. Texte, ou chemin d'un fichier audio."),
    ("voice.phrases", "restored", "str", "Phrase : retour à la normale", ""),
    ("voice.phrases", "cancelled", "str", "Phrase : action annulée", ""),
    ("voice.phrases", "failed", "str", "Phrase : action échouée", ""),
    ("voice.phrases", "invalid", "str", "Phrase : demande incomprise", ""),
    ("voice.phrases", "unknown_speaker", "str", "Phrase : personne inconnue", "Dite quand quelqu'un sans identité demande une action."),
    ("web", "admin_groups", "list", "Grades administrateurs du site", "Grades TeamSpeak dont les membres administrent ce site. Un par ligne."),
    ("web", "admin_uids", "list", "Administrateurs supplémentaires (UID)", "Identifiants TeamSpeak administrateurs même sans le grade. Un par ligne."),
]


def get_path(cfg: dict, dotted: str) -> dict:
    for part in dotted.split("."):
        cfg = cfg.get(part, {}) if isinstance(cfg, dict) else {}
    return cfg if isinstance(cfg, dict) else {}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, cfg: dict, config_path: str, opener=urllib.request.urlopen):
        self.cfg, self.config_path, self.opener = cfg, config_path, opener
        web = cfg.get("web", {})
        self.web = web
        self.settings_file = Path(web.get("settings_file", "data/settings.json"))
        self.token_file = Path(web.get("token_file", "data/control_token"))
        self.archive_path = cfg.get("archive", {}).get("path", "data/archive.sqlite3")
        secret_file = Path(web.get("secret_file", "data/web_secret"))
        if not secret_file.exists():
            secret_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            secret_file.chmod(0o600)
        self.secret = secret_file.read_text(encoding="utf-8").encode()
        self.codes: dict = {}  # client_id -> pending login code
        self.services = web.get("services", [])  # systemd user units the site may start, stop and read

    # --- the running bot ---

    def bot(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        url = f"http://127.0.0.1:{self.web.get('control_port', 8096)}{path}"
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(url, data, {"Authorization": f"Bearer {self.token_file.read_text().strip()}",
                                                 "Content-Type": "application/json"}, method=method)
        try:
            with self.opener(req, timeout=self.web.get("bot_timeout_s", 60)) as r:
                content = r.read()
        except (urllib.error.URLError, OSError, FileNotFoundError) as error:
            raise HttpError(503, f"Le bot ne répond pas ({error}). Il redémarre peut-être.")
        return content if raw else json.loads(content)

    # --- sessions ---

    def sign(self, data: dict) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        mac = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{mac}"

    def session(self, cookie: str | None) -> dict | None:
        if not cookie or "." not in cookie:
            return None
        payload, mac = cookie.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data.get("exp", 0) <= time.time():
            return None
        # Checked again on every request: someone removed from the admins loses access right away.
        data["admin"] = self.is_admin(data.get("uid"), data.get("groups", []))
        return data

    def is_admin(self, uid: str | None, groups) -> bool:
        cfg = self.current_config().get("web", {})
        return uid in cfg.get("admin_uids", []) or any(g in cfg.get("admin_groups", ["Server Admin"]) for g in groups)

    # --- login by code ---

    def login_clients(self) -> list:
        state = self.bot("GET", "/state")
        me = (state.get("me") or {}).get("client_id")
        return sorted(({"client_id": c["client_id"], "nickname": c.get("nickname", "?")}
                       for c in state["clients"] if c.get("uid") and c["client_id"] != me), key=lambda c: c["nickname"].lower())

    def bot_name(self) -> str:
        """The name shown on the site: the bot's TeamSpeak nickname without its "(…)" part."""
        nick = self.current_config().get("teamspeak", {}).get("nickname", "Bot")
        return nick.split("(")[0].strip() or nick

    def login_start(self, client_id: int, site_url: str = "") -> dict:
        pending = self.codes.get(client_id)
        if pending and time.time() - pending["sent"] < self.web.get("code_resend_s", 30):
            raise HttpError(429, "Un code vient d'être envoyé : attends quelques secondes avant d'en redemander un.")
        client = next((c for c in self.bot("GET", "/state")["clients"] if c["client_id"] == client_id), None)
        if client is None or not client.get("uid"):
            raise HttpError(404, "Cette personne n'est plus connectée au serveur.")
        code = f"{secrets.randbelow(1_000_000):06d}"
        ttl = self.web.get("code_ttl_s", 300)
        self.codes[client_id] = {"code": code, "client": client, "sent": time.time(), "exp": time.time() + ttl, "tries": 0}
        name = self.bot_name()
        link = f" Ou clique : {site_url}/?login={client_id}-{code}" if site_url else ""
        # The poke (100 characters at most) carries the code; the private message adds a link that logs in by itself.
        self.bot("POST", "/message", {"client_id": client_id, "poke": True, "text":
                 f"Code du site de {name} : {code} (5 min).{link} Si tu n'as rien demandé, ignore ce message."})
        log.info("login code sent to %s (client %d)", client.get("nickname"), client_id)
        return {"sent": True, "ttl_s": ttl}

    def login_finish(self, client_id: int, code: str) -> tuple[dict, str]:
        pending = self.codes.get(client_id)
        if not pending or pending["exp"] < time.time():
            raise HttpError(400, "Code expiré ou jamais demandé : redemande un code.")
        pending["tries"] += 1
        if pending["tries"] > self.web.get("code_tries", 5):
            del self.codes[client_id]
            raise HttpError(429, "Trop d'essais : redemande un code.")
        if not hmac.compare_digest(pending["code"], str(code).strip()):
            log.warning("wrong login code for %s (try %d)", pending["client"].get("nickname"), pending["tries"])
            raise HttpError(400, "Code incorrect : vérifie les 6 chiffres reçus dans TeamSpeak.")
        del self.codes[client_id]
        c = pending["client"]
        log.info("%s logged in", c.get("nickname"))
        user = {"uid": c["uid"], "nickname": c.get("nickname"), "identity": c.get("identity"),
                "groups": c.get("server_groups") or [], "admin": self.is_admin(c["uid"], c.get("server_groups") or []),
                "exp": time.time() + self.web.get("session_days", 30) * 86400}
        return user, self.sign(user)

    # --- archive (read only) ---

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.archive_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def my_phrases(self, uid: str, q: str = "", before_id: int = 0, limit: int = 50) -> list:
        sql = "SELECT id, started_ms, duration_ms, text FROM utterances WHERE uid = ?"
        args: list = [uid]
        if q:
            sql += " AND text LIKE ?"
            args.append(f"%{q}%")
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, min(limit, 200)])]

    # A sentence may be read by its author and by the people who could hear it when it was said.
    HEARD = "(uid = ? OR EXISTS (SELECT 1 FROM json_each(COALESCE(listeners, '[]')) WHERE value = ?))"

    def conversation(self, uid: str, after_id: int = 0, limit: int = 60) -> list:
        """What this person could hear, oldest first: the last `limit` sentences, or the new ones after `after_id`."""
        with closing(self.db()) as con:
            rows = con.execute(
                f"SELECT id, started_ms, COALESCE(identity, nickname) AS name, uid = ? AS mine, text FROM utterances "
                f"WHERE {self.HEARD} AND id > ? ORDER BY id DESC LIMIT ?", (uid, uid, uid, after_id, min(limit, 200))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def presence(self, uid: str) -> dict:
        """Can this person hear the bot's channel right now? Says why not, in plain words."""
        try:
            state = self.bot("GET", "/state")
        except HttpError:
            return {"hearing": False, "reason": "Le bot n'est pas connecté pour l'instant."}
        bot_channel = (state.get("me") or {}).get("channel_id")
        me = next((c for c in state["clients"] if c.get("uid") == uid), None)
        if me is None:
            return {"hearing": False, "reason": "Tu n'es pas connecté au serveur TeamSpeak."}
        if me.get("channel_id") != bot_channel:
            return {"hearing": False, "reason": f"Tu n'es pas dans le salon de {self.bot_name()}."}
        if not me.get("hearing"):
            return {"hearing": False, "reason": "Ton casque est coupé : tu n'entends pas le salon."}
        return {"hearing": True, "reason": ""}

    def actions(self, uid: str, admin: bool, before_id: int = 0, limit: int = 50) -> list:
        sql = "SELECT * FROM actions" + (" WHERE id < ?" if before_id else "") + " ORDER BY id DESC LIMIT ?"
        with closing(self.db()) as con:
            rows = [dict(r) for r in con.execute(sql, [before_id, min(limit, 200)] if before_id else [min(limit, 200)])]
            for row in rows:
                row["args"] = json.loads(row["args"])
                ids = json.loads(row.pop("utterance_ids") or "[]")
                row.pop("requested_uid")
                # The sentence that asked for it: only for its author and the people who could hear it.
                row["sentence"] = None
                if ids:
                    found = con.execute(f"SELECT text FROM utterances WHERE id = ? AND {self.HEARD}", (ids[0], uid, uid)).fetchone()
                    row["sentence"] = found["text"] if found else None
        return rows

    def stats(self, days: int = 30) -> dict:
        since = int((time.time() - days * 86400) * 1000)

        def spread(values: list) -> dict:
            values = sorted(values)
            if not values:
                return {"count": 0}
            pick = lambda f: values[min(len(values) - 1, int(len(values) * f))]
            return {"count": len(values), "median": pick(0.5), "p90": pick(0.9), "mean": round(sum(values) / len(values))}

        with closing(self.db()) as con:
            q = lambda sql: con.execute(sql, (since,)).fetchall()
            return {
                "days": days,
                "stt_ms": spread([r[0] for r in q("SELECT stt_ms FROM utterances WHERE started_ms > ? AND stt_ms IS NOT NULL")]),
                "llm_ms": spread([r[0] for r in q("SELECT ms FROM llm_calls WHERE started_ms > ? AND ok = 1")]),
                "llm_failures": q("SELECT COUNT(*) FROM llm_calls WHERE started_ms > ? AND ok = 0")[0][0],
                "speakers": [dict(r) for r in q(
                    "SELECT COALESCE(identity, nickname) AS name, COUNT(*) AS phrases, SUM(duration_ms) AS ms FROM utterances "
                    "WHERE started_ms > ? AND stt_ms IS NOT NULL GROUP BY name ORDER BY ms DESC LIMIT 15")],
                "per_day": [dict(r) for r in q(
                    "SELECT date(started_ms / 1000, 'unixepoch', 'localtime') AS day, COUNT(*) AS phrases FROM utterances "
                    "WHERE started_ms > ? AND stt_ms IS NOT NULL GROUP BY day ORDER BY day")],
                "actions": [dict(r) for r in q(
                    "SELECT status, COUNT(*) AS n FROM actions WHERE created_ms > ? GROUP BY status ORDER BY n DESC")],
            }

    # --- settings ---

    def overlay(self) -> dict:
        return json.loads(self.settings_file.read_text(encoding="utf-8")) if self.settings_file.exists() else {}

    def current_config(self) -> dict:
        from tsbot.__main__ import load_config

        return load_config(self.config_path)

    def plugin_names(self) -> list:
        found = {"tsbot.plugins_examples.scoreboard"}
        for folder in os.environ.get("PYTHONPATH", "").split(os.pathsep):
            if folder and Path(folder).is_dir():
                found |= {p.stem for p in Path(folder).glob("*.py") if not p.stem.startswith(("test_", "_"))}
        plugins = self.current_config().get("plugins", {})
        return sorted(found | set(plugins.get("enabled", [])) | {k for k, v in plugins.items() if isinstance(v, dict)})

    def settings(self) -> dict:
        cfg = self.current_config()
        plugins = cfg.get("plugins", {})
        sections = {name: {k: (MASK if k in ("password", "api_key", "token") and v else v)
                           for k, v in plugins.get(name, {}).items()} for name in self.plugin_names()}
        return {
            "fields": [{"section": s, "key": k, "type": t, "label": label, "help": h,
                        "value": (MASK if get_path(cfg, s).get(k) else "") if t == SECRET else get_path(cfg, s).get(k),
                        **({"type": "choice:" + ",".join(["default", *self.voice_names()])} if t == "voice" else {}),
                        "default": DEFAULT_PHRASES.get(k) if s == "voice.phrases" else None}
                       for s, k, t, label, h in FIELDS],
            "identities": cfg.get("identities", {}),
            "opt_out_uids": cfg.get("privacy", {}).get("opt_out_uids", []),
            "plugins": {"enabled": plugins.get("enabled", []), "available": self.plugin_names(), "config": sections},
            "pending_restart": self.settings_file.exists() and self.settings_file.stat().st_mtime * 1000 > self.bot_started_ms(),
        }

    def voice_names(self) -> list:
        folder = Path(self.current_config().get("voice", {}).get("phonon_voices_dir", "data/voices"))
        return sorted(p.stem for p in folder.glob("*.safetensors"))

    def bot_started_ms(self) -> float:
        try:
            return self.bot("GET", "/state")["started_ms"]
        except HttpError:
            return 0

    def save_settings(self, changes: dict) -> dict:
        """`changes`: {"fields": {"llm.wake_words": [...]}, "identities": {...}, "opt_out_uids": [...], "plugins": {...}}."""
        overlay = self.overlay()
        known = {f"{s}.{k}": ("choice:" + ",".join(["default", *self.voice_names()]) if t == "voice" else t) for s, k, t, *_ in FIELDS}
        for dotted, value in changes.get("fields", {}).items():
            kind = known.get(dotted)
            if kind is None:
                raise HttpError(400, f"Réglage inconnu : {dotted}")
            section, key = dotted.rsplit(".", 1)
            target = overlay
            for part in section.split("."):
                target = target.setdefault(part, {})
            if kind == SECRET and value == MASK:
                continue  # left as is: keep the saved password
            target[key] = self.checked("str" if kind == SECRET else kind, value, dotted)
        if "identities" in changes:
            table = changes["identities"]
            if not all(isinstance(k, str) and k.startswith(("uid:", "group:")) and isinstance(v, str) and v.strip() for k, v in table.items()):
                raise HttpError(400, "Chaque identité associe « uid:… » ou « group:… » à un nom non vide.")
            overlay["identities"] = {k: v.strip() for k, v in table.items()}
        if "opt_out_uids" in changes:
            overlay.setdefault("privacy", {})["opt_out_uids"] = self.checked("list", changes["opt_out_uids"], "exclusions")
        if "plugins" in changes:
            p = changes["plugins"]
            plugins = overlay.setdefault("plugins", {})
            if "enabled" in p:
                plugins["enabled"] = self.checked("list", p["enabled"], "plugins")
            for name, section in p.get("config", {}).items():
                old = get_path(self.current_config(), "plugins").get(name, {})
                # A masked secret left as is keeps its saved value.
                plugins[name] = {k: (old.get(k) if v == MASK else v) for k, v in section.items()}
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)  # may hold plugin passwords
        tmp.replace(self.settings_file)
        return self.settings()

    @staticmethod
    def checked(kind: str, value, name: str):
        if kind == "bool" and isinstance(value, bool):
            return value
        if kind == "int" and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if kind in ("str", "text") and isinstance(value, str):
            return value.strip()
        if kind == "list" and isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [v.strip() for v in value if v.strip()]
        if kind.startswith("choice:") and value in kind[7:].split(","):
            return value
        raise HttpError(400, f"Valeur invalide pour {name}.")

    def test_plugin(self, name: str) -> dict:
        """Starts the plugin here with its saved settings, the way the bot would, and reports what it sees."""
        if name not in self.plugin_names():
            raise HttpError(404, "Plugin inconnu.")
        section = get_path(self.current_config(), "plugins").get(name, {})
        try:
            plugin = importlib.import_module(name).Plugin(section)
            context = plugin.context() if hasattr(plugin, "context") else {}
            actions = [a.name for a in plugin.actions()] if hasattr(plugin, "actions") else []
        except Exception as error:
            return {"ok": False, "error": str(error)}
        summary = {k: (len(v) if isinstance(v, (list, dict)) else str(v)[:80]) for k, v in context.items()}
        empty = context and not any(v for v in context.values())
        return {"ok": not empty, "actions": actions, "context": summary,
                "error": "Le plugin démarre mais ne reçoit aucune donnée : vérifie l'adresse et le compte." if empty else None}

    # --- services (systemd user units on this machine) ---

    def _unit(self, name: str) -> str:
        if name not in self.services:
            raise HttpError(404, "Service inconnu.")
        return name

    def service_list(self) -> list:
        if self.services and not shutil.which("systemctl"):
            raise HttpError(503, "Pas de systemd sur cette machine : la page Services ne peut pas fonctionner ici.")
        out = []
        for name in self.services:
            r = subprocess.run(["systemctl", "--user", "show", name, "--timestamp=unix",
                                "-p", "ActiveState", "-p", "SubState", "-p", "ActiveEnterTimestamp", "-p", "NRestarts"],
                               capture_output=True, text=True, timeout=10)
            props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)  # systemctl picks the order
            out.append({"name": name, "active": props.get("ActiveState", ""), "sub": props.get("SubState", ""),
                        "since": props.get("ActiveEnterTimestamp", ""), "restarts": props.get("NRestarts", "0")})
        return out

    def service_action(self, name: str, action: str) -> dict:
        unit = self._unit(name)
        if action not in ("start", "stop", "restart"):
            raise HttpError(400, "Action inconnue.")
        if action == "stop" and unit == self.web.get("self_service", "web"):
            raise HttpError(400, "Le site ne peut pas s'arrêter lui-même : tu ne pourrais plus le relancer. Utilise « Redémarrer ».")
        log.info("service %s: %s", unit, action)
        # Not waited for: restarting the site itself ends this process.
        subprocess.Popen(["systemctl", "--user", action, unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}

    def service_logs(self, name: str, lines: int = 200) -> list:
        unit = self._unit(name)
        r = subprocess.run(["journalctl", "--user", "-u", unit, "-n", str(min(lines, 1000)), "-o", "short-iso", "--no-pager"],
                           capture_output=True, text=True, timeout=20)
        noise = ("RSA puzzle", "Unknown argument", "tsproto::resend", "image pull", "container ")
        return [line for line in r.stdout.splitlines() if not any(n in line for n in noise)]
