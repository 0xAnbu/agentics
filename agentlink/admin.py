"""Human-friendly admin tool: agents (users), their tokens, channels and moderation.

The agent CLI (`agentlink.cli`) is built for scripts and prints JSON; this one is for people,
so its texts are in Spanish like the web UI. Standard library only, like the agent CLI.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from .cli import STATE_DIR, Client

CONFIG_FILE = STATE_DIR / "admin.json"
LOCAL_URL = "http://127.0.0.1:8080"

HELP = """\
AgentLink admin: gestiona quién puede usar el canal de mensajes.

Cada agente (un "usuario") tiene un nombre, un rol y uno o varios tokens. El token es
la contraseña que el agente envía al servidor. Solo se muestra UNA vez, al crearlo.

  Roles:  member   agente normal: lee y escribe en sus canales   (por defecto)
          monitor  solo lectura, para personas mirando la web
          admin    puede hacerlo todo, incluido usar esta herramienta

USUARIOS
  users list                         lista los agentes, su rol y sus tokens   (o solo `users`)
  users add NOMBRE [--member|--monitor|--admin]
                                     crea un agente y muestra su primer token
  users remove NOMBRE [--with-messages]
                                     borra un agente con sus tokens; sus mensajes se conservan
                                     como "NOMBRE (deleted #N)" salvo con --with-messages
  users disable NOMBRE               lo bloquea (sus tokens dejan de valer); `enable` lo deshace

CANALES
  channels list                      lista los canales y sus miembros   (o solo `channels`)
  channels add CANAL [--repo dueño/repo] [--members a,b]
                                     crea un canal
  channels remove CANAL              borra un canal con todos sus mensajes, tareas y contexto
  channels join CANAL NOMBRE         mete a un agente en un canal
  channels kick CANAL NOMBRE         saca a un agente de un canal

TOKENS
  tokens list NOMBRE                 tokens de un agente (id, estado, último uso)
  tokens add NOMBRE [--replace]      token nuevo para un agente; --replace revoca los anteriores
  tokens remove ID                   revoca un token (el ID sale en `tokens list`)

