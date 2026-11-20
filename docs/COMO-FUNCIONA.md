# Como o Discord Proxy Launcher funciona

Detalhes internos para quem quer entender, auditar ou modificar o app. Para o uso do dia a dia, veja o
[README](../README.md). A especificação original do projeto está em [ESPECIFICACAO.md](ESPECIFICACAO.md).

## Visão geral

1. O app localiza o Discord instalado: `%LOCALAPPDATA%\Discord\app-X.X.XXX\Discord.exe`, na pasta da versão mais
   recente (também procura o Discord PTB e o Canary).
2. Prepara um proxy SOCKS5:
   - **Meu servidor:** um túnel SSH com encaminhamento dinâmico (o equivalente a `ssh -D 1080 -N`) escutando em
     `127.0.0.1:1080`.
   - **Proxy público:** um proxy SOCKS5 local em `127.0.0.1` (o *relay*) que repassa cada conexão do Discord ao
     **servidor dedicado** (o dconnect, numa VPS fora do Brasil, atrás de um Cloudflare Tunnel) ou, se ele estiver
     fora do ar, a um proxy público já testado — e, se esse falhar, ao próximo da fila.
3. Fecha o Discord e o reabre com `--proxy-server=socks5://127.0.0.1:PORTA`, a opção do Chromium que faz **só
   aquele processo** usar o proxy. Nada é configurado no Windows. Com o app elevado, quem abre o Discord é o
   próprio Explorer (veja *Encerramento e privilégios*).
4. Fica monitorando: a cada 30 s, uma conexão pelo proxy até `1.1.1.1:80` (com o servidor dedicado, o `/health`
   dele). Duas falhas seguidas disparam a reconexão do túnel (Meu servidor) ou a troca de proxy (Proxy público).

As listas de proxies são consultadas nesta ordem, até uma responder: ProxyScrape, TheSpeedX no GitHub e a mesma
lista pelo jsDelivr.

## O que passa pelo proxy

O `--proxy-server` vale para a pilha de rede do Chromium dentro do Discord: login, API, gateway (WebSocket),
mensagens, imagens e anexos. O áudio e o vídeo das chamadas, das câmeras e do compartilhamento de tela usam o
módulo de voz nativo do Discord, que manda a mídia por UDP direto aos servidores de voz, fora do proxy. Por isso
as chamadas não sofrem com a latência do proxy, e, nelas, o servidor de voz vê o IP real.

Com o servidor dedicado, o relay só manda por ele os domínios do Discord na porta 443; o envio de anexos (para o
Google), as *Activities*, os embeds e as integrações saem direto, com o IP do usuário (veja abaixo). Com um proxy
público ou no modo Meu servidor, tudo o que passa pelo `--proxy-server` vai pelo proxy.

## Código

| Arquivo | Papel |
|---|---|
| `main.py` | ponto de entrada |
| `launcher/config.py` | os números da especificação (portas, intervalos, fontes de proxy) |
| `launcher/ui.py` | interface tkinter |
| `launcher/session.py` | fluxo de início, monitor de saúde e troca automática nos dois modos |
| `launcher/tunnel.py` | túnel SSH: `ssh.exe` do Windows (chave) e cliente embutido Paramiko + servidor SOCKS5 (senha) |
| `launcher/proxies.py` | download das listas, teste de proxy e fila de reserva |
| `launcher/relay.py` | o proxy local do modo Proxy público: o caminho de cada conexão (servidor dedicado, proxy público ou direto) |
| `launcher/dconnect.py` | cliente do servidor dedicado: túnel WebSocket por HTTPS, verificações |
| `launcher/destinations.py` | quais destinos vão pelo servidor dedicado (a mesma lista do servidor) |
| `server/` | o servidor dedicado (`dconnect_server.py`), o instalador, o firewall e as unidades do systemd — veja [server/README.md](../server/README.md) |
| `launcher/socks5.py` | cliente SOCKS5 mínimo e a verificação de saúde |
| `launcher/discord.py` | localizar, fechar e abrir o Discord com `--proxy-server` |
| `launcher/win.py` | chamadas à API do Windows via ctypes (processos, portas, início sem elevação) |
| `launcher/target.py` | validação dos campos do modo Meu servidor |
| `launcher/selftest.py` | autoteste do executável (`--selftest`) |

## Decisões de projeto

Os números estão todos em [`launcher/config.py`](../launcher/config.py).

### Servidor dedicado (Proxy público)

- **Primeiro o servidor dedicado, depois as listas.** O app tenta o dconnect ao iniciar; as listas públicas só são
  baixadas se ele estiver fora do ar (ou quando ele cair no meio da sessão).
