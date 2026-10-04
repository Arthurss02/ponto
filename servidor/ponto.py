#!/usr/bin/env python3
"""Ponto: cronometro de semana de trabalho, com app pro celular e painel pro PC.

    python3 servidor/ponto.py          ->  http://localhost:8095/      (painel)
                                           http://localhost:8095/app   (celular)

So biblioteca padrao do Python (3.9+): nada pra instalar. Um arquivo SQLite
guarda tudo. Configuracao por variavel de ambiente:

    PONTO_PORTA   porta (padrao 8095)
    PONTO_HOST    endereco de escuta (padrao 0.0.0.0 = a rede toda)
    PONTO_BANCO   caminho do banco (padrao dados/ponto.db ao lado do projeto)
    PONTO_SENHA   se definida, pede senha. OBRIGATORIA numa VPS/internet.
    TZ            fuso: a semana vira na segunda 00:00 DESTE fuso

    Enviar pra um Ponto central (desligado se vazio):
    PONTO_ENVIAR_PARA   ex.: https://ponto.seudominio.com
    PONTO_ENVIAR_SENHA  a PONTO_SENHA do central
    PONTO_ENVIAR_A_CADA segundos entre envios (padrao 300)
    PONTO_NOME          nome desta maquina no central (padrao: hostname)
    PONTO_ACEITAR_COMANDOS=1  deixa o central controlar esta maquina
                        (bater ponto, lancar/consertar/apagar turno, temas,
                        meta). So acoes do Ponto - nunca comando de sistema.

Tudo isso tambem pode ir num arquivo ponto.env na raiz do projeto
(CHAVE=valor por linha); variavel de ambiente de verdade ganha do arquivo.

Regras que valem estar escritas:
 - a semana comeca na SEGUNDA 00:00. Domingo a noite ainda e a semana velha.
 - um turno conta inteiro no dia em que COMECOU: 22h-02h e um turno de 4h no
   dia em que comecou, nao "2h num dia e 2h no outro".
 - o toque do celular nunca se perde: sem rede o app guarda a batida com a
   hora do toque e manda depois; o servidor aceita batida atrasada (ate uma
   semana) e sabe nao fechar um turno que comecou depois dela.
"""
import contextlib
import csv
import datetime
import hashlib
import hmac
import io
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def le_arquivo_env(caminho):
    """ponto.env: chegar numa maquina nova, clonar, copiar este arquivo e
    rodar. So preenche o que o ambiente ainda nao tem."""
    if not caminho or not os.path.isfile(caminho):
        return
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k.startswith("PONTO_") or k == "TZ":
                os.environ.setdefault(k, v)
    if os.environ.get("TZ") and hasattr(time, "tzset"):
        time.tzset()


le_arquivo_env(os.environ.get("PONTO_ENV", os.path.join(RAIZ, "ponto.env")))
SITE = os.path.join(RAIZ, "web")
BANCO = os.environ.get("PONTO_BANCO") or os.path.join(RAIZ, "dados", "ponto.db")
PORTA = int(os.environ.get("PONTO_PORTA") or 8095)
HOST = os.environ.get("PONTO_HOST") or "0.0.0.0"
SENHA = os.environ.get("PONTO_SENHA") or ""
DESTINO = (os.environ.get("PONTO_ENVIAR_PARA") or "").strip().rstrip("/")
DESTINO_SENHA = os.environ.get("PONTO_ENVIAR_SENHA") or ""
ENVIAR_A_CADA = max(30, int(os.environ.get("PONTO_ENVIAR_A_CADA") or 300))
NOME = (os.environ.get("PONTO_NOME") or socket.gethostname().split(".")[0] or "ponto")[:40]
ACEITA_COMANDOS = (os.environ.get("PONTO_ACEITAR_COMANDOS") or "").strip().lower() in (
    "1", "sim", "s", "true", "yes")

META_PADRAO = 40.0            # horas por semana; cada um muda no painel
TURNO_ESQUECIDO = 16 * 3600   # aberto mais que isso e esquecimento, nao jornada
TETO_DIA = 14.0               # acima disso a meta do dia virou cobranca, nao alvo
PONTO_ATRASO_MAX = 7 * 86400  # batida guardada sem sinal vale ate uma semana
MAX_TEMAS = 6
TEMAS_PADRAO = ["Trabalho", "Estudo"]

_trava = threading.Lock()


# ------------------------------------------------------------------ banco

