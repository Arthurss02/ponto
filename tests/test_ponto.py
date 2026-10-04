"""Testes do Ponto. So biblioteca padrao:

    python3 -m unittest discover -s tests -v
"""
import datetime
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA = tempfile.mkdtemp(prefix="ponto-teste-")
os.environ["PONTO_BANCO"] = os.path.join(PASTA, "ponto.db")
os.environ.pop("PONTO_SENHA", None)
os.environ["PONTO_ENV"] = ""          # nao ler o ponto.env de quem roda os testes
os.environ.pop("PONTO_ENVIAR_PARA", None)

spec = importlib.util.spec_from_file_location("ponto", os.path.join(RAIZ, "servidor", "ponto.py"))
ponto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ponto)


def ts(*a):
    return int(datetime.datetime(*a).timestamp())


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(ponto.BANCO):
            os.remove(ponto.BANCO)
        ponto.SENHA = ""
        ponto.cria_banco()

    def turnos(self):
        with ponto.conexao() as c:
            return [dict(r) for r in c.execute("SELECT * FROM turnos ORDER BY inicio")]


class Batidas(Base):
    def test_entrar_duas_vezes_nao_cria_fantasma(self):
        ponto.bate_ponto("entra", "Trabalho")
        r = ponto.bate_ponto("entra", "Trabalho")
        self.assertTrue(r["ja_estava"])
        self.assertEqual(len(self.turnos()), 1)

    def test_sair_parado_nao_quebra(self):
        r = ponto.bate_ponto("sai")
        self.assertFalse(r["rodando"])
        self.assertTrue(r["ja_estava"])

    def test_troca_fecha_e_abre_no_mesmo_instante(self):
        agora = int(time.time())
        ponto.bate_ponto("entra", "Trabalho", agora - 600)
        ponto.bate_ponto("troca", "Estudo", agora - 300)
        t = self.turnos()
        self.assertEqual([x["tema"] for x in t], ["Trabalho", "Estudo"])
        self.assertEqual(t[0]["fim"], t[1]["inicio"])
        self.assertIsNone(t[1]["fim"])

    def test_tema_desconhecido_cai_no_primeiro(self):
        r = ponto.bate_ponto("entra", "Inventado")
        self.assertEqual(r["tema"], "Trabalho")

    def test_alterna_volta_pro_ultimo_tema(self):
        agora = int(time.time())
        ponto.bate_ponto("entra", "Estudo", agora - 100)
        ponto.bate_ponto("sai", None, agora - 50)
        r = ponto.bate_ponto("alterna")
        self.assertEqual(r["tema"], "Estudo")
        self.assertFalse(ponto.bate_ponto("alterna")["rodando"])

    def test_sai_guardado_offline_antes_do_turno_nao_fecha_ele(self):
        agora = int(time.time())
        ponto.bate_ponto("entra", "Trabalho", agora - 60)
        r = ponto.bate_ponto("sai", None, agora - 3600)
        self.assertTrue(r["rodando"])
        self.assertIsNone(self.turnos()[0]["fim"])

    def test_entra_atrasado_nao_sobrepoe_turno_fechado(self):
        agora = int(time.time())
        ponto.cria_turno({"inicio": agora - 7200, "fim": agora - 1800, "tema": "Trabalho"})
        r = ponto.bate_ponto("entra", "Estudo", agora - 3600)
        self.assertEqual(r["desde"], agora - 1800)

    def test_batida_do_futuro_ou_velha_vira_agora(self):
        agora = int(time.time())
        self.assertEqual(ponto.hora_da_batida(agora + 999, agora), agora)
        self.assertEqual(ponto.hora_da_batida(agora - 8 * 86400, agora), agora)
        self.assertEqual(ponto.hora_da_batida(agora - 60, agora), agora - 60)


