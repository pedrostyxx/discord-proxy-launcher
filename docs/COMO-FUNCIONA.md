# Como o Discord Proxy Launcher funciona

Detalhes internos para quem quer entender, auditar ou modificar o app. Para o uso do dia a dia, veja o
[README](../README.md). A especificação original do projeto está em [ESPECIFICACAO.md](ESPECIFICACAO.md).

## Visão geral

1. O app localiza o Discord instalado: `%LOCALAPPDATA%\Discord\app-X.X.XXX\Discord.exe`, na pasta da versão mais
   recente (também procura o Discord PTB e o Canary).
2. Prepara um proxy SOCKS5:
   - **Meu servidor:** um túnel SSH com encaminhamento dinâmico (o equivalente a `ssh -D 1080 -N`) escutando em
     `127.0.0.1:1080`.
   - **Proxy público:** um proxy de uma das listas gratuitas, já testado.
3. Fecha o Discord e o reabre com `--proxy-server=socks5://IP:PORTA`, a opção do Chromium que faz **só aquele
   processo** usar o proxy. Nada é configurado no Windows.
4. Fica monitorando: a cada 30 s, uma conexão pelo proxy até `1.1.1.1:80`. Duas falhas seguidas disparam a
   reconexão do túnel (Meu servidor) ou a troca de proxy (Proxy público).

As listas de proxies são consultadas nesta ordem, até uma responder: ProxyScrape, TheSpeedX no GitHub e a mesma
lista pelo jsDelivr.

## O que passa pelo proxy

O `--proxy-server` vale para a pilha de rede do Chromium dentro do Discord: login, API, gateway (WebSocket),
mensagens, imagens e anexos. O áudio e o vídeo das chamadas, das câmeras e do compartilhamento de tela usam o
módulo de voz nativo do Discord, que manda a mídia por UDP direto aos servidores de voz, fora do proxy. Por isso
as chamadas não sofrem com a latência do proxy, e, nelas, o servidor de voz vê o IP real.

## Código

| Arquivo | Papel |
|---|---|
| `main.py` | ponto de entrada |
| `launcher/config.py` | os números da especificação (portas, intervalos, fontes de proxy) |
| `launcher/ui.py` | interface tkinter |
| `launcher/session.py` | fluxo de início, monitor de saúde e troca automática nos dois modos |
| `launcher/tunnel.py` | túnel SSH: `ssh.exe` do Windows (chave) e cliente embutido Paramiko + servidor SOCKS5 (senha) |
| `launcher/proxies.py` | download das listas, teste de proxy e fila de reserva |
| `launcher/socks5.py` | cliente SOCKS5 mínimo e a verificação de saúde |
| `launcher/discord.py` | localizar, fechar e abrir o Discord com `--proxy-server` |
| `launcher/win.py` | chamadas à API do Windows via ctypes (processos, portas, início sem elevação) |
| `launcher/target.py` | validação dos campos do modo Meu servidor |
| `launcher/selftest.py` | autoteste do executável (`--selftest`) |

## Decisões de projeto

Os números estão todos em [`launcher/config.py`](../launcher/config.py).

### Teste e escolha de proxies (Proxy público)

- **O teste para cedo.** As listas têm de 1.000 a 3.000 entradas; testar tudo a 10 por vez com 5 s de limite
  levaria uns 9 minutos. O teste para ao juntar `TEST_ENOUGH_APPROVED` (6) aprovados — cerca de 20 s — e o resto
  da lista fica guardado para quando a reserva acabar. Com `None`, o app testa a lista inteira.
- **Um proxy só é aprovado se o Discord responde por ele**: uma requisição HTTPS real a `discord.com`, com o
  certificado verificado. Um simples teste de conexão aprovaria proxies que interceptam TLS ou que o Discord
  bloqueia.
- **A reserva é revalidada antes do uso.** Proxy gratuito cai a toda hora; o Discord só é reiniciado por um
  proxy que acabou de passar no teste.
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
  arquivo. O app o inicia como filho do Explorer; se isso não for possível, com um token rebaixado do próprio
  app (só quando a conta é a mesma); se nem isso, mostra um erro.
- **Permissões do processo novo.** O Windows protege o processo novo (o processo, a thread e o token dele) com
  o DACL padrão de *quem o cria*. O de um processo elevado é "só Administradores e SYSTEM", e com ele o Discord
  — rodando como usuário comum — não conseguia usar o próprio token: o Chromium dele não criava o processo de
  GPU (`'GPU' process exited with 'launch-failed'`) e o Discord fechava ao abrir (corrigido na 1.0.1).
  Enquanto cria o processo, o app passa a usar o DACL padrão do próprio Explorer (ou, se não der para lê-lo,
  um equivalente montado a partir do SID do usuário) e depois volta ao seu.
- **Elevação com outra conta.** Se o UAC for respondido com a senha de outra conta (ou com a "Proteção de
  administrador" do Windows 11), o app localiza o perfil de quem está na área de trabalho pelo Registro. Esse
  caminho foi escrito e testado por partes, mas não de ponta a ponta.

### Textos da linha de status

- Progresso do teste: `Testando proxies... (X/Y) — N aprovados` (junta os dois formatos da especificação).
- Meu servidor: `Proxy ativo — host:porta (Xms)` mostra o servidor SSH (o proxy de fato é `127.0.0.1`), e a
  queda aparece como `Instabilidade detectada — reconectando o túnel SSH...`, porque nesse modo não há troca de
  proxy.
- Textos a mais: `Sem internet — aguardando a conexão voltar...` e `O Discord reiniciou sem o proxy — reabrindo...`.

## Compilar

Requer Python 3.12+ no PATH.

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

O script cria o `.venv`, instala as dependências de `requirements.txt`, gera o ícone e roda o PyInstaller
(`--onefile --windowed --uac-admin`). O resultado é `dist\discord-proxy-launcher.exe`.

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Os testes não tocam no Discord verdadeiro, nas suas chaves SSH nem na porta 1080: usam um servidor SSH local
(`tests/fakes.py`), uma porta SOCKS aleatória e um "Discord falso" — uma cópia do `pythonw.exe` assinado, com
outro nome, que só anota como foi aberto.

- `DPL_NETWORK_TESTS=1` habilita o teste do modo Proxy público com as listas e os proxies públicos de verdade.
- `python tests/check_exe.py` confere o `.exe` compilado: autoteste embutido, janela e instância única (roda sem
  elevação, sem abrir o UAC).
- `python tests/probe_public.py` mede a taxa de aprovação das listas reais.
- `python tests/ui_snapshot.py <pasta> [escala]` gera imagens de cada tela.
- A variável de ambiente `DPL_DEBUG_LOG=1` liga um log de diagnóstico, gravado como
  `discord-proxy-launcher.log` na pasta do executável (senhas nunca são registradas).
