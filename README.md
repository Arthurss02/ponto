# Ponto

Cronômetro da semana de trabalho. Você bate o ponto pelo celular quando senta
pra trabalhar (ou estudar, ou o que for) e quando levanta, e ele responde a
pergunta que importa: **no ritmo que estou, fecho a semana com quantas horas?**

- **App do celular** (`/app`): uma tela só. Toca no tema pra começar, toca em
  Parar pra parar. Sem sinal, a batida fica guardada no aparelho com a hora do
  toque e sobe sozinha quando a conexão volta — o toque nunca se perde.
- **Painel do PC** (`/`): a semana inteira, dia a dia por tema, as últimas
  semanas, a lista de turnos pra consertar o que ficou esquecido aberto, lançar
  o dia que você não bateu, e mudar temas e meta.

Um arquivo Python, só biblioteca padrão (nada pra instalar além do Python 3.9+),
e um banco SQLite. Cada pessoa roda o seu: no próprio PC, num servidor de casa
ou numa VPS.

## Rodar no seu PC

Precisa do Python 3.9 ou mais novo ([python.org](https://www.python.org/downloads/)).

```bash
git clone https://github.com/Arthurss02/ponto.git
cd ponto
python3 servidor/ponto.py          # no Windows: py servidor\ponto.py
```

Abra **http://localhost:8095/** no PC. No celular, conectado na **mesma Wi-Fi**,
abra **http://IP-DO-PC:8095/app** (o IP aparece em Configurações de rede do PC,
algo como `192.168.0.15`). No Windows, aceite o aviso do firewall na primeira vez.

Os dados ficam em `dados/ponto.db`. O PC precisa estar ligado pra o celular
bater o ponto — se ele estiver desligado, o app guarda as batidas e manda
quando conseguir.

**Fora de casa:** o jeito mais simples é o [Tailscale](https://tailscale.com)
(gratuito) no PC e no celular. Aí você usa `http://IP-DO-TAILSCALE:8095/app` de
qualquer lugar, sem abrir porta nenhuma no roteador.

## Rodar numa VPS (ou servidor de casa sempre ligado)

Numa máquina exposta na internet, **defina uma senha** e ponha **HTTPS** na
frente. Sem senha, qualquer um que achar o endereço mexe no seu ponto.

### Com Docker

```bash
git clone https://github.com/Arthurss02/ponto.git && cd ponto
# edite docker-compose.yml e troque PONTO_SENHA
docker compose up -d
```

### Sem Docker (Linux com systemd)

```bash
git clone https://github.com/Arthurss02/ponto.git ~/ponto
mkdir -p ~/.config/systemd/user && cp ~/ponto/extras/ponto.service ~/.config/systemd/user/
# edite ~/.config/systemd/user/ponto.service e descomente/troque a PONTO_SENHA
systemctl --user daemon-reload && systemctl --user enable --now ponto
loginctl enable-linger $USER
```

### HTTPS

Com um domínio apontando pra VPS, o [Caddy](https://caddyserver.com) faz o
certificado sozinho — veja `extras/Caddyfile`. Com HTTPS o app do celular abre
até sem internet (service worker) e o cookie de login vai só por conexão segura.
Nesse caso deixe o Ponto escutando só local: `PONTO_HOST=127.0.0.1`.

## Pôr o app na tela de início do celular

- **iPhone:** abra `/app` no Safari → botão Compartilhar → **Adicionar à Tela de Início**.
- **Android:** abra `/app` no Chrome → menu ⋮ → **Instalar app** (ou Adicionar à tela inicial).

Abre em tela cheia, como um app.

## Atalho de um toque (botão de ação do iPhone, Tasker, etc.)

`alterna` decide sozinho: se está parado, começa (no último tema usado); se
está rodando, para. A resposta traz um `texto` pronto pra mostrar numa
notificação ("Parou · 3h12 de Trabalho · hoje 5h40 de 6h00 (94%)").

```
POST http://SEU-SERVIDOR:8095/api/ponto
Content-Type: application/json
Authorization: Bearer SUA-SENHA          (só se tiver PONTO_SENHA)

{"acao": "alterna"}
```

No app **Atalhos** do iPhone: ação *Obter Conteúdo de URL* (método POST,
cabeçalhos e corpo JSON acima) → *Obter Valor do Dicionário* `texto` →
*Mostrar Notificação*. Depois associe o atalho ao botão de ação.

## Configuração

Tudo por variável de ambiente:

| Variável | Padrão | Pra quê |
|---|---|---|
| `PONTO_PORTA` | `8095` | porta |
| `PONTO_HOST` | `0.0.0.0` | `127.0.0.1` pra aceitar só da própria máquina (atrás do Caddy) |
| `PONTO_BANCO` | `dados/ponto.db` | onde fica o banco |
| `PONTO_SENHA` | vazio | pede senha em tudo. Trocar a senha desloga todos os aparelhos |
| `TZ` | do sistema | fuso: a semana vira na segunda 00:00 deste fuso |
| `PONTO_LOG` | vazio | qualquer valor liga o log de cada requisição |

Meta da semana (padrão 40h) e temas (até 6) mudam pelo painel.

## Como a conta funciona

- A semana começa **segunda 00:00**. Domingo à noite ainda é a semana velha.
- Um turno conta **inteiro no dia em que começou**: 22h–02h são 4h no dia em
  que começou, não duas metades que não batem com o que você lembra.
- **Meta de hoje** = o que falta da semana ÷ os dias que sobram (hoje incluso),
  congelada no que estava feito antes de hoje. Se acompanhasse o que você fez
  hoje, andaria junto e nunca fecharia em 100%. Se ela passar de 14h (você
  ficou dias fora), o app para de mostrar porcentagem do dia — isso só serviria
  pra culpa.
- Tema marcado como **não conta** (ex.: Transporte) é medido e aparece no
  painel, mas não soma na meta.
- Turno aberto há mais de 16h é tratado como **esquecido**, e o painel avisa
  pra você consertar o fim.
- Bater duas vezes no mesmo sentido não cria turno fantasma. Batida guardada
  offline vale até uma semana e nunca sobrepõe um turno que já fechou.
- A comparação com a semana passada é **até o mesmo instante** (quarta 11h
  contra quarta 11h), não contra a semana passada inteira.

## Backup

O banco é um arquivo só: copie `dados/ponto.db` (com o Ponto parado, ou use
`sqlite3 dados/ponto.db ".backup copia.db"`). O painel também exporta tudo em
CSV (link **Exportar CSV**).

## API

| Método e rota | O que faz |
|---|---|
| `GET /api/semana` | estado atual, totais da semana, por dia, por tema, histórico |
| `POST /api/ponto` | `{"acao": "entra"\|"sai"\|"alterna"\|"troca", "tema": "...", "quando": unix}` |
| `GET /api/turnos?semana=unix` | turnos da semana que contém aquele instante |
| `POST /api/turnos` | lança turno: `{"inicio", "fim", "tema", "obs"}` |
| `PATCH /api/turnos/ID` | conserta `inicio`, `fim`, `tema`, `obs` |
| `DELETE /api/turnos/ID` | apaga |
| `POST /api/temas` | `{"temas": [...]}` |
| `POST /api/temas-fora` | `{"temas": [...]}` — os que não contam na meta |
| `POST /api/meta` | `{"horas": 40}` |
| `GET /api/historico?n=12` | horas das últimas n semanas |
| `GET /api/exportar.csv` | tudo em CSV |

## Desenvolvimento

```bash
python3 -m unittest discover -s tests -v

# brincar com dados inventados, sem encostar no banco de verdade
PONTO_BANCO=/tmp/demo.db python3 ferramentas/demo.py
PONTO_BANCO=/tmp/demo.db python3 servidor/ponto.py
```

`ferramentas/icone.py` redesenha os ícones do app (PNG gerado na mão, sem PIL).

Licença MIT.
