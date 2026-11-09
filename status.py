#!/usr/bin/env python3
"""
bot-status.py — Monitor de status + bot interativo no Telegram

Modos de uso:
  python3 bot-status.py             # Envia o relatório uma vez (ideal para cron)
  python3 bot-status.py --bot       # Inicia o modo bot (escuta mensagens)

Comandos aceitos no Telegram:
  - "oi"               → mostra menu de opções
  - "oi, tudo bem?"    → envia o relatório de status direto
  - "1" / "status"     → envia o relatório de status
"""

import argparse
import configparser
import platform
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from datetime import datetime

import psutil
import requests

# ================== CONFIGURAÇÃO ==================
SERVICOS = [
    "ssh",
    "cron",
    # "nginx",
    # "docker",
]

HOST_TESTE   = "8.8.8.8"
PORTA_TESTE  = 53

VERIFICAR_IP_PUBLICO = True
# ===================================================

LINHA = "─" * 52


# ---------- HELPERS ----------
def uptime_str() -> str:
    s = time.time() - psutil.boot_time()
    d, h, m = int(s // 86400), int((s % 86400) // 3600), int((s % 3600) // 60)
    return f"{d}d {h}h {m}m"


def disco_str() -> str:
    u = shutil.disk_usage("/")
    gb = 1024 ** 3
    return f"{u.used/gb:.1f}GB / {u.total/gb:.1f}GB ({u.used/u.total*100:.1f}%)"


def ip_local() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "N/A"


def ip_publico() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as r:
            return r.read().decode().strip()
    except Exception:
        return "N/A"


def testar_conexao(host: str = HOST_TESTE, porta: int = PORTA_TESTE):
    try:
        with socket.create_connection((host, porta), timeout=3):
            return True, None
    except Exception as e:
        return False, str(e)


def status_servico(nome: str) -> dict:
    def _run(*args) -> str:
        try:
            r = subprocess.run(
                ["systemctl", *args, nome],
                capture_output=True, text=True, timeout=3,
            )
            return (r.stdout or r.stderr).strip() or "erro"
        except Exception as e:
            return f"erro: {e}"

    return {
        "nome": nome,
        "active": _run("is-active"),
        "enabled": _run("is-enabled"),
    }


# ---------- BLOCOS ----------
def bloco_fetch() -> list[str]:
    u = platform.uname()
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    return [
        f"Host:     {u.node}",
        f"OS:       {u.system} {u.release}",
        f"Kernel:   {u.version[:60]}",
        f"Arch:     {u.machine}",
        f"Uptime:   {uptime_str()}",
        f"CPU:      {cpu:.1f}% ({psutil.cpu_count(logical=True)} threads)",
        f"RAM:      {mem.used/1024**3:.1f}GB / {mem.total/1024**3:.1f}GB ({mem.percent:.1f}%)",
        f"Disco:    {disco_str()}",
    ]


def bloco_rede() -> list[str]:
    ok, err = testar_conexao()
    conectado = "✅ OK" if ok else f"❌ {err}"
    out = [
        f"IP local:    {ip_local()}",
        f"Conectividade ({HOST_TESTE}:{PORTA_TESTE}): {conectado}",
    ]
    if VERIFICAR_IP_PUBLICO:
        out.append(f"IP público:  {ip_publico()}")
    return out


def bloco_servicos() -> list[str]:
    out = []
    for nome in SERVICOS:
        s = status_servico(nome)
        icone = "✅" if s["active"] == "active" else "❌"
        out.append(
            f"{icone} {s['nome']:<12} active={s['active']:<10} enabled={s['enabled']}"
        )
    return out


# ---------- RELATÓRIO ----------
def gerar_relatorio() -> str:
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    linhas = [f"🖥️ *RELATÓRIO DE STATUS — {agora}*", LINHA]
    secoes = [
        ("📊 *SISTEMA*",  bloco_fetch()),
        ("🌐 *REDE*",     bloco_rede()),
        ("⚙️ *SERVIÇOS*", bloco_servicos()),
    ]
    for titulo, itens in secoes:
        linhas.append(f"\n{titulo}")
        for i in itens:
            linhas.append(f"   {i}")
    linhas.append("\n" + LINHA)
    return "\n".join(linhas)


# ---------- TELEGRAM ----------
def carregar_credenciais():
    config = configparser.ConfigParser()
    if not config.read("config.ini"):
        raise RuntimeError("arquivo config.ini não encontrado")
    return config["Telegram"]["token"], str(config["Telegram"]["chat_id"])


def enviar_mensagem(texto: str, token: str, chat_id: str,
                    parse_mode: str = "Markdown") -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": texto, "parse_mode": parse_mode}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print(f"❌ Resposta API Telegram: {r.text}")
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"❌ Falha ao enviar mensagem: {e}")
        return False


