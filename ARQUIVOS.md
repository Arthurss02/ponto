# Mapa dos arquivos (pra quem vai mexer no código)

Tudo roda com a biblioteca padrão do Python 3.9+.

Cada proteção também está marcada no próprio código com um comentário
`SEGURANCA:`. Pra listar todas: `grep -rn SEGURANCA servidor web`.

## servidor/ponto.py

O servidor inteiro: banco, regras da semana, API e login. Dividido em
seções marcadas com `# ----`. Não executa shell nem nada do sistema.

- **Configuração (topo)**: `le_arquivo_env` lê o `ponto.env`, mas só aceita
  chaves `PONTO_*` e `TZ`, e nunca sobrescreve uma variável de ambiente que
  já exista.
- **banco**: `conexao()` passa toda operação por um lock global. Se der
  erro, desfaz (rollback). Todo SQL usa parâmetros `?`. `cria_banco` pode
  rodar de novo sem estragar nada.
- **temas e meta**: nome de tema tem até 28 caracteres e são no máximo 6
  temas. A lista de "não conta" só aceita temas que existem. A meta fica
  presa entre 1 e 168 h.
- **batidas**: `hora_da_batida` troca hora do futuro, ou com mais de 7 dias,
  pela hora de agora. `bate_ponto` é idempotente: bater duas vezes não cria
  turno fantasma. Batida atrasada não sobrepõe turno que já fechou, e tema
  desconhecido cai no primeiro da lista.
- **turnos a mão**: anotação tem até 120 caracteres. Se o fim vier antes
  do início, vira duração zero. O `UPDATE` do conserto só usa nomes de
  coluna fixos.
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
  - o login errado espera 1 s antes de responder, e o redirecionamento
    depois do login só vai pra `app` ou `./`, pra ninguém usar o login pra
    mandar a pessoa pra outro site;
  - o cookie é `HttpOnly` e `SameSite=Lax`, e ganha `Secure` quando vem
    `X-Forwarded-Proto: https`.
- **main**: avisa no terminal quando está aberto pra rede sem senha.

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

## ferramentas/icone.py

Gera os ícones PNG à mão (zlib + struct, sem PIL). Só é preciso se quiser
redesenhar o ícone; os PNGs prontos já estão em `web/`.

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