@contextlib.contextmanager
def conexao():
    """Uma conexao por operacao, uma operacao por vez. E um app de uma pessoa:
    serializar tudo custa nada e tira qualquer corrida entre dois toques."""
    with _trava:
        c = sqlite3.connect(BANCO, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


def cria_banco():
    pasta = os.path.dirname(BANCO)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    with conexao() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS turnos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inicio INTEGER NOT NULL,
            fim INTEGER,
            obs TEXT DEFAULT '',
            tema TEXT DEFAULT '')""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_turnos_inicio ON turnos(inicio)")
        # turno que veio de outra maquina: origem = nome dela, id_origem = id
        # la. Os daqui tem origem ''. So a maquina de origem conserta o turno.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(turnos)")]
        if "origem" not in cols:
            c.execute("ALTER TABLE turnos ADD COLUMN origem TEXT NOT NULL DEFAULT ''")
        if "id_origem" not in cols:
            c.execute("ALTER TABLE turnos ADD COLUMN id_origem INTEGER")
        # lado central: as maquinas que mandam pra ca e a fila de comandos
        c.execute("""CREATE TABLE IF NOT EXISTS maquinas (
            nome TEXT PRIMARY KEY,
            ultimo_envio INTEGER,
            ultimo_pedido INTEGER,
            estado TEXT DEFAULT '{}')""")
        c.execute("""CREATE TABLE IF NOT EXISTS comandos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origem TEXT NOT NULL,
            acao TEXT NOT NULL,
            dados TEXT NOT NULL DEFAULT '{}',
            criado_em INTEGER NOT NULL,
            entregue_em INTEGER,
            feito_em INTEGER,
            ok INTEGER,
            resultado TEXT DEFAULT '')""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_comandos_origem ON comandos(origem, feito_em)")
        # lado da maquina: o que ja foi executado, pra resposta perdida nao
        # virar comando executado duas vezes
        c.execute("""CREATE TABLE IF NOT EXISTS comandos_feitos (
            id_central INTEGER PRIMARY KEY,
            resposta TEXT NOT NULL,
            quando INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ajustes (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL)""")


def ajuste(c, chave, padrao=None):
    r = c.execute("SELECT valor FROM ajustes WHERE chave=?", (chave,)).fetchone()
    return r["valor"] if r else padrao


def grava_ajuste(c, chave, valor):
    c.execute("INSERT INTO ajustes (chave,valor) VALUES (?,?)"
              " ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor", (chave, valor))


# ------------------------------------------------------------------ temas e meta

def limpa_nome(x):
    return str(x if x is not None else "").strip()[:28]


def temas(c):
    bruto = ajuste(c, "temas")
    if bruto:
        try:
            lista = [limpa_nome(x) for x in json.loads(bruto) if limpa_nome(x)]
            if lista:
                return lista[:MAX_TEMAS]
        except (TypeError, ValueError):
            pass
    return list(TEMAS_PADRAO)


def temas_fora(c):
    """Temas que a pessoa mede mas nao quer somando na meta (transporte, por
    exemplo): ver pra onde o tempo vai sem que isso vire hora trabalhada."""
    bruto = ajuste(c, "temas_fora")
    if bruto:
        try:
            return [limpa_nome(x) for x in json.loads(bruto) if limpa_nome(x)]
        except (TypeError, ValueError):
            pass
    return []


def grava_fora(c, lista):
    # so tema que existe: senao um tema renomeado ficava fora pra sempre,
    # invisivel, e voltaria a nao contar se alguem recriasse o nome
    validos, limpos = temas(c), []
    for x in (lista or []):
        n = limpa_nome(x)
        if n and n in validos and n not in limpos:
            limpos.append(n)
    grava_ajuste(c, "temas_fora", json.dumps(limpos, ensure_ascii=False))
    return limpos


def muda_temas_fora(lista):
    with conexao() as c:
        return grava_fora(c, lista)


def muda_temas(lista):
    limpos = []
    for x in (lista or []):
        n = limpa_nome(x)
        if n and n not in limpos:
            limpos.append(n)
    limpos = limpos[:MAX_TEMAS] or list(TEMAS_PADRAO)
    with conexao() as c:
        grava_ajuste(c, "temas", json.dumps(limpos, ensure_ascii=False))
        grava_fora(c, temas_fora(c))   # tema que saiu da lista sai do "nao conta"
    return limpos


def muda_meta(horas):
    h = max(1.0, min(168.0, float(horas)))
    with conexao() as c:
        grava_ajuste(c, "meta_semana", str(h))
    return h


# ------------------------------------------------------------------ batidas

def inicio_da_semana(quando=None):
    d = datetime.datetime.fromtimestamp(quando or time.time())
    seg = d - datetime.timedelta(days=d.weekday())
    return int(seg.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def turno_aberto(c):
    # so o daqui: o cronometro que esta rodando em outra maquina nao e este
    return c.execute(
        "SELECT * FROM turnos WHERE fim IS NULL AND origem='' ORDER BY inicio DESC LIMIT 1").fetchone()


def hora_da_batida(quando, agora):
    """Futuro vira agora (relogio adiantado); mais velha que uma semana tambem
    - ai ja nao e sinal ruim, e relogio errado."""
    if quando is None:
        return agora
    try:
        q = int(quando)
    except (TypeError, ValueError):
        return agora
    if q > agora or q < agora - PONTO_ATRASO_MAX:
        return agora
    return q


def bate_ponto(acao, tema=None, quando=None):
    """Liga e desliga o cronometro. Idempotente de proposito: bater duas vezes
    no mesmo sentido nao cria turno fantasma nem fecha turno ja fechado.

    acao: "entra" | "sai" | "alterna" (um botao so - atalho do celular: ele
    decide) | "troca" (fecha o tema atual e abre outro no mesmo instante, sem
    buraco nem sobreposicao).
    """
    agora = int(time.time())
    t_bat = hora_da_batida(quando, agora)
    pedido = limpa_nome(tema) if tema else ""
    with conexao() as c:
        aberto = turno_aberto(c)
        lista = temas(c)
        if pedido and pedido not in lista:
            pedido = ""
        if acao == "alterna":
            acao = "sai" if aberto else "entra"
            if acao == "entra" and not pedido:
                # sem dizer o tema, volta pro ultimo usado: quase sempre e o
                # que a pessoa vai fazer de novo
                ult = c.execute("SELECT tema FROM turnos WHERE tema IS NOT NULL AND tema<>''"
                                " ORDER BY inicio DESC LIMIT 1").fetchone()
                if ult and ult["tema"] in lista:
                    pedido = ult["tema"]
        if acao == "troca":
            if aberto:
                if t_bat < aberto["inicio"]:
                    # troca guardada offline ANTES do turno aberto comecar
                    # (bateu em outro aparelho): aplicar fecharia o turno
                    # novo no passado
                    return {"ok": True, "rodando": True, "ja_estava": True,
                            "tema": aberto["tema"] or "", "desde": aberto["inicio"]}
                if (aberto["tema"] or "") == pedido:
                    return {"ok": True, "rodando": True, "ja_estava": True,
                            "tema": pedido, "desde": aberto["inicio"]}
                c.execute("UPDATE turnos SET fim=? WHERE id=?", (t_bat, aberto["id"]))
                aberto = None
            acao = "entra"
        if acao == "entra":
            if aberto:
                return {"ok": True, "rodando": True, "ja_estava": True,
                        "tema": aberto["tema"] or "", "desde": aberto["inicio"]}
            if t_bat < agora:
                # batida atrasada nao pode comecar dentro de um turno que ja
                # fechou depois dela: seria hora contada duas vezes
                r = c.execute("SELECT MAX(fim) AS f FROM turnos"
                              " WHERE fim IS NOT NULL AND fim > ? AND origem=''",
                              (t_bat,)).fetchone()
                if r and r["f"]:
                    t_bat = min(max(t_bat, r["f"]), agora)
            t = pedido or lista[0]
            c.execute("INSERT INTO turnos (inicio,tema) VALUES (?,?)", (t_bat, t))
            return {"ok": True, "rodando": True, "tema": t, "desde": t_bat}
        if acao != "sai":
            return {"ok": False, "erro": "acao invalida"}
        if not aberto:
            return {"ok": True, "rodando": False, "ja_estava": True}
        if t_bat < aberto["inicio"]:
            # "parei" guardado offline antes deste turno comecar: nao e dele
            return {"ok": True, "rodando": True, "ja_estava": True,
                    "tema": aberto["tema"] or "", "desde": aberto["inicio"]}
        c.execute("UPDATE turnos SET fim=? WHERE id=?", (t_bat, aberto["id"]))
        return {"ok": True, "rodando": False, "durou": t_bat - aberto["inicio"],
                "tema": aberto["tema"] or ""}


def hm_txt(h):
    m = int(round(max(0.0, h) * 60))
    return ("%dh%02d" % (m // 60, m % 60)) if m >= 60 else ("%dmin" % m)


def texto_ponto(r, s):
    """Uma linha so, pra notificacao do atalho do celular: aperta o botao e
    precisa saber o que aconteceu sem abrir nada."""
    sem = "semana %s de %dh" % (hm_txt(s["horas"]), round(s["meta"]))
    if r.get("rodando"):
        desde = datetime.datetime.fromtimestamp(
            r.get("desde") or s.get("desde") or time.time()).strftime("%H:%M")
        tema = r.get("tema") or s.get("tema_atual") or ""
        if r.get("ja_estava"):
            return "Já estava rodando: %s desde %s" % (tema, desde)
        return "Começou %s às %s · %s" % (tema, desde, sem)
    if r.get("ja_estava"):
        return "Já estava parado · " + sem
    txt = "Parou · %s de %s" % (hm_txt(r.get("durou", 0) / 3600.0), r.get("tema") or "")
    if s.get("meta_hoje_ok") and s.get("meta_hoje", 0) > 0.05:
        if s.get("pct_hoje", 0) >= 100:
            txt += " · meta de hoje batida, pode descansar"
        else:
            txt += " · hoje %s de %s (%d%%)" % (
                hm_txt(s["hoje"]), hm_txt(s["meta_hoje"]), round(s["pct_hoje"]))
    return txt


# ------------------------------------------------------------------ turnos a mao

def _inteiro(v):
    if v in (None, "", 0, "0"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def cria_turno(d):
    """Turno lancado a mao: todo mundo esquece de bater, e um cronometro em que
    o dia esquecido some pra sempre vira um cronometro em que ninguem confia."""
    ini = _inteiro(d.get("inicio"))
    if not ini:
        return None
    fim = _inteiro(d.get("fim"))
    if fim is not None and fim < ini:
        fim = ini
    with conexao() as c:
        if fim is None and turno_aberto(c):
            fim = ini    # dois cronometros rodando ao mesmo tempo nao existe
        cur = c.execute("INSERT INTO turnos (inicio,fim,obs,tema) VALUES (?,?,?,?)",
                        (ini, fim, str(d.get("obs") or "")[:120],
                         limpa_nome(d.get("tema")) or temas(c)[0]))
        return cur.lastrowid


def muda_turno(tid, campos):
    """Conserta o turno esquecido aberto (ou que comecou errado)."""
    sets, vals = [], []
    for k in ("inicio", "fim"):
        if k in campos:
            v = campos[k]
            if k == "inicio" and _inteiro(v) is None:
                continue
            sets.append(k + "=?")
            vals.append(_inteiro(v))
    if "obs" in campos:
        sets.append("obs=?")
        vals.append(str(campos["obs"] or "")[:120])
    if "tema" in campos:
        sets.append("tema=?")
        vals.append(limpa_nome(campos["tema"]))
    if not sets:
        return None
    with conexao() as c:
        if c.execute("SELECT 1 FROM turnos WHERE id=? AND origem<>''", (tid,)).fetchone():
            return None   # veio de outra maquina: o proximo envio desfaria o conserto
        c.execute("UPDATE turnos SET " + ",".join(sets) + " WHERE id=?", vals + [tid])
        r = c.execute("SELECT * FROM turnos WHERE id=?", (tid,)).fetchone()
        # fim antes do inicio viraria hora negativa na semana inteira
        if r and r["fim"] is not None and r["fim"] < r["inicio"]:
            c.execute("UPDATE turnos SET fim=? WHERE id=?", (r["inicio"], tid))
            r = c.execute("SELECT * FROM turnos WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def apaga_turno(tid):
    with conexao() as c:
        return c.execute("DELETE FROM turnos WHERE id=? AND origem=''", (tid,)).rowcount > 0


def turnos_da_semana(ini):
    ini = inicio_da_semana(ini)
    with conexao() as c:
        linhas = [dict(r) for r in c.execute(
            "SELECT id,inicio,fim,obs,tema,origem FROM turnos"
            " WHERE inicio>=? AND inicio<? ORDER BY inicio", (ini, inicio_da_semana(ini + 8 * 86400)))]
    return {"inicio_semana": ini, "turnos": linhas}


def exporta_csv():
    with conexao() as c:
        linhas = list(c.execute("SELECT id,inicio,fim,tema,obs,origem FROM turnos ORDER BY inicio"))
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["id", "inicio", "fim", "horas", "tema", "obs", "maquina"])
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""
    for r in linhas:
        horas = "" if r["fim"] is None else "%.2f" % ((r["fim"] - r["inicio"]) / 3600.0)
        w.writerow([r["id"], fmt(r["inicio"]), fmt(r["fim"]), horas, r["tema"], r["obs"],
                    r["origem"] or NOME])
    return s.getvalue()


# ------------------------------------------------------------------ varias maquinas

JANELA_ENVIO = 35 * 86400   # quanto pra tras cada envio regrava (consertos e apagados)
_acorda_envio = threading.Event()
ENVIO = {"destino": DESTINO, "nome": NOME, "ultimo_ok": None, "erro": None}


def receber(d):
    """O lado central. Uma maquina manda os turnos DELA a partir de 'desde';
    aqui os daquela origem a partir de 'desde' sao trocados pelo que chegou.
    Trocar em vez de somar e o que faz conserto e turno apagado la chegarem
    aqui, e mandar a mesma coisa duas vezes nao duplicar nada."""
    origem = str(d.get("origem") or "").strip()[:40]
    if not origem:
        raise ValueError("falta origem")
    try:
        desde = max(0, int(d.get("desde") or 0))
    except (TypeError, ValueError):
        raise ValueError("desde invalido")
    turnos = d.get("turnos")
    if not isinstance(turnos, list) or len(turnos) > 20000:
        raise ValueError("turnos invalidos")
    limpos = []
    for t in turnos:
        if not isinstance(t, dict):
            continue
        ini, fim = _inteiro(t.get("inicio")), _inteiro(t.get("fim"))
        if not ini or ini < desde:
            continue
        if fim is not None and fim < ini:
            fim = ini
        limpos.append((ini, fim, str(t.get("obs") or "")[:120], limpa_nome(t.get("tema")),
                       origem, _inteiro(t.get("id"))))
    estado = d.get("estado") if isinstance(d.get("estado"), dict) else {}
    with conexao() as c:
        c.execute("DELETE FROM turnos WHERE origem=? AND inicio>=?", (origem, desde))
        c.executemany("INSERT INTO turnos (inicio,fim,obs,tema,origem,id_origem)"
                      " VALUES (?,?,?,?,?,?)", limpos)
        c.execute("INSERT INTO maquinas (nome,ultimo_envio,estado) VALUES (?,?,?)"
                  " ON CONFLICT(nome) DO UPDATE SET ultimo_envio=excluded.ultimo_envio,"
                  " estado=excluded.estado",
                  (origem, int(time.time()), json.dumps(estado, ensure_ascii=False)[:4000]))
    return {"ok": True, "origem": origem, "recebidos": len(limpos)}


def pede_central(metodo, caminho, corpo=None, timeout=20):
    cab = {"User-Agent": "Ponto/1"}
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo).encode("utf-8")
        cab["Content-Type"] = "application/json"
    if DESTINO_SENHA:
        cab["Authorization"] = "Bearer " + DESTINO_SENHA
    req = urllib.request.Request(DESTINO + caminho, data=dados, headers=cab, method=metodo)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        bruto = r.read()
    return json.loads(bruto.decode("utf-8") or "{}") if bruto else {}


def envia_uma_vez():
    """O lado de quem viaja: manda os turnos DAQUI (nunca os recebidos, senao
    duas maquinas ficariam se reenviando). A primeira vez pra um destino vai
    tudo; depois, so as ultimas 5 semanas."""
    with conexao() as c:
        completo = ajuste(c, "envio_completo") != DESTINO
        desde = 0 if completo else int(time.time()) - JANELA_ENVIO
        linhas = [dict(r) for r in c.execute(
            "SELECT id,inicio,fim,tema,obs FROM turnos WHERE origem='' AND inicio>=?"
            " ORDER BY inicio", (desde,))]
        ab = turno_aberto(c)
        estado = {"rodando": bool(ab), "tema": (ab["tema"] if ab else ""),
                  "desde": (ab["inicio"] if ab else None), "temas": temas(c),
                  "aceita_comandos": ACEITA_COMANDOS}
    pede_central("POST", "/api/receber",
                 {"origem": NOME, "desde": desde, "turnos": linhas, "estado": estado})
    if completo:
        with conexao() as c:
            grava_ajuste(c, "envio_completo", DESTINO)
    return len(linhas)


def laco_envio():
    while True:
        try:
            n = envia_uma_vez()
            ENVIO["ultimo_ok"], ENVIO["erro"] = int(time.time()), None
            if os.environ.get("PONTO_LOG"):
                print("enviado: %d turnos pra %s" % (n, DESTINO), flush=True)
        except urllib.error.HTTPError as e:
            ENVIO["erro"] = "o central respondeu %d%s" % (
                e.code, " (senha errada?)" if e.code == 401 else "")
        except Exception as e:   # sem rede, DNS, timeout: tenta de novo depois
            ENVIO["erro"] = str(getattr(e, "reason", None) or e)[:160]
        if ENVIO["erro"]:
            print("envio falhou: %s" % ENVIO["erro"], file=sys.stderr, flush=True)
        _acorda_envio.wait(ENVIAR_A_CADA)
        if _acorda_envio.is_set():
            _acorda_envio.clear()
            time.sleep(3)   # junta "parou" + "comecou" de uma troca num envio so


# ------------------------------------------------------------------ comandos do central
#
# O central nao alcanca a maquina (ela esta atras de roteador, 4G, rede de
# outro lugar), entao e a maquina que pergunta: um GET que fica pendurado ate
# 25 s no central e volta na hora em que chega um comando. Na pratica o botao
# no painel do central bate o ponto da outra maquina em 1-2 segundos.
#
# A lista do que pode ser pedido e FECHADA e so mexe no Ponto. Nao existe
# "rode este comando no sistema": quem invadisse o central teria todas as
# maquinas na mao.

COMANDOS = ("ponto", "lanca_turno", "muda_turno", "apaga_turno", "temas", "temas_fora", "meta")
COMANDO_VALIDADE = 7 * 86400   # comando que a maquina nao buscou em 7 dias caduca
ESPERA_MAX = 25
_chegou_comando = threading.Condition()
COMANDOS_ESTADO = {"conectado": False, "erro": None, "ultimo": None}


def enfileira_comando(origem, acao, dados):
    origem = str(origem or "").strip()[:40]
    if not origem:
        raise ValueError("falta a maquina (origem)")
    if acao not in COMANDOS:
        raise ValueError("comando desconhecido: use " + ", ".join(COMANDOS))
    if not isinstance(dados, dict):
        dados = {}
    with conexao() as c:
        cid = c.execute("INSERT INTO comandos (origem,acao,dados,criado_em) VALUES (?,?,?,?)",
                        (origem, acao, json.dumps(dados, ensure_ascii=False)[:8000],
                         int(time.time()))).lastrowid
    with _chegou_comando:
        _chegou_comando.notify_all()
    return cid


def _pendentes(c, origem):
    agora = int(time.time())
    linhas = [dict(r) for r in c.execute(
        "SELECT id,acao,dados,criado_em FROM comandos WHERE origem=? AND feito_em IS NULL"
        " AND criado_em>? ORDER BY id LIMIT 50", (origem, agora - COMANDO_VALIDADE))]
    for l in linhas:
        try:
            l["dados"] = json.loads(l["dados"] or "{}")
        except ValueError:
            l["dados"] = {}
    return linhas


def busca_comandos(origem, espera):
    """GET pendurado: devolve na hora se ja tem comando, senao espera ate
    'espera' segundos acordando quando alguem enfileirar."""
    origem = str(origem or "").strip()[:40]
    if not origem:
        raise ValueError("falta origem")
    fim = time.time() + max(0, min(ESPERA_MAX, espera))
    while True:
        with conexao() as c:
            c.execute("INSERT INTO maquinas (nome,ultimo_pedido) VALUES (?,?)"
                      " ON CONFLICT(nome) DO UPDATE SET ultimo_pedido=excluded.ultimo_pedido",
                      (origem, int(time.time())))
            p = _pendentes(c, origem)
            if p:
                c.execute("UPDATE comandos SET entregue_em=? WHERE id IN (%s) AND entregue_em IS NULL"
                          % ",".join("?" * len(p)), [int(time.time())] + [x["id"] for x in p])
        falta = fim - time.time()
        if p or falta <= 0:
            return p
        with _chegou_comando:
            _chegou_comando.wait(timeout=min(falta, 5))


def registra_resultados(origem, resultados):
    origem = str(origem or "").strip()[:40]
    n = 0
    with conexao() as c:
        for r in (resultados or [])[:100]:
            if not isinstance(r, dict) or _inteiro(r.get("id")) is None:
                continue
            n += c.execute(
                "UPDATE comandos SET feito_em=?, ok=?, resultado=? WHERE id=? AND origem=?",
                (int(time.time()), 1 if r.get("ok") else 0,
                 str(r.get("texto") or r.get("erro") or "")[:300], int(r["id"]), origem)).rowcount
    return n


def maquinas():
    agora = int(time.time())
    with conexao() as c:
        lista = []
        for m in c.execute("SELECT * FROM maquinas ORDER BY nome"):
            m = dict(m)
            try:
                m["estado"] = json.loads(m.get("estado") or "{}")
            except ValueError:
                m["estado"] = {}
            # perguntou por comando no ultimo minuto = esta ouvindo agora
            m["ouvindo"] = bool(m.get("ultimo_pedido") and agora - m["ultimo_pedido"] < 75)
            m["comandos"] = [dict(r) for r in c.execute(
                "SELECT id,acao,dados,criado_em,entregue_em,feito_em,ok,resultado FROM comandos"
                " WHERE origem=? ORDER BY id DESC LIMIT 8", (m["nome"],))]
            lista.append(m)
    return lista


def executa_comando(cmd):
    """O lado da maquina. So o que esta na lista, cada um com o mesmo codigo
    que o painel daqui usaria."""
    acao, d = cmd.get("acao"), cmd.get("dados") or {}
    if not isinstance(d, dict):
        d = {}
    try:
        if acao == "ponto":
            qual = d.get("acao") or "alterna"
            if qual not in ("entra", "sai", "alterna", "troca"):
                return {"ok": False, "erro": "acao de ponto invalida"}
            # a hora e a do clique no central: comando que esperou a maquina
            # ligar nao comeca o turno no momento em que ela ligou
            r = bate_ponto(qual, d.get("tema"), cmd.get("criado_em"))
            return {"ok": bool(r.get("ok")), "texto": texto_ponto(r, semana())}
        if acao == "lanca_turno":
            tid = cria_turno(d)
            return {"ok": bool(tid), "texto": "turno lan\u00e7ado" if tid else "precisa de inicio"}
        if acao == "muda_turno":
            campos = {k: d[k] for k in ("inicio", "fim", "tema", "obs") if k in d}
            t = muda_turno(_inteiro(d.get("id")) or 0, campos)
            return {"ok": bool(t), "texto": "turno consertado" if t else "turno n\u00e3o encontrado"}
        if acao == "apaga_turno":
            ok = apaga_turno(_inteiro(d.get("id")) or 0)
            return {"ok": ok, "texto": "turno apagado" if ok else "turno n\u00e3o encontrado"}
        if acao == "temas":
            return {"ok": True, "texto": "temas: " + ", ".join(muda_temas(d.get("temas")))}
        if acao == "temas_fora":
            return {"ok": True, "texto": "n\u00e3o contam: " + (", ".join(muda_temas_fora(d.get("temas"))) or "nenhum")}
        if acao == "meta":
            return {"ok": True, "texto": "meta %s por semana" % hm_txt(muda_meta(d.get("horas")))}
    except (TypeError, ValueError) as e:
        return {"ok": False, "erro": "dados invalidos: %s" % e}
    return {"ok": False, "erro": "comando desconhecido"}


def atende_comandos(lista):
    """Executa cada comando uma vez so, mesmo que o central mande de novo
    (a resposta anterior pode ter se perdido no caminho)."""
    respostas = []
    for cmd in lista:
        cid = _inteiro(cmd.get("id"))
        if cid is None:
            continue
        with conexao() as c:
            ja = c.execute("SELECT resposta FROM comandos_feitos WHERE id_central=?", (cid,)).fetchone()
        if ja:
            r = json.loads(ja["resposta"])
        else:
            r = executa_comando(cmd)
            with conexao() as c:
                c.execute("INSERT OR REPLACE INTO comandos_feitos (id_central,resposta,quando)"
                          " VALUES (?,?,?)", (cid, json.dumps(r, ensure_ascii=False), int(time.time())))
                c.execute("DELETE FROM comandos_feitos WHERE quando<?",
                          (int(time.time()) - 30 * 86400,))
            print("comando do central: %s -> %s" % (cmd.get("acao"), r.get("texto") or r.get("erro")),
                  flush=True)
        r["id"] = cid
        respostas.append(r)
    return respostas


def laco_comandos():
    falhas = 0
    while True:
        try:
            r = pede_central("GET", "/api/comandos?origem=%s&espera=%d" % (quote(NOME), ESPERA_MAX),
                             timeout=ESPERA_MAX + 15)
            COMANDOS_ESTADO.update(conectado=True, erro=None)
            lista = r.get("comandos") or []
            if lista:
                respostas = atende_comandos(lista)
                pede_central("POST", "/api/comandos/resultado",
                             {"origem": NOME, "resultados": respostas})
                COMANDOS_ESTADO["ultimo"] = int(time.time())
                _acorda_envio.set()   # o central ve o efeito no proximo envio, ja
            falhas = 0
        except urllib.error.HTTPError as e:
            falhas += 1
            COMANDOS_ESTADO.update(conectado=False, erro="o central respondeu %d" % e.code)
        except Exception as e:
            falhas += 1
            COMANDOS_ESTADO.update(conectado=False, erro=str(getattr(e, "reason", None) or e)[:160])
        if falhas:
            time.sleep(min(60, 5 * falhas))


# ------------------------------------------------------------------ a conta da semana

def historico(n=8, quando=None):
    """As ultimas n semanas, a atual inclusa. "Estou me dedicando como de
    costume?" - e costume e o que a propria pessoa fez, nao um numero que
    alguem escolheu. Sem isso o cronometro so sabe cobrar."""
    agora = int(quando or time.time())
    ini_atual = inicio_da_semana(agora)
    desde = inicio_da_semana(ini_atual - (n - 1) * 7 * 86400 + 3600)
    with conexao() as c:
        linhas = [dict(r) for r in c.execute(
            "SELECT inicio,fim,tema FROM turnos WHERE inicio>=? ORDER BY inicio", (desde,))]
        fora = set(temas_fora(c))
    baldes = {}
    for t in linhas:
        if (t.get("tema") or "").strip() in fora:
            continue
        f = t["fim"] if t["fim"] is not None else agora
        b = baldes.setdefault(inicio_da_semana(t["inicio"]), {"h": 0.0, "d": set()})
        b["h"] += max(0, f - t["inicio"]) / 3600.0
        b["d"].add(datetime.datetime.fromtimestamp(t["inicio"]).date())
    saida, ini = [], desde
    for _ in range(n):
        b = baldes.get(ini, {"h": 0.0, "d": set()})
        saida.append({
            "inicio": ini,
            "rotulo": datetime.datetime.fromtimestamp(ini).strftime("%d/%m"),
            "horas": round(b["h"], 2),
            "dias": len(b["d"]),
            "atual": ini == ini_atual,
        })
        # +8 dias e volta pra segunda: semana de horario de verao tem 167/169h
        ini = inicio_da_semana(ini + 8 * 86400)
    return saida


def ordem_tema(lista, nome):
    try:
        return lista.index(nome)
    except ValueError:
        return len(lista)


def mediana(vals):
    if not vals:
        return 0.0
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2.0


def semana(quando=None):
    agora = int(quando or time.time())
    ini = inicio_da_semana(agora)
    fim_semana = inicio_da_semana(ini + 8 * 86400)
    with conexao() as c:
        try:
            meta = float(ajuste(c, "meta_semana", META_PADRAO))
        except (TypeError, ValueError):
            meta = META_PADRAO
        linhas = [dict(r) for r in c.execute(
            "SELECT id,inicio,fim,obs,tema,origem FROM turnos"
            " WHERE inicio>=? AND inicio<? ORDER BY inicio", (ini, fim_semana))]
        lista_temas = temas(c)
        fora = set(t for t in temas_fora(c) if t in lista_temas)
        ab = turno_aberto(c)
        aberto = dict(ab) if ab else None
        # o mesmo instante da semana passada: comparar o que foi feito ate
        # quarta 11h contra a semana passada INTEIRA seria comparacao torta
        corte = agora - 7 * 86400
        ini_passada = inicio_da_semana(ini - 3600)
        passada_ate_agora = 0
        passada_total = 0
        for r in c.execute(
                "SELECT inicio,fim,tema FROM turnos WHERE inicio>=? AND inicio<?",
                (ini_passada, ini)):
            if (r["tema"] or "").strip() in fora:
                continue
            f = r["fim"] if r["fim"] is not None else agora
            passada_total += max(0, f - r["inicio"])
            if r["inicio"] < corte:
                passada_ate_agora += max(0, min(f, corte) - r["inicio"])

    por_dia = [0.0] * 7
    # a barra do dia fatiada por tema: meia tarde de estudo e meia de trabalho
    # aparecem como duas metades, nao como um "6h" cego
    por_dia_tema = [{} for _ in range(7)]
    por_dia_fora = [0.0] * 7
    por_tema = {}
    total = total_fora = fechadas = fechadas_hoje = 0
    hoje_i = datetime.datetime.fromtimestamp(agora).weekday()
    for t in linhas:
        f = t["fim"] if t["fim"] is not None else agora
        dur = max(0, f - t["inicio"])
        dia = datetime.datetime.fromtimestamp(t["inicio"]).weekday()
        chave = (t.get("tema") or "").strip() or "sem tema"
        if chave in fora:
            total_fora += dur
            por_dia_fora[dia] += dur / 3600.0
        else:
            total += dur
            # turno aberto de outra maquina entra como fechado ate agora: a
            # tela so faz andar o cronometro DAQUI
            if t["fim"] is not None or t.get("origem"):
                fechadas += dur
                if dia == hoje_i:
                    fechadas_hoje += dur
            por_dia[dia] += dur / 3600.0
        por_tema[chave] = por_tema.get(chave, 0.0) + dur / 3600.0
        por_dia_tema[dia][chave] = por_dia_tema[dia].get(chave, 0.0) + dur / 3600.0

    dias_corridos = hoje_i + 1          # segunda=1 ... domingo=7, contando hoje
    faltam_dias = 6 - hoje_i            # dias INTEIROS depois de hoje
    horas = total / 3600.0
    hoje = por_dia[hoje_i]
    media_dia = horas / dias_corridos
    falta = max(0.0, meta - horas)
    # meta do dia: o que sobra da semana dividido pelos dias que ainda existem,
    # congelada no que estava feito ANTES de hoje. Se olhasse o que ja foi
    # feito hoje, andaria pra frente junto e nunca fecharia em 100%.
    antes_de_hoje = max(0.0, horas - hoje)
    meta_hoje = max(0.0, (meta - antes_de_hoje) / max(1, faltam_dias + 1))

    return {
        "meta": round(meta, 2),
        "horas": round(horas, 3),
        # so os turnos fechados: a tela soma o cronometro vivo em cima disso
        # sem contar duas vezes o turno que esta correndo agora
        "horas_fechadas": round(fechadas / 3600.0, 3),
        "hoje_fechadas": round(fechadas_hoje / 3600.0, 3),
        "hoje": round(hoje, 3),
        "por_dia": [round(x, 3) for x in por_dia],
        # na ordem da lista de temas, nao por tamanho: as cores nao dancam de
        # um dia pro outro. O que conta vem primeiro.
        "por_dia_tema": [
            sorted([{"tema": k, "horas": round(v, 4), "conta": k not in fora}
                    for k, v in d.items()],
                   key=lambda x: (0 if x["conta"] else 1,
                                  ordem_tema(lista_temas, x["tema"]), -x["horas"]))
            for d in por_dia_tema],
        "horas_fora": round(total_fora / 3600.0, 3),
        "hoje_fora": round(por_dia_fora[hoje_i], 3),
        "por_dia_fora": [round(x, 3) for x in por_dia_fora],
        "temas_fora": sorted(fora, key=lambda x: ordem_tema(lista_temas, x)),
        "dia_atual": hoje_i,
        "dias_corridos": dias_corridos,
        "dias_restantes": faltam_dias,
        "media_dia": round(media_dia, 3),
        "projecao_media": round(media_dia * 7, 2),          # mantendo a media
        "projecao_hoje": round(horas + hoje * faltam_dias, 2),  # todo dia igual a hoje
        "falta": round(falta, 2),
        "por_dia_pra_meta": round(falta / max(1, faltam_dias + 1), 2),
        "meta_hoje": round(meta_hoje, 2),
        "pct_hoje": (round(100 * hoje / meta_hoje, 1) if meta_hoje > 0.05 else 100.0),
        # ficou dias fora e a conta pede 18h/dia: mostrar isso so faz sentir
        # culpa. A tela esconde a porcentagem do dia nesse caso.
        "meta_hoje_ok": bool(meta_hoje <= TETO_DIA),
        "sobra_hoje": round(hoje - meta_hoje, 2),
        "pct": round(100 * horas / meta, 1) if meta else 0,
        "passada_ate_agora": round(passada_ate_agora / 3600.0, 2),
        "passada_total": round(passada_total / 3600.0, 2),
        # dedicacao e mais sentar todo dia do que somar hora: os dois numeros
        "dias_trabalhados": sum(1 for x in por_dia if x > 0),
        "mediana_dia": round(mediana(sorted(x for x in por_dia if x > 0)), 2),
        "rodando": bool(aberto),
        "desde": aberto["inicio"] if aberto else None,
        "turno_id": aberto["id"] if aberto else None,
        # aberto ha 19h nao e jornada, e "esqueci de bater a saida"
        "esquecido": bool(aberto and (agora - aberto["inicio"]) > TURNO_ESQUECIDO),
        "turnos": linhas,
        "temas": lista_temas,
        "por_tema": sorted(
            [{"tema": k, "horas": round(v, 2), "conta": k not in fora}
             for k, v in por_tema.items()],
            key=lambda x: (0 if x["conta"] else 1, -x["horas"])),
        "tema_atual": (aberto.get("tema") or "") if aberto else "",
        # sem isso o total ao vivo cresceria durante uma viagem de carro
        "tema_atual_conta": bool(
            aberto and (aberto.get("tema") or "").strip() not in fora) if aberto else True,
        "inicio_semana": ini,
        "historico": historico(8, agora),
    }


# ------------------------------------------------------------------ senha

COOKIE = "ponto"


def ficha():
    """Valor do cookie de sessao. Derivado da senha: trocar a PONTO_SENHA
    desloga todos os aparelhos de uma vez, sem tabela de sessoes."""
    return hmac.new(SENHA.encode(), b"ponto-sessao-v1", hashlib.sha256).hexdigest()


def le_cookie(cabecalho, nome):
    for parte in (cabecalho or "").split(";"):
        k, _, v = parte.strip().partition("=")
        if k == nome:
            return v
    return ""


def autorizado(h):
    if not SENHA:
        return True
    if hmac.compare_digest(le_cookie(h.headers.get("Cookie"), COOKIE), ficha()):
        return True
    # atalho do celular (iOS Atalhos, Tasker...) manda a senha no cabecalho
    auth = h.headers.get("Authorization") or ""
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), SENHA):
        return True
    return False


# ------------------------------------------------------------------ http

ESTATICOS = {
    # caminho -> (arquivo em web/, tipo). Lista fechada: nada fora dela sai.
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app": ("app.html", "text/html; charset=utf-8"),
    "/entrar": ("entrar.html", "text/html; charset=utf-8"),
    "/app.webmanifest": ("app.webmanifest", "application/manifest+json"),
    "/app-sw.js": ("app-sw.js", "text/javascript; charset=utf-8"),
    "/icone-180.png": ("icone-180.png", "image/png"),
    "/icone-192.png": ("icone-192.png", "image/png"),
    "/icone-512.png": ("icone-512.png", "image/png"),
    "/favicon.ico": ("icone-192.png", "image/png"),
}
# o que abre sem senha: a tela de entrar e o que o celular pede pra montar o
# icone na tela de inicio antes de ter o cookie
PUBLICOS = {"/entrar", "/app.webmanifest", "/icone-180.png", "/icone-192.png",
            "/icone-512.png", "/favicon.ico"}
CORPO_MAX = 64 * 1024


class Ponto(BaseHTTPRequestHandler):
    server_version = "Ponto/1"

    # ---- respostas
    def _envia(self, codigo, corpo, tipo, extra=None):
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _json(self, codigo, obj):
        self._envia(codigo, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _vai(self, destino, extra=None):
        h = {"Location": destino}
        h.update(extra or {})
        self._envia(303, b"", "text/plain", h)

    def _estatico(self, caminho):
        arq, tipo = ESTATICOS[caminho]
        try:
            with open(os.path.join(SITE, arq), "rb") as f:
                dados = f.read()
        except OSError:
            return self._json(404, {"erro": "nao achei " + arq})
        extra = {}
        if arq == "app-sw.js":
            extra["Service-Worker-Allowed"] = "./"
        self._envia(200, dados, tipo, extra)

    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > CORPO_MAX:
            raise ValueError("corpo grande demais")
        bruto = self.rfile.read(n)
        tipo = self.headers.get("Content-Type") or ""
        if "application/x-www-form-urlencoded" in tipo:
            return {k: v[0] for k, v in parse_qs(bruto.decode("utf-8", "replace")).items()}
        d = json.loads(bruto.decode("utf-8") or "{}")
        return d if isinstance(d, dict) else {}

    def _porta_fechada(self, u):
        """Devolve True se respondeu 'precisa de senha'."""
        if u.path in PUBLICOS or autorizado(self):
            return False
        if u.path.startswith("/api/"):
            self._json(401, {"erro": "precisa de senha", "entrar": "entrar"})
        else:
            volta = "app" if u.path == "/app" else ""
            self._vai("entrar" + ("?volta=app" if volta else ""))
        return True

    # ---- verbos
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/app/":
            # o app usa caminho relativo (api/semana): com a barra no fim
            # pediria /app/api/semana
            return self._vai("../app")
        if self._porta_fechada(u):
            return
        if u.path == "/sair":
            return self._vai("entrar", {"Set-Cookie": COOKIE + "=; Path=/; Max-Age=0"})
        if u.path in ESTATICOS:
            return self._estatico(u.path)
        q = parse_qs(u.query)
        if u.path == "/api/semana":
            s = semana()
            s["com_senha"] = bool(SENHA)   # o painel so mostra "Sair" se houver
            s["envio"] = (dict(ENVIO, comandos=ACEITA_COMANDOS, ouvindo=COMANDOS_ESTADO["conectado"])
                          if DESTINO else None)
            return self._json(200, s)
        if u.path == "/api/historico":
            n = max(1, min(104, _inteiro((q.get("n") or [""])[0]) or 12))
            return self._json(200, {"semanas": historico(n)})
        if u.path == "/api/turnos":
            ini = _inteiro((q.get("semana") or [""])[0]) or int(time.time())
            return self._json(200, turnos_da_semana(ini))
        if u.path == "/api/maquinas":
            return self._json(200, {"maquinas": maquinas()})
        if u.path == "/api/comandos":
            if not SENHA:
                return self._json(403, {"erro": "o central precisa de PONTO_SENHA"})
            try:
                espera = _inteiro((q.get("espera") or [""])[0]) or 0
                return self._json(200, {"comandos": busca_comandos(
                    (q.get("origem") or [""])[0], espera)})
            except ValueError as e:
                return self._json(400, {"erro": str(e)})
        if u.path == "/api/exportar.csv":
            nome = "ponto-%s.csv" % datetime.date.today().isoformat()
            return self._envia(200, exporta_csv(), "text/csv; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="%s"' % nome})
        return self._json(404, {"erro": "rota desconhecida"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            d = self._corpo()
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"erro": "corpo invalido"})
        if u.path == "/entrar":
            return self._entrar(d)
        if self._porta_fechada(u):
            return
        if u.path == "/api/ponto":
            acao = d.get("acao") or "alterna"
            if acao not in ("entra", "sai", "alterna", "troca"):
                return self._json(400, {"erro": "acao: entra, sai, alterna ou troca"})
            r = bate_ponto(acao, d.get("tema"), d.get("quando"))
            _acorda_envio.set()   # manda ja, nao espera o proximo ciclo
            r["semana"] = semana()
            r["texto"] = texto_ponto(r, r["semana"])
            return self._json(200, r)
        if u.path == "/api/receber":
            if not SENHA:
                # receber de qualquer um sem senha seria deixar a internet
                # escrever no seu ponto
                return self._json(403, {"erro": "o central precisa de PONTO_SENHA"})
            try:
                return self._json(200, receber(d))
            except ValueError as e:
                return self._json(400, {"erro": str(e)})
        if u.path == "/api/comandos":
            if not SENHA:
                return self._json(403, {"erro": "o central precisa de PONTO_SENHA"})
            try:
                cid = enfileira_comando(d.get("origem"), d.get("acao"), d.get("dados"))
            except ValueError as e:
                return self._json(400, {"erro": str(e)})
            return self._json(201, {"ok": True, "id": cid})
        if u.path == "/api/comandos/resultado":
            if not SENHA:
                return self._json(403, {"erro": "o central precisa de PONTO_SENHA"})
            return self._json(200, {"ok": True, "marcados": registra_resultados(
                d.get("origem"), d.get("resultados"))})
        if u.path == "/api/turnos":
            tid = cria_turno(d)
            if not tid:
                return self._json(400, {"erro": "precisa de inicio"})
            _acorda_envio.set()
            return self._json(201, {"ok": True, "id": tid, "semana": semana()})
        if u.path == "/api/temas":
            return self._json(200, {"ok": True, "temas": muda_temas(d.get("temas")),
                                    "semana": semana()})
        if u.path == "/api/temas-fora":
            return self._json(200, {"ok": True, "temas_fora": muda_temas_fora(d.get("temas")),
                                    "semana": semana()})
        if u.path == "/api/meta":
            try:
                h = muda_meta(d.get("horas") or META_PADRAO)
            except (TypeError, ValueError):
                return self._json(400, {"erro": "horas invalidas"})
            return self._json(200, {"ok": True, "meta": h, "semana": semana()})
        return self._json(404, {"erro": "rota desconhecida"})

    def _id_turno(self, u):
        partes = [p for p in u.path.split("/") if p]
        if len(partes) == 3 and partes[:2] == ["api", "turnos"]:
            return _inteiro(partes[2])
        return None

    def do_PATCH(self):
        u = urlparse(self.path)
        if self._porta_fechada(u):
            return
        tid = self._id_turno(u)
        if not tid:
            return self._json(404, {"erro": "rota desconhecida"})
        try:
            d = self._corpo()
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"erro": "corpo invalido"})
        t = muda_turno(tid, d)
        _acorda_envio.set()
        return self._json(200 if t else 400, {"ok": bool(t), "turno": t, "semana": semana(),
                                              "erro": None if t else "turno de outra maquina: conserte la"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if self._porta_fechada(u):
            return
        tid = self._id_turno(u)
        if not tid:
            return self._json(404, {"erro": "rota desconhecida"})
        if not apaga_turno(tid):
            return self._json(400, {"erro": "turno de outra maquina: apague la"})
        _acorda_envio.set()
        return self._json(200, {"ok": True, "semana": semana()})

    def _entrar(self, d):
        volta = "app" if d.get("volta") == "app" else "./"
        if not SENHA:
            return self._vai(volta)
        if not hmac.compare_digest(str(d.get("senha") or ""), SENHA):
            time.sleep(1.0)   # chute em serie fica lento sem estado nenhum
            return self._vai("entrar?erro=1" + ("&volta=app" if volta == "app" else ""))
        seguro = "; Secure" if (self.headers.get("X-Forwarded-Proto") == "https") else ""
        biscoito = "%s=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax%s" % (
            COOKIE, ficha(), seguro)
        return self._vai(volta, {"Set-Cookie": biscoito})

    def log_message(self, formato, *args):
        if os.environ.get("PONTO_LOG"):
            sys.stderr.write("%s %s\n" % (self.address_string(), formato % args))


def main():
    cria_banco()
    srv = ThreadingHTTPServer((HOST, PORTA), Ponto)
    srv.daemon_threads = True
    aberto_pra_rede = HOST not in ("127.0.0.1", "localhost", "::1")
    print("Ponto rodando em http://%s:%d/  (celular: /app)" % (
        "localhost" if not aberto_pra_rede else HOST, PORTA), flush=True)
    print("Banco: %s" % BANCO, flush=True)
    if not SENHA and aberto_pra_rede:
        print("AVISO: sem PONTO_SENHA e escutando na rede. Em casa/Tailscale tudo bem;"
              " numa VPS defina PONTO_SENHA e ponha HTTPS na frente.", flush=True)
    if DESTINO:
        if DESTINO.startswith("http://") and not any(
                h in DESTINO for h in ("localhost", "127.0.0.1", "192.168.", "10.", "100.")):
            print("AVISO: enviando por http pra internet - a senha vai aberta. Use https.",
                  flush=True)
        print("Enviando pra %s a cada %ds como '%s'" % (DESTINO, ENVIAR_A_CADA, NOME), flush=True)
        threading.Thread(target=laco_envio, daemon=True).start()
        if ACEITA_COMANDOS:
            print("Aceitando comandos de %s (so acoes do Ponto)" % DESTINO, flush=True)
            threading.Thread(target=laco_comandos, daemon=True).start()
    elif ACEITA_COMANDOS:
        print("AVISO: PONTO_ACEITAR_COMANDOS sem PONTO_ENVIAR_PARA - nao ha de quem receber.",
              flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