def get_updates(token: str, offset=None, timeout: int = 25) -> list:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    payload = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    try:
        r = requests.post(url, json=payload, timeout=timeout + 10)
        r.raise_for_status()
        return r.json().get("result", [])
    except requests.exceptions.RequestException as e:
        print(f"❌ getUpdates: {e}")
        return []


# ---------- INTERPRETAÇÃO DAS MENSAGENS ----------
MENU_TEXTO = (
    "👋 Olá! Escolha uma opção de monitoramento:\n\n"
    "1️⃣ *Status do servidor* — envie *1* ou *status*\n\n"
    "💡 Dica: envie *oi, tudo bem?* para receber o relatório direto."
)

SAUDACAO_MENU = {
    "oi", "ola", "olá", "eae", "eai", "hey", "start", "menu",
    "bom dia", "boa tarde", "boa noite", "ajuda", "help",
}

SAUDACAO_STATUS = {
    "oi tudo bem", "oi tudo bom", "oi td bem", "oi td bom",
    "oi como vai", "oi tudo bem com voce", "oi tudo bem voce",
    "e ai tudo bem", "eae tudo bem", "eai tudo bem",
}

OPCOES_STATUS = {
    "1", "status", "status do servidor", "status servidor",
    "servidor", "relatorio", "relatório",
}


def _normalizar(texto: str) -> str:
    """Remove pontuação e normaliza espaços (mantém acentos)."""
    t = re.sub(r"[^\w\sÀ-ÿ]", "", texto.lower())
    return re.sub(r"\s+", " ", t).strip()


def processar_mensagem(texto: str):
    """
    Retorna (resposta_pronta, precisa_status).
    Se precisa_status=True, 'resposta_pronta' é ignorada e o relatório é gerado.
    """
    t = texto.strip().lower()
    t_norm = _normalizar(texto)

    # 1) "oi, tudo bem?" (e variações) → status direto
    if t_norm in SAUDACAO_STATUS:
        return None, True

    # 2) "oi" / "start" / "menu" / "bom dia" → mostra menu
    if t_norm in SAUDACAO_MENU or t in SAUDACAO_MENU or t in {"/start", "/menu"}:
        return MENU_TEXTO, False

    # 3) opção do menu → status
    if t in OPCOES_STATUS or t_norm in OPCOES_STATUS:
        return None, True

    # 4) desconhecido
    return (
        "🤔 Não entendi.\n\n"
        "Envie *oi* para ver o menu ou *status* para receber o relatório.",
        False,
    )


# ---------- MODO BOT ----------
def loop_bot(token: str, chat_id_padrao: str) -> None:
    # Consome updates pendentes para não responder mensagens antigas no boot
    try:
        pendentes = get_updates(token, offset=-1, timeout=0)
        offset = (pendentes[-1]["update_id"] + 1) if pendentes else None
    except Exception:
        offset = None

    print("🤖 Bot iniciado. Envie 'oi' no Telegram para testar.")
    print("   Ctrl+C para encerrar.\n")

    while True:
        try:
            updates = get_updates(token, offset=offset, timeout=25)
        except KeyboardInterrupt:
            print("\n👋 Encerrando bot.")
            return
        except Exception as e:
            print(f"❌ Erro no loop: {e}")
            time.sleep(3)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            texto = msg.get("text", "") or ""
            nome = msg.get("from", {}).get("first_name", "?")
            print(f"📩 [{nome} | chat {chat_id}] {texto!r}")

            # (Opcional) Restringe apenas ao chat do config.ini:
            # if chat_id != chat_id_padrao:
            #     print(f"   ↳ ignorado (chat não autorizado)")
            #     continue

            resposta, precisa_status = processar_mensagem(texto)
            if precisa_status:
                try:
                    resposta = gerar_relatorio()
                except Exception as e:
                    resposta = f"❌ Erro ao gerar relatório: {e}"

            if resposta:
                enviar_mensagem(resposta, token, chat_id)


# ---------- MAIN ----------
def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor de status + bot Telegram")
    parser.add_argument(
        "--bot", action="store_true",
        help="Inicia modo bot (escuta mensagens do Telegram)",
    )
    args = parser.parse_args()

    try:
        token, chat_id = carregar_credenciais()
    except Exception as e:
        print(f"❌ Erro ao carregar config.ini: {e}")
        return

    if args.bot:
        loop_bot(token, chat_id)
    else:
        relatorio = gerar_relatorio()
        print(relatorio)
        enviar_mensagem(relatorio, token, chat_id)


if __name__ == "__main__":
    main()