- **Transporte:** cada conexão do Discord vira um WebSocket por HTTPS até `dconnect.styxx.cloud` (Cloudflare →
  Cloudflare Tunnel → `127.0.0.1:47787` na VPS), com o token no cabeçalho `Authorization` e o destino em
  `X-DPL-Target`. O certificado é conferido como o de qualquer site. A VPS não expõe porta nenhuma.
- **O app decide a rota sozinho** (`destinations.py`, a mesma lista do servidor): domínios do Discord na porta 443
  vão pelo servidor dedicado; o resto (embeds, Spotify, envio de anexos para o Google, *Activities*) sai direto —
  o servidor nem os aceitaria. Uma recusa do servidor para um host do Discord nunca vira conexão direta.
- **O servidor só serve para o Discord:** lista de destinos, nome validado byte a byte, nada de nomes cujo CNAME
  leve para fora da lista, SNI do ClientHello igual ao destino pedido, IPs internos barrados, firewall do kernel
  para o usuário do serviço, frames pequenos com prazo e limites por origem — IPv4, ou IPv6 por /64 e pelo /48
  (detalhes em [server/README.md](../server/README.md)).
- **Falhas:** só contam contra o servidor as que são dele (sem conexão, sem TLS, tempo esgotado); três delas em
  pelo menos 10 s disparam uma verificação na hora. Uma recusa respondida (limite, destino) prova que ele está
  vivo — mas se ele só recusa por 20 s seguidos (cota de um IP compartilhado, por exemplo), para este usuário ele
  não está servindo, e o app passa para os proxies públicos. Se o servidor cair, o app tenta de novo por alguns
  segundos (um reinício rápido não manda ninguém para as listas) e só então passa para os proxies públicos — sem
  fechar o Discord. As respostas são classificadas pelo código de erro do próprio dconnect: um 404 ou 502 da
  Cloudflare (túnel mal configurado, servidor parado) conta como "fora do ar", nunca como "versão recusada".
- **Volta ao servidor dedicado:** usando um proxy público, o app tenta o servidor depois de 2 minutos (dobrando
  até 30 minutos se ele continuar fora) e só volta depois de duas verificações boas, com 30 s entre elas.
- **Versão recusada:** se o servidor não aceitar mais o token desta versão (401/404/410), o app para de tentá-lo
  na sessão e mostra "Atualize o app"; as listas públicas continuam funcionando.
- **Verificação de saúde:** com o servidor dedicado, a de 30 s é o `/health` dele (que não toca no Discord); a VPS
  confere sozinha, uma vez por minuto, se o Discord ainda a atende, e responde 503 se não.
- **O token não está no Git:** fica em `dconnect.json`, que o `build.ps1` embute no `.exe`. Sem esse arquivo o
  `build.ps1` para; com `-NoDedicated` (ou rodando do código-fonte) o app usa só as listas públicas.

### Teste e escolha de proxies (Proxy público)

- **O teste para cedo.** As listas têm de 1.000 a 3.000 entradas; testar tudo a 10 por vez com 5 s de limite
  levaria uns 9 minutos. O teste para ao juntar `TEST_ENOUGH_APPROVED` (6) aprovados — cerca de 20 s — e o resto
  da lista fica guardado para quando a reserva acabar. Com `None`, o app testa a lista inteira.
- **Um proxy só é aprovado se o Discord responde por ele**: uma requisição HTTPS real à API do Discord
  (`discord.com/api/v9/gateway`, precisa dar 200), com o certificado verificado. Um simples teste de conexão
  aprovaria proxies que interceptam TLS ou que o Discord bloqueia.
- **E só se sair fora do Brasil.** Em seguida o app pergunta à Cloudflare, pelo mesmo proxy, de onde ele vem
  (`gateway.discord.gg/cdn-cgi/trace`, campo `loc=`; se esse falhar, `www.cloudflare.com/cdn-cgi/trace`). Proxies
  que saem pelo Brasil — ou cujo país não dá para saber — são recusados. A regra vale só para o modo Proxy
  público: no modo Meu servidor o servidor é do usuário e pode estar em qualquer lugar.
- **A reserva é revalidada antes de virar o proxy ativo.** Proxy gratuito cai a toda hora.
- **O Discord não fala direto com o proxy público.** Ele aponta para o relay (`127.0.0.1`), que manda cada
  conexão pelo proxy ativo. Um proxy aprovado no teste muitas vezes falha minutos — ou segundos — depois, ou
  aguenta uma conexão e não as dezenas que o Discord abre ao carregar; preso a um proxy assim, o Discord ficava
  na tela de carregamento. No relay, se o ativo recusa, a mesma conexão vai na hora para o melhor da reserva;
  se ele não responde em `RELAY_STAGGER` (2,5 s), o próximo corre junto e vale quem responder primeiro.