class Semana(Base):
    def test_semana_comeca_na_segunda(self):
        # 2026-10-07 e uma quarta
        self.assertEqual(ponto.inicio_da_semana(ts(2026, 10, 7, 15)), ts(2026, 10, 5))
        # domingo a noite ainda e a semana velha
        self.assertEqual(ponto.inicio_da_semana(ts(2026, 10, 11, 23, 30)), ts(2026, 10, 5))

    def test_turno_que_vira_a_madrugada_conta_no_dia_em_que_comecou(self):
        ponto.cria_turno({"inicio": ts(2026, 10, 6, 22), "fim": ts(2026, 10, 7, 2), "tema": "Trabalho"})
        s = ponto.semana(ts(2026, 10, 8, 12))
        self.assertAlmostEqual(s["por_dia"][1], 4.0)   # terca
        self.assertAlmostEqual(s["por_dia"][2], 0.0)
        self.assertAlmostEqual(s["horas"], 4.0)

    def test_tema_fora_e_medido_mas_nao_soma(self):
        ponto.muda_temas(["Trabalho", "Transporte"])
        ponto.muda_temas_fora(["Transporte"])
        ponto.cria_turno({"inicio": ts(2026, 10, 5, 8), "fim": ts(2026, 10, 5, 10), "tema": "Trabalho"})
        ponto.cria_turno({"inicio": ts(2026, 10, 5, 10), "fim": ts(2026, 10, 5, 11), "tema": "Transporte"})
        s = ponto.semana(ts(2026, 10, 5, 20))
        self.assertAlmostEqual(s["horas"], 2.0)
        self.assertAlmostEqual(s["horas_fora"], 1.0)
        self.assertEqual(s["temas_fora"], ["Transporte"])

    def test_tema_removido_sai_do_nao_conta(self):
        ponto.muda_temas(["Trabalho", "Transporte"])
        ponto.muda_temas_fora(["Transporte"])
        ponto.muda_temas(["Trabalho"])
        with ponto.conexao() as c:
            self.assertEqual(ponto.temas_fora(c), [])

    def test_meta_do_dia_congela_no_que_havia_antes_de_hoje(self):
        ponto.muda_meta(35)
        ponto.cria_turno({"inicio": ts(2026, 10, 5, 8), "fim": ts(2026, 10, 5, 15), "tema": "Trabalho"})
        s = ponto.semana(ts(2026, 10, 6, 9))           # terca: faltam 28h em 6 dias
        self.assertAlmostEqual(s["meta_hoje"], 28 / 6, places=2)
        ponto.cria_turno({"inicio": ts(2026, 10, 6, 8), "fim": ts(2026, 10, 6, 14), "tema": "Trabalho"})
        s = ponto.semana(ts(2026, 10, 6, 15))
        self.assertAlmostEqual(s["meta_hoje"], 28 / 6, places=2)   # nao fugiu
        self.assertGreaterEqual(s["pct_hoje"], 100)

    def test_historico_tem_n_semanas_e_marca_a_atual(self):
        h = ponto.historico(8, ts(2026, 10, 7, 12))
        self.assertEqual(len(h), 8)
        self.assertTrue(h[-1]["atual"])
        self.assertEqual(h[-1]["inicio"], ts(2026, 10, 5))

    def test_conserto_com_fim_antes_do_inicio_vira_zero(self):
        tid = ponto.cria_turno({"inicio": ts(2026, 10, 5, 8), "fim": ts(2026, 10, 5, 9)})
        t = ponto.muda_turno(tid, {"fim": ts(2026, 10, 5, 7)})
        self.assertEqual(t["fim"], t["inicio"])


class VariasMaquinas(Base):
    def lote(self, desde, *turnos):
        return {"origem": "oficina", "desde": desde,
                "turnos": [{"id": i + 1, "inicio": a, "fim": b, "tema": "Trabalho"}
                           for i, (a, b) in enumerate(turnos)]}

    def test_receber_duas_vezes_nao_duplica_e_reflete_apagado(self):
        a, b = (ts(2026, 10, 5, 8), ts(2026, 10, 5, 12)), (ts(2026, 10, 6, 8), ts(2026, 10, 6, 10))
        ponto.receber(self.lote(0, a, b))
        ponto.receber(self.lote(0, a, b))
        self.assertEqual(len(self.turnos()), 2)
        ponto.receber(self.lote(0, a))            # b foi apagado la
        self.assertEqual(len(self.turnos()), 1)
        self.assertAlmostEqual(ponto.semana(ts(2026, 10, 7, 12))["horas"], 4.0)

    def test_janela_nao_apaga_o_que_veio_antes_dela(self):
        velho = (ts(2026, 9, 1, 8), ts(2026, 9, 1, 9))
        ponto.receber(self.lote(0, velho))
        ponto.receber(self.lote(ts(2026, 9, 20), (ts(2026, 10, 5, 8), ts(2026, 10, 5, 9))))
        self.assertEqual(len(self.turnos()), 2)

    def test_turno_de_fora_e_so_leitura_e_nao_e_o_cronometro_daqui(self):
        agora = int(time.time())
        ponto.receber(self.lote(0, (agora - 600, None)))   # rodando la
        tid = self.turnos()[0]["id"]
        self.assertIsNone(ponto.muda_turno(tid, {"fim": agora}))
        self.assertFalse(ponto.apaga_turno(tid))
        r = ponto.bate_ponto("entra", "Estudo")
        self.assertFalse(r.get("ja_estava"))
        self.assertTrue(ponto.semana()["rodando"])

    def test_receber_sem_origem_e_recusado(self):
        with self.assertRaises(ValueError):
            ponto.receber({"turnos": []})


