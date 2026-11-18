<p align="center">
  <img src="docs/images/icone.png" width="96" height="96" alt="">
</p>

<h1 align="center">Discord Proxy Launcher</h1>

<p align="center">
  Abre o Discord através de um proxy — pelo seu próprio servidor ou por proxies gratuitos —<br>
  sem VPN, sem instalar nada e sem mexer no resto do computador.
</p>

<p align="center">
  <a href="https://github.com/pedrostyxx/discord-proxy-launcher/releases/latest/download/discord-proxy-launcher.exe">
    <img src="docs/images/botao-download.png" width="355" alt="Baixar o discord-proxy-launcher.exe para Windows">
  </a>
  <br>
  <sub>Windows 10 e 11 (64 bits) · arquivo único, sem instalação · <a href="https://github.com/pedrostyxx/discord-proxy-launcher/releases">todas as versões</a></sub>
</p>

<p align="center">
  <img src="docs/images/janela.png" width="446" alt="Tela inicial do app, com os botões Meu servidor e Proxy público">
</p>

## Por que este app existe

Em algumas redes o Discord não funciona direito: fica preso em "Conectando...", cai a toda hora, fica lento ou
é simplesmente bloqueado — pela operadora, pela rede da empresa ou da faculdade, ou num Wi-Fi público.

A saída de costume é uma VPN, mas ela cobra um preço: desvia **todo** o tráfego do computador (e deixa o resto
da internet mais lento), precisa ser instalada e quase sempre é paga.

O **Discord Proxy Launcher** resolve só o que precisa ser resolvido: ele coloca **apenas o Discord** atrás de um
proxy. Não há nada para configurar — com um clique o app prepara o proxy, abre o Discord, fica de olho na
conexão e troca de proxy sozinho se ela cair.

## O que ele faz

Ao abrir, você escolhe um de dois modos:

|  | 🖥️ **Meu servidor** | 🌐 **Proxy público** |
|---|---|---|
| **Para quem** | tem um VPS ou servidor com SSH | não tem servidor |
| **O que você preenche** | o IP do servidor (porta, usuário e senha são opcionais) | nada |
| **Como funciona** | cria um túnel SSH até o seu servidor e passa o Discord por ele | baixa listas de proxies SOCKS5 gratuitos, testa vários ao mesmo tempo e usa o mais rápido |
| **Se a conexão cair** | reconecta o túnel sozinho (até 3 tentativas) | passa para o próximo proxy da fila e reabre o Discord em poucos segundos |
| **Estabilidade** | a do seu servidor — a melhor opção | proxies gratuitos caem com frequência; o app troca sozinho |

Nos dois modos:

- **Monitor de conexão.** A cada 30 segundos o app confere se o proxy continua respondendo. Duas falhas
  seguidas disparam a reconexão ou a troca de proxy. Se quem caiu foi a sua internet (Wi-Fi, notebook voltando
  da suspensão), ele espera a conexão voltar em vez de trocar de proxy à toa.
- **Discord sempre no proxy.** Se o Discord se atualizar e reiniciar sozinho (voltando sem o proxy), o app
  percebe e o reabre pelo proxy.
- **Status sempre à vista.** Uma linha colorida no rodapé mostra o que está acontecendo:
  🔵 conectando · 🟡 testando proxies · 🟢 proxy ativo · 🟠 trocando proxy · 🔴 erro · ⚪ desconectado.

## Só o Discord passa pelo proxy

O app **não** liga o proxy do Windows, não cria VPN e não altera nenhuma configuração do sistema. Ele abre o
Discord com a opção `--proxy-server` do próprio Discord (que é feito em Electron/Chromium), e essa opção vale
**só para o Discord**:

- navegador, jogos, downloads e todos os outros programas continuam usando a sua conexão normal, na velocidade
  de sempre;
- ao clicar em **Parar** (ou fechar o app), o Discord aberto por ele é fechado; da próxima vez que você o abrir
  pelo atalho, ele volta ao normal, sem proxy.

## Chamadas e compartilhamento de tela funcionam normalmente

O proxy leva o "Discord de texto": login, mensagens, servidores, canais, imagens e o carregamento do app. Já o
áudio e o vídeo das **chamadas de voz, das câmeras e do compartilhamento de tela (Go Live)** seguem pela conexão
de mídia do próprio Discord, direto para os servidores de voz.

Na prática: **chamadas e compartilhamento de tela funcionam tranquilamente**, sem ficar lentos ou travando por
causa do proxy — mesmo com os proxies gratuitos.

> **Bom saber:** como as chamadas não passam pelo proxy, os servidores de voz do Discord veem o seu IP real
> durante uma chamada. Se o seu objetivo for esconder o IP, o proxy cobre o resto do Discord, mas não as chamadas.

## Como usar