- **Quem perde para a reserva sai de uso.** Um proxy tentado antes do vencedor de uma conexão leva uma
  "derrota"; `RELAY_MAX_STRIKES` (3) seguidas tiram da fila um proxy da reserva, e, se for o ativo, a sessão
  troca o ativo (`Reconectado via IP:Porta`) sem esperar a verificação de saúde. Quando todos falham, ninguém
  é culpado: pode ser o destino ou a rede do próprio usuário.
- **Trocar de proxy não reinicia o Discord.** A flag dele continua apontando para o relay; só o proxy público
  por trás muda. (A especificação pedia fechar e reabrir o Discord a cada troca; com o relay isso deixou de ser
  necessário.)
- **Queda da rede local durante o teste** não reprova a lista: os proxies voltam para a fila e o teste espera a
  conexão voltar.

### Monitor de saúde

- **A verificação espera uma resposta HTTP** de `1.1.1.1:80`, não só a conexão: o `ssh -D` responde
  "conectado" antes de a conexão com o destino existir.
- **Duas camadas.** Se a verificação em `1.1.1.1:80` falha, o app pergunta se o Discord responde pelo proxy;
  se responde, a conexão é considerada saudável. Um proxy ou servidor que não alcança aquele endereço (ou uma
  queda do próprio `1.1.1.1`) não derruba um Discord que está funcionando.
- **Queda da internet local** (Wi-Fi, notebook voltando da suspensão): o app espera a conexão voltar em vez de
  gastar a fila de proxies ou as três tentativas de reconexão.
- **Vigia do Discord.** Ao se atualizar, o Discord reinicia sem o `--proxy-server`; o monitor percebe e o reabre
  pelo proxy. Se o usuário fechar o Discord, ele fica fechado. Se o Discord reiniciar sem proxy mais de 3 vezes
  em 10 minutos, a sessão termina com erro. Para decidir, o app lê a linha de comando do Discord que está
  rodando: se o próprio Discord se reiniciou mantendo os argumentos (ainda no proxy), o app só passa a
  acompanhar o processo novo, sem derrubá-lo.

### Túnel SSH (Meu servidor)

- **Nada é encerrado antes de o login dar certo.** O Discord só é reiniciado depois que o túnel novo sobe e é
  verificado; um `ssh` antigo na porta 1080 também só é encerrado nesse momento (e só ele, não as outras
  sessões SSH do usuário). Enquanto a 1080 estiver ocupada, o túnel novo usa outra porta local.
- **Chave do servidor.** No login por chave vale o `StrictHostKeyChecking=no` do `ssh.exe`. No login por senha o
  cliente embutido aplica a mesma regra lendo o `known_hosts`: servidor desconhecido é aceito; servidor
  conhecido com outra chave — de qualquer tipo — é recusado antes de a senha ser enviada. Como o app não grava
  nada em disco, as chaves que um servidor apresentou ficam guardadas na memória enquanto o app estiver aberto
  e valem como linhas a mais do `known_hosts` (para as reconexões e para um Parar/Iniciar).
- **Reconexão não insiste no que não é passageiro.** Se, ao reconectar, a chave do servidor for outra ou o
  login for recusado, a sessão termina na hora com o aviso, em vez de gastar as três tentativas e esconder o
  motivo.
- **Login por chave usa o `ssh.exe` do Windows** (só o de `System32\OpenSSH` ou `Program Files\OpenSSH`, nunca
  um achado pelo PATH) com `-F none`: como ele roda com o privilégio de administrador do app, o
  `~/.ssh/config` — que qualquer programa do usuário consegue alterar e que sabe mandar executar comandos — não
  é lido. Apelidos de host e `IdentityFile` do config não valem; host, porta e usuário vêm da tela. Se o
  `ssh.exe` recusar a chave, não existir ou falhar por motivo próprio, o cliente embutido tenta com as chaves
  de `.ssh`.

### Encerramento e privilégios

- **Parar, fechar a janela ou um erro fatal fecham o Discord que o app abriu.** Sem túnel e sem monitor ele
  ficaria preso a um proxy morto, e reabri-lo sem proxy exporia a conexão direta sem o usuário pedir.
- **O Discord nunca é aberto como administrador**, mesmo com o app elevado: ele fica em uma pasta que qualquer
  programa do usuário consegue alterar, e executá-la elevada daria poder de administrador a quem trocasse o
  arquivo.
