"""Enche um banco de demonstracao com 8 semanas de turnos inventados.

    PONTO_BANCO=/tmp/ponto-demo.db python3 ferramentas/demo.py
    PONTO_BANCO=/tmp/ponto-demo.db python3 servidor/ponto.py
"""
import importlib.util, os, random, time
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.environ.get("PONTO_BANCO"):
    raise SystemExit("defina PONTO_BANCO (nao vou mexer no banco de verdade)")
spec = importlib.util.spec_from_file_location("ponto", os.path.join(RAIZ, "servidor", "ponto.py"))
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
p.cria_banco()
p.muda_temas(["Trabalho", "Estudo", "Transporte"])
p.muda_temas_fora(["Transporte"])
random.seed(7)
agora = int(time.time())
ini = p.inicio_da_semana(agora) - 7 * 7 * 86400
dia = ini
while dia + 86400 < agora:
    if random.random() < 0.85:
        t = dia + 8 * 3600 + random.randint(0, 3600)
        for tema, horas in (("Transporte", .5), ("Trabalho", random.uniform(3, 5)),
                            ("Trabalho", random.uniform(2, 4)), ("Estudo", random.uniform(.5, 2))):
            fim = min(t + int(horas * 3600), agora - 600)
            if fim > t:
                p.cria_turno({"inicio": t, "fim": fim, "tema": tema})
            t = fim + random.randint(600, 3600)
    dia += 86400
p.bate_ponto("entra", "Trabalho", agora - 4000)
print("ok:", p.BANCO)