1. **[Baixe o `discord-proxy-launcher.exe`](https://github.com/pedrostyxx/discord-proxy-launcher/releases/latest/download/discord-proxy-launcher.exe).**
   Não tem instalação: é um arquivo só.
2. **Abra o arquivo** e aceite o pedido de permissão do Windows — o app pede para rodar como administrador.
   - Se aparecer *"O Windows protegeu o computador"*, clique em **Mais informações → Executar assim mesmo**. O
     aviso aparece porque o executável ainda não tem assinatura digital (veja [Limitações](#limitações)).
3. **Escolha o modo:**
   - **Meu servidor:** digite o IP (ou o endereço) do servidor. A porta (22) e o usuário (root) já vêm
     preenchidos. Informe a senha ou deixe em branco para usar a sua chave SSH (`id_ed25519`, `id_ecdsa` ou
     `id_rsa` na pasta `.ssh` do seu usuário).
   - **Proxy público:** é só clicar em **Iniciar**. O app baixa a lista, testa os proxies e abre o Discord com o
     melhor em cerca de 15 a 30 segundos.
4. **Pronto.** O Discord abre pelo proxy. Deixe o app aberto (pode minimizar) — é ele quem vigia a conexão. Para
   voltar ao normal, clique em **Parar**.

## Telas

<p align="center">
  <img src="docs/images/telas.png" alt="Meu servidor: formulário preenchido e conectado. Proxy público: testando proxies e conectado.">
</p>

<p align="center">
  <img src="docs/images/outras-telas.png" alt="Proxy público em espera e trocando de proxy; Meu servidor conectando e mostrando um erro.">
</p>

## Requisitos

- Windows 10 ou 11, 64 bits.
- Discord para desktop instalado (versão normal, PTB ou Canary — o app encontra a instalação sozinho).
- Para o modo **Meu servidor**: um servidor com SSH que permita encaminhamento de portas
  (`AllowTcpForwarding yes`, o padrão do OpenSSH).

## Perguntas frequentes

<details>
<summary><b>Isso é uma VPN?</b></summary>
<br>

Não. Uma VPN muda a rota de todo o computador. Aqui só o Discord usa o proxy; o resto continua igual.
</details>

<details>
<summary><b>O app guarda minha senha ou algum dado?</b></summary>
<br>

Não. Nada é gravado em disco: a senha SSH fica só na memória enquanto o app está aberto, e o app não cria
configurações, serviços nem arquivos de dados.
</details>

<details>
<summary><b>Usar proxy público é seguro?</b></summary>
<br>

O tráfego do Discord é criptografado (HTTPS), então o dono do proxy não consegue ler suas mensagens nem sua
senha — e o app só aceita um proxy depois de conferir, através dele, o certificado do Discord. O dono do proxy
consegue ver que você está usando o Discord. Para mais privacidade e estabilidade, use o modo **Meu servidor**.
</details>

<details>
<summary><b>Por que o app pede para rodar como administrador?</b></summary>
<br>

Para conseguir fechar e reabrir o Discord e túneis SSH antigos mesmo quando eles foram abertos com privilégio
elevado. O Discord em si é sempre aberto como usuário comum, nunca como administrador.
</details>

<details>
<summary><b>O Discord se atualizou e voltou sem o proxy. E agora?</b></summary>
<br>

Nada a fazer: o app percebe em poucos segundos e reabre o Discord pelo proxy.
</details>

<details>
<summary><b>Dá para usar o meu servidor com chave SSH em vez de senha?</b></summary>
<br>

Sim. Deixe a senha em branco: o app usa o cliente SSH do próprio Windows com a sua chave padrão e, se ele não
estiver disponível, um cliente SSH embutido.
</details>

## Segurança

- **Nada vai para o disco** — nem senha, nem lista de proxies, nem log.
- **O Discord nunca roda como administrador**, mesmo com o app elevado.
- **A chave do servidor SSH é conferida** contra o seu `known_hosts`: se o servidor apresentar uma chave
  diferente da registrada, o app recusa a conexão antes de enviar a senha.
- **Proxies públicos são testados de verdade:** só entram na fila os que entregam uma resposta HTTPS válida do
  Discord, com o certificado conferido.

## Limitações

- **Sem assinatura digital.** O Windows mostra o aviso do SmartScreen na primeira execução
  (*Mais informações → Executar assim mesmo*). Em PCs com o **Controle Inteligente de Aplicativos** (Smart App
  Control) ligado, o Windows pode bloquear o executável.
- **Proxies gratuitos são instáveis:** podem ser lentos e cair a qualquer momento. O app troca sozinho, mas cada
  troca fecha e reabre o Discord por alguns segundos.
- **IP nas chamadas:** voz, vídeo e compartilhamento de tela não passam pelo proxy (veja acima).

## Para desenvolvedores

O app é feito em Python 3.12 com tkinter e compilado com PyInstaller em um `.exe` único.

```powershell
# compilar: cria o .venv, instala as dependências e gera dist\discord-proxy-launcher.exe
powershell -ExecutionPolicy Bypass -File build.ps1

# rodar os testes
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Os testes não tocam no seu Discord, nas suas chaves SSH nem na porta 1080. O funcionamento interno, as decisões
de projeto e as ferramentas de teste estão em **[docs/COMO-FUNCIONA.md](docs/COMO-FUNCIONA.md)**; a
especificação original está em [docs/ESPECIFICACAO.md](docs/ESPECIFICACAO.md).