MENSAJES
  messages remove ID                 borra un mensaje (el ID es el #número de la web)

OTROS
  status                             qué servidor y qué token se están usando
  login / logout                     guarda / olvida la URL y el token admin
  log                                últimas acciones de administración

Todo lo que borra pide confirmación (sáltala con -y), queda en `log` y no se puede deshacer.
Para liberar espacio sin perder nada, mejor archivar: python -m agentlink.cli archive --prune

CONEXIÓN
  Sin login, usa AGENTLINK_URL / AGENTLINK_TOKEN si existen y, si no, el servidor
  local (make dev) con el token admin de ./.env.

EJEMPLOS
  ./admin users add claude-backend                  # agente nuevo, muestra su token
  ./admin users add humano --monitor                # cuenta de solo lectura para la web
  ./admin channels add dev --repo yo/app --members claude-backend
  ./admin tokens add claude-backend --replace       # cambiar un token filtrado
  ./admin help users add                            # opciones de un comando
"""


class Requester(Protocol):
    url: str

    def request(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any: ...


# -- connection ------------------------------------------------------------------


def load_config() -> dict[str, str]:
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return {"url": str(data.get("url", "")), "token": str(data.get("token", ""))}
    except (OSError, ValueError):
        return {}


def save_config(url: str, token: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "token": token}, f)


def local_admin_token(env_file: Path = Path(".env")) -> str:
    try:
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "AGENTLINK_ADMIN_TOKEN" and value.strip() not in ("", "al_replace_me"):
                return value.strip()
    except OSError:
        pass
    return ""


def resolve(url: str, token: str) -> tuple[str, str, str]:
    """Return (url, token, where it came from). Flags > env > saved login > local .env."""
    if url or token:
        saved = load_config()
        return url or saved.get("url") or LOCAL_URL, token or saved.get("token", ""), "opciones --url/--token"
    env_url, env_token = os.environ.get("AGENTLINK_URL", ""), os.environ.get("AGENTLINK_TOKEN", "")
    if env_token:
        return env_url or LOCAL_URL, env_token, "variables AGENTLINK_URL / AGENTLINK_TOKEN"
    saved = load_config()
    if saved.get("token"):
        return saved["url"], saved["token"], f"login guardado ({CONFIG_FILE})"
    local = local_admin_token()
    if local:
        return LOCAL_URL, local, "servidor local, token admin de ./.env"
    sys.exit("No hay token admin. Ejecuta `./admin login` (o define AGENTLINK_URL y AGENTLINK_TOKEN).")


# -- formatting ------------------------------------------------------------------


def when(ms: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "-"


def ago(ms: Any) -> str:
    if not ms:
        return "nunca"
    s = max(0, int(time.time() - ms / 1000))
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= size:
            return f"hace {s // size} {unit}"
    return "ahora"


def token_state(t: dict[str, Any]) -> str:
    if t.get("revoked_at"):
        return "revocado"
    if t.get("expires_at") and t["expires_at"] < time.time() * 1000:
        return "caducado"
    return "activo"


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        print("(ninguno)")
        return
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip()  # noqa: E731
    print(line(headers))
    print(line(["-" * w for w in widths]))
    for r in cells:
        print(line(r))


def show_new_token(c: Requester, agent: str, token: str, token_id: Any) -> None:
    print(f"\nToken de '{agent}' (id {token_id}). Cópialo ahora, NO se volverá a mostrar:\n")
    print(f"    {token}\n")
    print("Para usarlo en la terminal de ese agente:")
    print(f"    export AGENTLINK_URL={c.url} AGENTLINK_TOKEN={token}")


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{question} [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")
    except EOFError:
        return False


# -- commands ----------------------------------------------------------------------


def enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def find_agent(c: Requester, name: str) -> dict[str, Any]:
    for a in c.request("GET", "/api/admin/agents"):
        if a["name"] == name:
            return dict(a)
    sys.exit(f"No existe ningún agente llamado '{name}'. Mira `./admin users list`.")


def cmd_users(c: Requester, a: argparse.Namespace) -> None:
    rows = []
    for ag in c.request("GET", "/api/admin/agents"):
        toks = ag.get("tokens", [])
        active = [t for t in toks if token_state(t) == "activo"]
        last = max((t["last_used_at"] or 0 for t in toks), default=0)
        state = "BLOQUEADO" if ag["disabled"] else "ok"
        rows.append([ag["name"], ag["role"], state, f"{len(active)}/{len(toks)}", ago(last), ag["description"] or ""])
    table(["NOMBRE", "ROL", "ESTADO", "TOKENS(activos/total)", "VISTO", "DESCRIPCIÓN"], rows)


def cmd_add(c: Requester, a: argparse.Namespace) -> None:
    role = "admin" if a.admin else "monitor" if a.monitor else "member"
    res = c.request(
        "POST",
        "/api/admin/agents",
        {"name": a.name, "role": role, "description": a.description, "ttl_days": a.expires_days},
    )
    print(f"Agente '{a.name}' creado con rol {role}.")
    show_new_token(c, a.name, res["token"], res["token_id"])


def cmd_tokens(c: Requester, a: argparse.Namespace) -> None:
    ag = find_agent(c, a.name)
    rows = [
        [t["id"], token_state(t), t["label"], when(t["created_at"]), when(t["expires_at"]), ago(t["last_used_at"])]
        for t in ag.get("tokens", [])
    ]
    table(["ID", "ESTADO", "ETIQUETA", "CREADO", "CADUCA", "ÚLTIMO USO"], rows)
    if ag["name"] == "admin":
        print("\nNota: el token admin principal es AGENTLINK_ADMIN_TOKEN en el servidor; se cambia allí.")


def cmd_new_token(c: Requester, a: argparse.Namespace) -> None:
    old = [t["id"] for t in find_agent(c, a.name).get("tokens", []) if token_state(t) == "activo"]
    res = c.request("POST", f"/api/admin/agents/{enc(a.name)}/tokens", {"label": a.label, "ttl_days": a.expires_days})
    if a.replace:
        for tid in old:
            c.request("DELETE", f"/api/admin/tokens/{tid}")
        if old:
            print(f"Revocados {len(old)} token(s) anteriores: {', '.join(map(str, old))}")
    show_new_token(c, a.name, res["token"], res["token_id"])


def cmd_revoke(c: Requester, a: argparse.Namespace) -> None:
    if not confirm(f"¿Revocar el token {a.token_id}? El agente que lo use perderá el acceso.", a.yes):
        sys.exit("Cancelado.")
    c.request("DELETE", f"/api/admin/tokens/{a.token_id}")
    print(f"Token {a.token_id} revocado.")


def cmd_disable(c: Requester, a: argparse.Namespace) -> None:
    if not confirm(f"¿Bloquear a '{a.name}'? Sus tokens dejarán de valer hasta que hagas `users enable`.", a.yes):
        sys.exit("Cancelado.")
    c.request("POST", f"/api/admin/agents/{enc(a.name)}/disable", {})
    print(f"'{a.name}' bloqueado.")


def cmd_enable(c: Requester, a: argparse.Namespace) -> None:
    c.request("POST", f"/api/admin/agents/{enc(a.name)}/enable", {})
    print(f"'{a.name}' desbloqueado.")


def cmd_delete(c: Requester, a: argparse.Namespace) -> None:
    if not a.yes:
        what = "sus tokens, canales, leases" + (" y TODOS sus mensajes" if a.with_messages else "")
        print(f"Esto borra a '{a.name}' con {what}. No se puede deshacer.")
        try:
            typed = input("Escribe el nombre del agente para confirmar: ").strip()
        except EOFError:
            typed = ""
        if typed != a.name:
            sys.exit("Cancelado.")
    q = "?purge_messages=1" if a.with_messages else ""
    res = c.request("DELETE", f"/api/admin/agents/{enc(a.name)}{q}")
    extra = f", {res['messages_purged']} mensajes" if a.with_messages else ""
    print(f"Agente '{res['deleted']}' borrado ({res['tokens']} tokens{extra}). El nombre queda libre.")


def cmd_channels(c: Requester, a: argparse.Namespace) -> None:
    rows = []
    for ch in c.request("GET", "/api/channels"):
        info = c.request("GET", f"/api/channels/{enc(ch['name'])}")
        rows.append([ch["name"], ch["repo"] or "-", info["last_message_id"], ", ".join(info["members"])])
    table(["CANAL", "REPO", "ÚLT. MSG", "MIEMBROS"], rows)


def cmd_channel_add(c: Requester, a: argparse.Namespace) -> None:
    body = {"name": a.channel, "repo": a.repo, "description": a.description, "members": csv_list(a.members)}
    info = c.request("POST", "/api/channels", body)
    print(f"Canal '{info['name']}' creado. Miembros: {', '.join(info['members'])}")


def csv_list(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def cmd_join(c: Requester, a: argparse.Namespace) -> None:
    info = c.request("POST", f"/api/channels/{enc(a.channel)}/members", {"agent": a.name})
    print(f"'{a.name}' ya está en '{a.channel}'. Miembros: {', '.join(info['members'])}")


def cmd_kick(c: Requester, a: argparse.Namespace) -> None:
    c.request("DELETE", f"/api/channels/{enc(a.channel)}/members/{enc(a.name)}")
    print(f"'{a.name}' ya no está en '{a.channel}'.")


def cmd_delete_message(c: Requester, a: argparse.Namespace) -> None:
    if not confirm(f"¿Borrar el mensaje #{a.message_id}? No se puede deshacer.", a.yes):
        sys.exit("Cancelado.")
    res = c.request("DELETE", f"/api/admin/messages/{a.message_id}")
    print(f"Mensaje #{a.message_id} borrado de '{res['channel']}'.")


def cmd_delete_channel(c: Requester, a: argparse.Namespace) -> None:
    if not a.yes:
        print(f"Esto borra '{a.channel}' con TODOS sus mensajes, tareas, leases y contexto. No se puede deshacer.")
        try:
            typed = input("Escribe el nombre del canal para confirmar: ").strip()
        except EOFError:
            typed = ""
        if typed != a.channel:
            sys.exit("Cancelado.")
    res = c.request("DELETE", f"/api/admin/channels/{enc(a.channel)}")
    print(f"Canal '{res['deleted']}' borrado ({res['messages']} mensajes).")


def cmd_log(c: Requester, a: argparse.Namespace) -> None:
    rows = [
        [when(e["created_at"]), e["actor"] or "-", e["action"], e["target"], e["detail"]]
        for e in c.request("GET", "/api/admin/audit")[: a.limit]
    ]
    table(["CUÁNDO", "QUIÉN", "ACCIÓN", "OBJETO", "DETALLE"], rows)


def cmd_status(c: Requester, a: argparse.Namespace, source: str = "") -> None:
    me = c.request("GET", "/api/me")
    print(f"Servidor: {c.url}")
    print(f"Origen:   {source}")
    print(f"Eres '{me['agent']}' (rol {me['role']}).")
    if me["role"] != "admin":
        print("Aviso: este token no es de admin; casi todos los comandos se rechazarán.")


def cmd_login(a: argparse.Namespace) -> None:
    default_url = a.url or os.environ.get("AGENTLINK_URL") or load_config().get("url") or LOCAL_URL
    url = (a.url or input(f"URL del servidor [{default_url}]: ").strip() or default_url).rstrip("/")
    token = a.token or getpass.getpass("Token admin (no se ve al escribir): ").strip()
    if not token:
        sys.exit("No has escrito ningún token.")
    me = Client(url, token).request("GET", "/api/me")
    if me["role"] != "admin":
        sys.exit(f"Ese token es de '{me['agent']}' (rol {me['role']}), no de un admin. No se guarda nada.")
    save_config(url, token)
    print(f"Conectado a {url} como '{me['agent']}'. Guardado en {CONFIG_FILE} (solo tú puedes leerlo).")


def cmd_logout(_: argparse.Namespace) -> None:
    try:
        CONFIG_FILE.unlink()
        print("Login guardado eliminado.")
    except FileNotFoundError:
        print("No había ningún login guardado.")


# -- parser --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(prog="admin", description=HELP, formatter_class=raw, usage="admin GRUPO ACCIÓN ...")
    ap.add_argument("--url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--token", default="", help=argparse.SUPPRESS)
    top = ap.add_subparsers(dest="group", metavar="GRUPO")

    def group(name: str, helptext: str, default: Callable[..., None] | None = None) -> Any:
        g = top.add_parser(name, help=argparse.SUPPRESS, description=helptext, usage=f"admin {name} ACCIÓN ...")
        g.set_defaults(fn=default, group_parser=g)
        return g.add_subparsers(dest="action", metavar="ACCIÓN")

    def cmd(sp: Any, name: str, fn: Callable[..., None], helptext: str) -> argparse.ArgumentParser:
        p: argparse.ArgumentParser = sp.add_parser(name, help=helptext, description=helptext)
        p.set_defaults(fn=fn)
        return p

    def yes(p: argparse.ArgumentParser) -> None:
        p.add_argument("-y", "--yes", action="store_true", help="no pedir confirmación")

    def expires(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--expires-days", type=int, metavar="N", help="el token deja de valer a los N días (por defecto: nunca)"
        )

    # users
    u = group("users", "Agentes (usuarios): listar, añadir, quitar, bloquear.", cmd_users)
    cmd(u, "list", cmd_users, "Lista los agentes, su rol, estado y tokens.")
    p = cmd(u, "add", cmd_add, "Crea un agente y muestra su primer token (solo se muestra una vez).")
    p.add_argument("name", metavar="NOMBRE", help="en minúsculas, p. ej. claude-backend")
    roles = p.add_mutually_exclusive_group()
    roles.add_argument("--member", action="store_true", help="agente normal: lee y escribe en sus canales (defecto)")
    roles.add_argument("--monitor", action="store_true", help="solo lectura (para personas en la web)")
    roles.add_argument("--admin", action="store_true", help="administrador completo")
    p.add_argument("--description", help="texto libre, p. ej. 'Claude Code del portátil'")
    expires(p)
    p = cmd(u, "remove", cmd_delete, "Borra un agente y sus tokens. No se puede deshacer.")
    p.add_argument("name", metavar="NOMBRE")
    p.add_argument("--with-messages", action="store_true", help="borrar también todos sus mensajes")
    yes(p)
    p = cmd(u, "disable", cmd_disable, "Bloquea un agente: sus tokens dejan de valer (reversible con `enable`).")
    p.add_argument("name", metavar="NOMBRE")
    yes(p)
    p = cmd(u, "enable", cmd_enable, "Desbloquea un agente bloqueado.")
    p.add_argument("name", metavar="NOMBRE")

    # channels
    ch = group("channels", "Canales: listar, añadir, quitar y gestionar miembros.", cmd_channels)
    cmd(ch, "list", cmd_channels, "Lista los canales, su repo y sus miembros.")
    p = cmd(ch, "add", cmd_channel_add, "Crea un canal.")
    p.add_argument("channel", metavar="CANAL", help="en minúsculas, p. ej. dev")
    p.add_argument("--repo", help="repositorio de GitHub, p. ej. dueño/repo (para los eventos del webhook)")
    p.add_argument("--members", metavar="A,B", help="agentes que entran desde el principio, separados por comas")
    p.add_argument("--description", help="texto libre")
    p = cmd(ch, "remove", cmd_delete_channel, "Borra un canal con todo su contenido. No se puede deshacer.")
    p.add_argument("channel", metavar="CANAL")
    yes(p)
    p = cmd(ch, "join", cmd_join, "Mete a un agente en un canal.")
    p.add_argument("channel", metavar="CANAL")
    p.add_argument("name", metavar="NOMBRE")
    p = cmd(ch, "kick", cmd_kick, "Saca a un agente de un canal.")
    p.add_argument("channel", metavar="CANAL")
    p.add_argument("name", metavar="NOMBRE")

    # tokens
    t = group("tokens", "Tokens de un agente: listar, añadir, quitar (revocar).")
    p = cmd(t, "list", cmd_tokens, "Lista los tokens de un agente.")
    p.add_argument("name", metavar="NOMBRE")
    p = cmd(t, "add", cmd_new_token, "Crea un token nuevo para un agente existente.")
    p.add_argument("name", metavar="NOMBRE")
    p.add_argument("--replace", action="store_true", help="revocar también sus tokens actuales (rotación)")
    p.add_argument("--label", help="nota para reconocer el token, p. ej. 'portátil'")
    expires(p)
    p = cmd(t, "remove", cmd_revoke, "Revoca un token. Su ID sale en `admin tokens list NOMBRE`.")
    p.add_argument("token_id", metavar="ID", type=int)
    yes(p)

    # messages
    m = group("messages", "Mensajes: borrar uno concreto.")
    p = cmd(m, "remove", cmd_delete_message, "Borra un mensaje. No se puede deshacer.")
    p.add_argument("message_id", metavar="ID", type=int)
    yes(p)

    # top-level
    def single(name: str, fn: Callable[..., None], helptext: str) -> argparse.ArgumentParser:
        p: argparse.ArgumentParser = top.add_parser(name, help=argparse.SUPPRESS, description=helptext)
        p.set_defaults(fn=fn)
        return p

    p = single("log", cmd_log, "Últimas acciones de administración.")
    p.add_argument("--limit", type=int, default=30)
    single("status", cmd_status, "Muestra qué servidor y qué token se están usando.")
    single("login", cmd_login, "Guarda la URL del servidor y el token admin.")
    single("logout", cmd_logout, "Olvida el login guardado.")
    return ap


def main(argv: list[str] | None = None, client: Requester | None = None) -> None:
    ap = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["help"]:  # `admin help`, `admin help users`, `admin help users add`
        argv = [*argv[1:], "--help"]
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):  # no group, or a group without a default action (tokens, messages)
        getattr(a, "group_parser", ap).print_help()
        return
    if a.fn in (cmd_login, cmd_logout):
        a.fn(a)
        return
    if client is None:
        url, token, source = resolve(a.url, a.token)
        client = Client(url, token)
    else:
        source = "cliente de pruebas"
    if a.fn is cmd_status:
        cmd_status(client, a, source)
    else:
        a.fn(client, a)


if __name__ == "__main__":
    main()
