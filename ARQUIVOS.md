# Mapa dos arquivos (pra quem vai mexer no código)

Tudo roda com a biblioteca padrão do Python 3.9+. Rode os testes antes e
depois de mudar qualquer coisa: `python3 -m unittest discover -s tests -v`.

## servidor/ponto.py

O servidor inteiro: banco, regras da semana, API, login, envio pro central
e comandos. Está dividido em seções marcadas com `# ----`.

- **Configuração (topo)**: `le_arquivo_env` lê o `ponto.env`, mas só aceita
  chaves `PONTO_*` e `TZ`, e nunca sobrescreve uma variável de ambiente que
  já exista.
- **banco**: `conexao()` passa toda operação por um lock global. Se der
  erro, desfaz (rollback). `cria_banco` faz a migração e pode rodar de novo
  sem estragar nada (adiciona `origem` e `id_origem` em banco antigo).
- **temas e meta**: nome de tema tem até 28 caracteres e são no máximo 6
  temas. A lista de "não conta" só aceita temas que existem. A meta fica
  presa entre 1 e 168 h.
- **batidas**: `hora_da_batida` troca hora do futuro, ou com mais de 7 dias,
  pela hora de agora. `bate_ponto` é idempotente: bater duas vezes não cria
  turno fantasma. Batida atrasada não sobrepõe turno que já fechou, e tema
  desconhecido cai no primeiro da lista.
- **turnos a mão**: anotação tem até 120 caracteres. Se o fim vier antes
  do início, vira duração zero. Turno vindo de outra máquina
  (`origem<>''`) não pode ser editado nem apagado aqui; só a máquina de
  origem conserta.
- **várias máquinas**: em `receber`, a origem tem até 40 caracteres, cada
  lote tem no máximo 20.000 turnos, e turno anterior ao `desde` é
  descartado. O lote substitui os turnos daquela origem no período, então
  receber o mesmo lote duas vezes não duplica nada. Já `envia_uma_vez` só
  manda os turnos locais, pra duas máquinas não ficarem reenviando uma pra
  outra.
- **comandos do central**: a lista do que pode ser pedido é fechada
  (`COMANDOS`) e só mexe no Ponto; não existe execução de comando de
  sistema. A validação acontece duas vezes, quando o comando entra na fila
  (`enfileira_comando`) e quando é executado (`executa_comando`). Além
  disso:
  - os dados de um comando têm até 8 KB;
  - o comando que ninguém buscou caduca em 7 dias;
  - a espera da requisição pendurada é de no máximo 25 s;
  - `atende_comandos` guarda o id de cada comando já executado (por 30
    dias), pra não executar duas vezes;
  - `registra_resultados` só marca comandos da própria origem, até 100 por
    chamada.
- **senha**: a sessão é um HMAC-SHA256 da `PONTO_SENHA`. Trocar a senha
  desloga todo mundo, porque não há tabela de sessões. Todas as comparações
  usam `hmac.compare_digest`. Aceita cookie ou `Authorization: Bearer`.
- **http**:
  - só serve os arquivos listados em `ESTATICOS`, então caminhos como
    `../servidor/ponto.py` ou `dados/ponto.db` dão 404;
  - abrem sem senha só o que está em `PUBLICOS`;
  - o corpo da requisição tem limite de 64 KB e precisa ser um objeto JSON;
  - toda resposta sai com `nosniff`, `Referrer-Policy: same-origin` e
    `no-store`;
  - API sem senha responde 401, página sem senha redireciona pro login;
  - `/api/receber` e `/api/comandos*` recusam com 403 se o servidor não
    tiver `PONTO_SENHA`;
  - o login errado espera 1 s antes de responder, e o redirecionamento
    depois do login só vai pra `app` ou `./`, pra ninguém usar o login pra
    mandar a pessoa pra outro site;
  - o cookie é `HttpOnly` e `SameSite=Lax`, e ganha `Secure` quando vem
    `X-Forwarded-Proto: https`.
- **main**: avisa quando está aberto pra rede sem senha e quando envia por
  http pra internet. Os laços de envio e de comandos só sobem se tiverem
  sido configurados.

**Pontos fracos conhecidos**, pra quem quiser melhorar:
- a espera de 1 s no login não segura chutes em paralelo;
- o `X-Forwarded-Proto` é aceito de qualquer um (isso só afeta a flag
  `Secure`);
- não há usuários: um servidor, uma senha;
- a senha fica em texto puro no ambiente ou no `ponto.env`.

## web/

- **index.html**: o painel do PC.
  - tudo que vem do servidor passa por `esc()` antes de ir pro `innerHTML`;
  - resposta 401 manda pra tela de login;
  - apagar turno pede confirmação;
  - turno de outra máquina aparece só pra leitura;
  - os botões das outras máquinas só enfileiram o comando `ponto`.
- **app.html**: o app do celular.
  - cada batida vai primeiro pra uma fila no `localStorage`
    (`ponto_fila_v1`) com a hora do toque, e sobe em ordem; se o envio
    falha, a batida fica na fila;
  - a mesma regra do servidor é aplicada por cima do estado, pra tela não
    mentir enquanto está offline;
  - o `esc()` vale aqui também;
  - resposta 401 manda pra `entrar?volta=app`.
- **app-sw.js**: o service worker, que só liga em HTTPS. Nunca guarda nada
  de `/api/` e só guarda resposta boa, sem redirecionamento. Assim a tela
  de login não vira a "casca" offline do app.
- **entrar.html**: a tela de senha. O parâmetro `volta` também é filtrado
  aqui, só `app` ou vazio.
- **app.webmanifest, icone-*.png**: instalação na tela de início. Abrem sem
  senha porque o celular pede esses arquivos antes de ter o cookie.

## tests/test_ponto.py

São 30 testes, divididos em batidas, semana, várias máquinas, comandos e
HTTP (senha, 403 do central, arquivo fora da lista). Cada teste usa um
banco temporário, e `PONTO_ENV=""` impede que o seu `ponto.env` vaze pros
testes. Para cada regra nova, escreva o teste junto.

## ferramentas/

- **demo.py**: enche um banco com 8 semanas inventadas. Se você não passar
  `PONTO_BANCO`, ele se recusa a rodar, pra não sujar o banco de verdade.
- **icone.py**: gera os ícones PNG à mão (zlib + struct, sem PIL).

## Instalação e configuração

- **Dockerfile**: Alpine com `tzdata` (sem ele a semana vira em UTC). O
  container roda como root, porque a pasta `./dados` montada do host
  costuma ser do root. Melhorar isso exige criar um usuário e acertar o
  dono da pasta.
- **docker-compose.yml**: tem a senha de exemplo `troque-esta-senha`, que é
  pra trocar.
- **extras/ponto.service**: serviço systemd de usuário, sem root.
- **extras/Caddyfile**: HTTPS automático na frente, com o Ponto escutando
  em `127.0.0.1`.
- **ponto.env.exemplo**: o modelo do `ponto.env`.
- **.gitignore**: deixa fora do git `dados/`, `*.db` e `ponto.env`, ou
  seja, nada de senha nem de banco no repositório.