class Http(Base):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ponto.Ponto)
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def pede(self, metodo, caminho, corpo=None, cab=None):
        dados = json.dumps(corpo).encode() if corpo is not None else None
        req = urllib.request.Request(self.url + caminho, data=dados, method=metodo,
                                     headers=dict({"Content-Type": "application/json"}, **(cab or {})))
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def test_sem_senha_tudo_abre(self):
        st, corpo, _ = self.pede("GET", "/api/semana")
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(corpo)["com_senha"])
        for caminho in ("/", "/app", "/app.webmanifest", "/app-sw.js", "/entrar"):
            self.assertEqual(self.pede("GET", caminho)[0], 200, caminho)

    def test_bater_pela_api(self):
        st, corpo, _ = self.pede("POST", "/api/ponto", {"acao": "entra", "tema": "Estudo"})
        r = json.loads(corpo)
        self.assertEqual(st, 200)
        self.assertTrue(r["semana"]["rodando"])
        self.assertIn("Começou Estudo", r["texto"])
        self.assertEqual(self.pede("POST", "/api/ponto", {"acao": "voar"})[0], 400)

    def test_com_senha_fecha_a_api_e_abre_com_cookie_ou_bearer(self):
        ponto.SENHA = "segredo"
        self.assertEqual(self.pede("GET", "/api/semana")[0], 401)
        self.assertEqual(self.pede("GET", "/app.webmanifest")[0], 200)
        st, _, _ = self.pede("GET", "/api/semana", cab={"Authorization": "Bearer segredo"})
        self.assertEqual(st, 200)
        biscoito = "%s=%s" % (ponto.COOKIE, ponto.ficha())
        self.assertEqual(self.pede("GET", "/api/semana", cab={"Cookie": biscoito})[0], 200)
        self.assertEqual(self.pede("GET", "/api/semana", cab={"Cookie": "ponto=errado"})[0], 401)

    def test_central_sem_senha_nao_recebe(self):
        self.assertEqual(self.pede("POST", "/api/receber", {"origem": "x", "turnos": []})[0], 403)

    def test_envio_chega_no_central(self):
        ponto.SENHA = "central"
        agora = int(time.time())
        ponto.cria_turno({"inicio": agora - 3600, "fim": agora - 60, "tema": "Trabalho"})
        ponto.DESTINO, ponto.DESTINO_SENHA = self.url, "errada"
        try:
            with self.assertRaises(urllib.error.HTTPError):
                ponto.envia_uma_vez()
            ponto.DESTINO_SENHA = "central"
            self.assertEqual(ponto.envia_uma_vez(), 1)
            ponto.envia_uma_vez()                           # de novo: nao duplica
        finally:
            ponto.DESTINO, ponto.DESTINO_SENHA = "", ""
        # mesmo banco nos dois papeis: o daqui (origem '') e a copia recebida
        origens = sorted(t["origem"] for t in self.turnos())
        self.assertEqual(origens, ["", ponto.NOME])

    def test_arquivo_fora_da_lista_nao_sai(self):
        self.assertEqual(self.pede("GET", "/../servidor/ponto.py")[0], 404)
        self.assertEqual(self.pede("GET", "/dados/ponto.db")[0], 404)


if __name__ == "__main__":
    unittest.main()
