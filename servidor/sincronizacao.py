"""Sincronizacao: liga este Ponto a um Ponto central.

Este arquivo e OPCIONAL e quase independente do resto. Apague-o e o Ponto vira
um cronometro de uma maquina so, sem central, sem envio, sem controle remoto -
o `servidor/ponto.py` nao fala de central em lugar nenhum. Enquanto ele existe,
se registra no nucleo (`instalar`) e acrescenta:

  - o ENVIO dos turnos desta maquina pra um central, de tempos em tempos;
  - o RECEBER, pro central juntar os turnos de varias maquinas;
  - os COMANDOS, pro central controlar esta maquina (so acoes do Ponto).

Configuracao (por variavel de ambiente ou pelo ponto.env):

    PONTO_ENVIAR_PARA        ex.: https://ponto.seudominio.com (vazio = nao envia)
    PONTO_ENVIAR_SENHA       a PONTO_SENHA do central
    PONTO_ENVIAR_A_CADA      segundos entre envios (padrao 300, minimo 30)
    PONTO_NOME               nome desta maquina no central (padrao: hostname)
    PONTO_ACEITAR_COMANDOS=1 deixa o central controlar esta maquina

TODA a seguranca do lado central mora aqui (procure por "SEGURANCA:"):
 - O SERVIDOR NAO EXECUTA SHELL: a lista COMANDOS e fechada e so mexe no Ponto;
   nao ha os.system/subprocess/eval/exec neste arquivo nem no nucleo.
 - o central nao aceita receber turno nem comando sem PONTO_SENHA;
 - tamanho de tudo que chega e limitado (turnos, estado, dados de comando);
 - cada comando roda uma vez so, mesmo se a resposta se perder;
 - uma maquina so mexe na propria fila de comandos (WHERE origem=?).
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

P = None   # o modulo ponto.py; preenchido por instalar()

DESTINO = (os.environ.get("PONTO_ENVIAR_PARA") or "").strip().rstrip("/")
DESTINO_SENHA = os.environ.get("PONTO_ENVIAR_SENHA") or ""
ENVIAR_A_CADA = max(30, int(os.environ.get("PONTO_ENVIAR_A_CADA") or 300))
NOME = (os.environ.get("PONTO_NOME") or socket.gethostname().split(".")[0] or "ponto")[:40]
ACEITA_COMANDOS = (os.environ.get("PONTO_ACEITAR_COMANDOS") or "").strip().lower() in (
    "1", "sim", "s", "true", "yes")

JANELA_ENVIO = 35 * 86400   # quanto pra tras cada envio regrava (consertos e apagados)
_acorda_envio = threading.Event()
ENVIO = {"destino": DESTINO, "nome": NOME, "ultimo_ok": None, "erro": None}

# SEGURANCA: O SERVIDOR NAO EXECUTA SHELL. Esta e a lista fechada de tudo que o
#   central pode pedir pra outra maquina, e tudo mexe so no Ponto (veja
#   executa_comando). Nao existe os.system, subprocess, eval ou exec em lugar
#   nenhum. Se um dia alguem invadir o central, ganha o seu ponto, nao o
#   computador das maquinas. Antes de acrescentar um item aqui, pense: "e se o
#   central for de outra pessoa?"
COMANDOS = ("ponto", "lanca_turno", "muda_turno", "apaga_turno", "temas", "temas_fora", "meta")
COMANDO_VALIDADE = 7 * 86400   # comando que a maquina nao buscou em 7 dias caduca
ESPERA_MAX = 25
_chegou_comando = threading.Condition()
COMANDOS_ESTADO = {"conectado": False, "erro": None, "ultimo": None}


# ------------------------------------------------------------------ tabelas do central

def cria_tabelas():
    """Rodada pelo nucleo no cria_banco. As tabelas que so o central usa: as
    maquinas que mandam pra ca e a fila de comandos. (As colunas `origem` e
    `id_origem` em `turnos` ficam no nucleo, porque o turno recebido vira um
    turno comum no mesmo banco.)"""
    with P.conexao() as c:
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


# ------------------------------------------------------------------ receber (central)

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
    # SEGURANCA: lote recebido: no maximo 20.000 turnos, origem com 40 caracteres,
    #   turno antes do "desde" descartado, fim antes do inicio vira zero.
    if not isinstance(turnos, list) or len(turnos) > 20000:
        raise ValueError("turnos invalidos")
    limpos = []
    for t in turnos:
        if not isinstance(t, dict):
            continue
        ini, fim = P._inteiro(t.get("inicio")), P._inteiro(t.get("fim"))
        if not ini or ini < desde:
            continue
        if fim is not None and fim < ini:
            fim = ini
        limpos.append((ini, fim, str(t.get("obs") or "")[:120], P.limpa_nome(t.get("tema")),
                       origem, P._inteiro(t.get("id"))))
    estado = d.get("estado") if isinstance(d.get("estado"), dict) else {}
    with P.conexao() as c:
        c.execute("DELETE FROM turnos WHERE origem=? AND inicio>=?", (origem, desde))
        c.executemany("INSERT INTO turnos (inicio,fim,obs,tema,origem,id_origem)"
                      " VALUES (?,?,?,?,?,?)", limpos)
        c.execute("INSERT INTO maquinas (nome,ultimo_envio,estado) VALUES (?,?,?)"
                  " ON CONFLICT(nome) DO UPDATE SET ultimo_envio=excluded.ultimo_envio,"
                  " estado=excluded.estado",
                  # SEGURANCA: o "estado" que a maquina manda e guardado cortado em 4 KB.
                  (origem, int(time.time()), json.dumps(estado, ensure_ascii=False)[:4000]))
    return {"ok": True, "origem": origem, "recebidos": len(limpos)}


# ------------------------------------------------------------------ enviar (maquina)

def pede_central(metodo, caminho, corpo=None, timeout=20):
    cab = {"User-Agent": "Ponto/1"}
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo).encode("utf-8")
        cab["Content-Type"] = "application/json"
    # SEGURANCA: quem viaja autentica no central com a senha dele num cabecalho
    #   Bearer (sem navegador nao ha cookie). Por http iria aberta - veja o aviso
    #   em sobe_threads e use https.
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
    with P.conexao() as c:
        completo = P.ajuste(c, "envio_completo") != DESTINO
        desde = 0 if completo else int(time.time()) - JANELA_ENVIO
        # SEGURANCA: so os turnos desta maquina (origem='') sobem; um recebido de
        #   outra nunca e reenviado, senao duas maquinas ficariam em pingue-pongue.
        linhas = [dict(r) for r in c.execute(
            "SELECT id,inicio,fim,tema,obs FROM turnos WHERE origem='' AND inicio>=?"
            " ORDER BY inicio", (desde,))]
        ab = P.turno_aberto(c)
        estado = {"rodando": bool(ab), "tema": (ab["tema"] if ab else ""),
                  "desde": (ab["inicio"] if ab else None), "temas": P.temas(c),
                  "aceita_comandos": ACEITA_COMANDOS}
    pede_central("POST", "/api/receber",
                 {"origem": NOME, "desde": desde, "turnos": linhas, "estado": estado})
    if completo:
        with P.conexao() as c:
            P.grava_ajuste(c, "envio_completo", DESTINO)
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


# ------------------------------------------------------------------ comandos
#
# O central nao alcanca a maquina (ela esta atras de roteador, 4G, rede de
# outro lugar), entao e a maquina que pergunta: um GET que fica pendurado ate
# 25 s no central e volta na hora em que chega um comando. Na pratica o botao
# no painel do central bate o ponto da outra maquina em 1-2 segundos.

def enfileira_comando(origem, acao, dados):
    """Lado central: poe um comando na fila de uma maquina."""
    origem = str(origem or "").strip()[:40]
    if not origem:
        raise ValueError("falta a maquina (origem)")
    # SEGURANCA: primeira barreira: comando fora da lista fechada nem entra na fila.
    if acao not in COMANDOS:
        raise ValueError("comando desconhecido: use " + ", ".join(COMANDOS))
    if not isinstance(dados, dict):
        dados = {}
    with P.conexao() as c:
        cid = c.execute("INSERT INTO comandos (origem,acao,dados,criado_em) VALUES (?,?,?,?)",
                        # SEGURANCA: dados do comando cortados em 8 KB.
                        (origem, acao, json.dumps(dados, ensure_ascii=False)[:8000],
                         int(time.time()))).lastrowid
    with _chegou_comando:
        _chegou_comando.notify_all()
    return cid


def _pendentes(c, origem):
    agora = int(time.time())
    linhas = [dict(r) for r in c.execute(
        "SELECT id,acao,dados,criado_em FROM comandos WHERE origem=? AND feito_em IS NULL"
        # SEGURANCA: comando com mais de 7 dias caduca; no maximo 50 por entrega.
        " AND criado_em>? ORDER BY id LIMIT 50", (origem, agora - COMANDO_VALIDADE))]
    for l in linhas:
        try:
            l["dados"] = json.loads(l["dados"] or "{}")
        except ValueError:
            l["dados"] = {}
    return linhas


def busca_comandos(origem, espera):
    """Lado central (GET pendurado): devolve na hora se ja ha comando, senao
    espera ate 'espera' s acordando quando alguem enfileirar."""
    origem = str(origem or "").strip()[:40]
    if not origem:
        raise ValueError("falta origem")
    # SEGURANCA: a espera pendurada e no maximo 25 s (nao prende thread pra sempre).
    fim = time.time() + max(0, min(ESPERA_MAX, espera))
    while True:
        with P.conexao() as c:
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
    """Lado central: a maquina devolve o que deu em cada comando."""
    origem = str(origem or "").strip()[:40]
    n = 0
    with P.conexao() as c:
        # SEGURANCA: uma maquina so marca comandos dela (WHERE origem=?), ate 100 por vez.
        for r in (resultados or [])[:100]:
            if not isinstance(r, dict) or P._inteiro(r.get("id")) is None:
                continue
            n += c.execute(
                "UPDATE comandos SET feito_em=?, ok=?, resultado=? WHERE id=? AND origem=?",
                (int(time.time()), 1 if r.get("ok") else 0,
                 str(r.get("texto") or r.get("erro") or "")[:300], int(r["id"]), origem)).rowcount
    return n


def maquinas():
    """Lado central: as maquinas, o que roda em cada uma e os ultimos comandos."""
    agora = int(time.time())
    with P.conexao() as c:
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


# SEGURANCA: segunda barreira, do lado da maquina: mesmo que o central mande
#   qualquer coisa, so o que esta neste if/elif roda; cada ramo chama exatamente
#   a mesma funcao do nucleo que o painel local chamaria. O resto vira "comando
#   desconhecido". Nada aqui toca o sistema operacional.
def executa_comando(cmd):
    acao, d = cmd.get("acao"), cmd.get("dados") or {}
    if not isinstance(d, dict):
        d = {}
    try:
        if acao == "ponto":
            qual = d.get("acao") or "alterna"
            # SEGURANCA: acao de ponto validada de novo na maquina.
            if qual not in ("entra", "sai", "alterna", "troca"):
                return {"ok": False, "erro": "acao de ponto invalida"}
            # a hora e a do clique no central: comando que esperou a maquina
            # ligar nao comeca o turno no momento em que ela ligou
            r = P.bate_ponto(qual, d.get("tema"), cmd.get("criado_em"))
            return {"ok": bool(r.get("ok")), "texto": P.texto_ponto(r, P.semana())}
        if acao == "lanca_turno":
            tid = P.cria_turno(d)
            return {"ok": bool(tid), "texto": "turno lan\u00e7ado" if tid else "precisa de inicio"}
        if acao == "muda_turno":
            campos = {k: d[k] for k in ("inicio", "fim", "tema", "obs") if k in d}
            t = P.muda_turno(P._inteiro(d.get("id")) or 0, campos)
            return {"ok": bool(t), "texto": "turno consertado" if t else "turno n\u00e3o encontrado"}
        if acao == "apaga_turno":
            ok = P.apaga_turno(P._inteiro(d.get("id")) or 0)
            return {"ok": ok, "texto": "turno apagado" if ok else "turno n\u00e3o encontrado"}
        if acao == "temas":
            return {"ok": True, "texto": "temas: " + ", ".join(P.muda_temas(d.get("temas")))}
        if acao == "temas_fora":
            return {"ok": True, "texto": "n\u00e3o contam: " + (", ".join(P.muda_temas_fora(d.get("temas"))) or "nenhum")}
        if acao == "meta":
            return {"ok": True, "texto": "meta %s por semana" % P.hm_txt(P.muda_meta(d.get("horas")))}
    except (TypeError, ValueError) as e:
        return {"ok": False, "erro": "dados invalidos: %s" % e}
    return {"ok": False, "erro": "comando desconhecido"}


def atende_comandos(lista):
    """Lado da maquina: executa cada comando uma vez so, mesmo que o central
    mande de novo (a resposta anterior pode ter se perdido no caminho)."""
    respostas = []
    for cmd in lista:
        cid = P._inteiro(cmd.get("id"))
        if cid is None:
            continue
        with P.conexao() as c:
            # SEGURANCA: comando repetido nao roda duas vezes (resposta perdida no
            #   caminho faz o central reenviar o mesmo id).
            ja = c.execute("SELECT resposta FROM comandos_feitos WHERE id_central=?", (cid,)).fetchone()
        if ja:
            r = json.loads(ja["resposta"])
        else:
            r = executa_comando(cmd)
            with P.conexao() as c:
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


# ------------------------------------------------------------------ ganchos no nucleo

def poe_envio(s):
    """Acrescenta o estado do envio ao /api/semana, pro painel mostrar."""
    s["envio"] = (dict(ENVIO, comandos=ACEITA_COMANDOS, ouvindo=COMANDOS_ESTADO["conectado"])
                  if DESTINO else None)


# SEGURANCA: as rotas do central (/api/receber e /api/comandos*) recusam com 403
#   quando o servidor nao tem PONTO_SENHA - sem senha, qualquer um na internet
#   escreveria no seu ponto ou mandaria comando pras suas maquinas. /api/maquinas
#   nao precisa do 403 proprio: ja passou pelo login do nucleo (_porta_fechada).
def _precisa_senha(h):
    if P.SENHA:
        return False
    h._json(403, {"erro": "o central precisa de PONTO_SENHA"})
    return True


def rota(h, metodo, u, dados):
    """Chamada pelo nucleo em cada GET/POST, ja depois do login. Devolve True
    se tratou a rota. `dados` e o dict de query (GET) ou do corpo (POST)."""
    if metodo == "GET" and u.path == "/api/maquinas":
        h._json(200, {"maquinas": maquinas()})
        return True
    if metodo == "GET" and u.path == "/api/comandos":
        if _precisa_senha(h):
            return True
        try:
            espera = P._inteiro((dados.get("espera") or [""])[0]) or 0
            h._json(200, {"comandos": busca_comandos((dados.get("origem") or [""])[0], espera)})
        except ValueError as e:
            h._json(400, {"erro": str(e)})
        return True
    if metodo == "POST" and u.path == "/api/receber":
        if _precisa_senha(h):
            return True
        try:
            h._json(200, receber(dados))
        except ValueError as e:
            h._json(400, {"erro": str(e)})
        return True
    if metodo == "POST" and u.path == "/api/comandos":
        if _precisa_senha(h):
            return True
        try:
            h._json(201, {"ok": True, "id": enfileira_comando(
                dados.get("origem"), dados.get("acao"), dados.get("dados"))})
        except ValueError as e:
            h._json(400, {"erro": str(e)})
        return True
    if metodo == "POST" and u.path == "/api/comandos/resultado":
        if _precisa_senha(h):
            return True
        h._json(200, {"ok": True, "marcados": registra_resultados(
            dados.get("origem"), dados.get("resultados"))})
        return True
    return False


def sobe_threads():
    """Chamada pelo nucleo no main(): liga os lacos de envio e de comandos."""
    if not DESTINO:
        if ACEITA_COMANDOS:
            print("AVISO: PONTO_ACEITAR_COMANDOS sem PONTO_ENVIAR_PARA - nao ha de quem receber.",
                  flush=True)
        return
    if DESTINO.startswith("http://") and not any(
            x in DESTINO for x in ("localhost", "127.0.0.1", "192.168.", "10.", "100.")):
        # SEGURANCA: avisa quando a senha do central iria aberta (http na internet).
        print("AVISO: enviando por http pra internet - a senha vai aberta. Use https.", flush=True)
    print("Enviando pra %s a cada %ds como '%s'" % (DESTINO, ENVIAR_A_CADA, NOME), flush=True)
    threading.Thread(target=laco_envio, daemon=True).start()
    if ACEITA_COMANDOS:
        print("Aceitando comandos de %s (so acoes do Ponto)" % DESTINO, flush=True)
        threading.Thread(target=laco_comandos, daemon=True).start()


def instalar(nucleo):
    """O nucleo chama isto ao subir, se este arquivo existir. Registra os
    ganchos; a partir daqui o nucleo nao conhece mais nada de central."""
    global P
    P = nucleo
    nucleo.registra("banco", cria_tabelas)
    nucleo.registra("semana", poe_envio)
    nucleo.registra("rota", rota)
    nucleo.registra("mudou", _acorda_envio.set)   # bater/consertar daqui envia ja
    nucleo.registra("subir", sobe_threads)