- **Com o app elevado, quem abre o Discord é o Explorer.** O app pede ao Explorer da área de trabalho que abra
  o Discord (o caminho documentado: `IShellWindows` → área de trabalho → `IShellDispatch2::ShellExecute`), e o
  processo nasce exatamente como num clique no atalho: token, ambiente e permissões vêm do Explorer. O PID é
  achado depois, pelo processo novo com os mesmos argumentos. Até a 1.0.1 o processo era criado pelo próprio
  app elevado (como filho do Explorer ou com um token rebaixado); com o UAC de verdade o Discord travava antes
  da tela de carregamento, e esses dois caminhos ficaram só como reserva, nessa ordem (o do token rebaixado só
  quando a conta é a mesma). Se nada funcionar, o app mostra um erro.
- **Permissões do processo novo.** O Windows protege o processo novo (o processo, a thread e o token dele) com
  o DACL padrão de *quem o cria*. O de um processo elevado é "só Administradores e SYSTEM", e com ele o Discord
  — rodando como usuário comum — não conseguia usar o próprio token: o Chromium dele não criava o processo de
  GPU (`'GPU' process exited with 'launch-failed'`) e o Discord fechava ao abrir (corrigido na 1.0.1).
  Nos caminhos de reserva, enquanto cria o processo, o app passa a usar o DACL padrão do próprio Explorer (ou,
  se não der para lê-lo, um equivalente montado a partir do SID do usuário) e depois volta ao seu. Aberto pelo
  Explorer, nada disso entra em jogo.
- **Elevação com outra conta.** Se o UAC for respondido com a senha de outra conta (ou com a "Proteção de
  administrador" do Windows 11), o app localiza o perfil de quem está na área de trabalho pelo Registro. Esse
  caminho foi escrito e testado por partes, mas não de ponta a ponta.

### Textos da linha de status

- Progresso do teste: `Testando proxies... (X/Y) — N aprovados` (junta os dois formatos da especificação).
- Meu servidor: `Proxy ativo — host:porta (Xms)` mostra o servidor SSH (o proxy de fato é `127.0.0.1`), e a
  queda aparece como `Instabilidade detectada — reconectando o túnel SSH...`, porque nesse modo não há troca de
  proxy.
- Textos a mais: `Sem internet — aguardando a conexão voltar...` e `O Discord reiniciou sem o proxy — reabrindo...`.
- Proxy público: o país de saída aparece junto do proxy (`Proxy ativo — 1.2.3.4:1080 · EUA (350ms)`), e o servidor
  dedicado aparece como `Proxy ativo — servidor dedicado de pedrostyxx · França (Xms)`.

## Compilar

Requer Python 3.12+ no PATH.

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1               # build oficial: precisa do dconnect.json
powershell -ExecutionPolicy Bypass -File build.ps1 -NoDedicated  # sem o servidor dedicado (só as listas)
```

O script cria o `.venv`, instala as dependências de `requirements.txt`, gera o ícone e roda o PyInstaller
(`--onefile --windowed --uac-admin`). O resultado é `dist\discord-proxy-launcher.exe`. O `dconnect.json` (endereço
e token do servidor dedicado) não fica no Git — veja [server/README.md](../server/README.md); sem ele e sem
`-NoDedicated`, o script para antes de compilar.

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Os testes não tocam no Discord verdadeiro, nas suas chaves SSH nem na porta 1080: usam um servidor SSH local
(`tests/fakes.py`), uma porta SOCKS aleatória e um "Discord falso" — uma cópia do `pythonw.exe` assinado, com
outro nome, que só anota como foi aberto.

- `tests/test_dconnect_server.py` roda o servidor dedicado aqui mesmo, com um "Discord" TLS local: token,
  destinos, SNI, IPs internos, limites e frames.
- `DPL_NETWORK_TESTS=1` habilita o teste do modo Proxy público com as listas e os proxies públicos de verdade.
- `python tests/check_exe.py` confere o `.exe` compilado: autoteste embutido, janela e instância única (roda sem
  elevação, sem abrir o UAC).
- `python tests/probe_public.py` mede a taxa de aprovação das listas reais.
- `python tests/ui_snapshot.py <pasta> [escala]` gera imagens de cada tela.
- A opção `--log` (ou a variável de ambiente `DPL_DEBUG_LOG=1`) liga um log de diagnóstico, gravado como
  `discord-proxy-launcher.log` na pasta do executável (senhas nunca são registradas; no modo Meu servidor, o
  endereço e a porta do servidor aparecem, e o usuário em algumas mensagens do ssh). Com o app elevado vale só a
  opção: o UAC não repassa as variáveis de ambiente.
