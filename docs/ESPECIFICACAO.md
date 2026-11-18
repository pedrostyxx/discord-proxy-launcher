# Spec: Discord Proxy Launcher

## Objetivo

App para Windows que qualquer pessoa consegue usar sem instalar nada. Na tela inicial, o usuário escolhe entre duas opções:

- **Meu servidor** — informa o IP de um servidor SSH próprio
- **Proxy público** — o app busca, testa e mantém proxies SOCKS5 gratuitos automaticamente

O app cuida de tudo: configura o proxy, lança o Discord, monitora a conexão e troca de proxy automaticamente se detectar instabilidade.

---

## Distribuição — sem instalação

O entregável é um **único arquivo `.exe`** compilado (Python + PyInstaller ou Node.js + pkg). O usuário baixa, clica com botão direito → "Executar como administrador" e pronto. Nenhuma instalação de Python, Node.js ou qualquer dependência.

Requisito mínimo: Windows 10 64-bit.

---

## Contexto técnico (validado e funcionando)

O Discord é um app Electron (Chromium) e aceita a flag `--proxy-server` na inicialização:

```
Discord.exe --proxy-server=socks5://IP:PORTA
```

Caminho do Discord.exe (detectado automaticamente pela versão mais recente instalada):
```
%LOCALAPPDATA%\Discord\app-X.X.XXX\Discord.exe
```

---

## Interface

### Tela inicial — escolha do modo

Dois botões grandes, bem visíveis:

```
┌─────────────────────────────────────────┐
│                                         │
│   [ 🖥️  Meu servidor ]                  │
│   Tenho um VPS ou servidor SSH          │
│                                         │
│   [ 🌐  Proxy público ]                  │
│   Não tenho servidor, usar lista grátis │
│                                         │
└─────────────────────────────────────────┘
```

---

### Modo A — Meu servidor (SSH)

Campos:

| Campo | Obrigatório | Padrão |
|---|---|---|
| IP / Host do servidor | Sim | — |
| Porta SSH | Não | 22 |
| Usuário SSH | Não | root |
| Senha SSH | Não | (usa chave SSH se vazio) |

Botões: **Iniciar** / **Parar**

**Fluxo ao iniciar:**

1. Fechar processos existentes de Discord e ssh
2. Iniciar túnel SSH: `ssh -D 1080 -N -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes usuario@host`
   - Com senha: via biblioteca SSH nativa (Paramiko no Python)
   - Sem senha: usar chave SSH padrão (`%USERPROFILE%\.ssh\id_rsa`)
3. Aguardar até a porta 1080 ouvir (timeout: 10s)
4. Se falhar: exibir erro e encerrar o processo ssh
5. Se funcionar: abrir Discord com `--proxy-server=socks5://127.0.0.1:1080`
6. Iniciar monitor de saúde (ver seção abaixo)

---

### Modo B — Proxy público (lista gratuita)

**Aviso exibido antes de iniciar** (caixa de diálogo ou banner fixo na tela):

> ⚠️ **Proxies gratuitos podem ser instáveis.**
> O app monitora a conexão e troca de proxy automaticamente se detectar queda, mas você pode perceber uma breve interrupção no Discord durante a troca.

Sem campos para preencher. Botões: **Iniciar** / **Parar**

**Fluxo ao iniciar:**

1. Baixar lista de proxies SOCKS5 (ver fontes abaixo)
2. Testar todos em paralelo (máx. 10 simultâneos, timeout 5s por proxy)
3. Exibir progresso: "Testando proxies... (X/Y)"
4. Montar fila ordenada por latência dos que passaram no teste
5. Usar o melhor (menor latência) → abrir Discord com esse proxy
6. Manter os demais aprovados em standby para failover rápido
7. Iniciar monitor de saúde

---

## Monitor de saúde (ambos os modos)

Executar em background a cada **30 segundos**:

1. Tentar conexão TCP pelo proxy ativo para `1.1.1.1:80` (timeout: 5s)
2. Se 2 falhas consecutivas:
   - Exibir status: "Instabilidade detectada — trocando proxy..."
   - **Modo A (SSH):** tentar reconectar o túnel SSH; se falhar 3x, exibir erro permanente
   - **Modo B (público):** pegar o próximo proxy da fila standby, fechar Discord, reabrir com novo proxy, exibir "Reconectado via [IP:Porta]"
3. Se a fila standby esvaziar no Modo B: baixar nova lista e testar novamente

A troca de proxy no Modo B fecha e reabre o Discord automaticamente. O usuário verá o Discord fechar por ~3 segundos e reabrir.

---

## Fontes de proxies públicos

Tentar em ordem até obter lista válida:

1. **ProxyScrape** (API gratuita):
   ```
   https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks5&timeout=5000&country=all&ssl=all&anonymity=all
   ```

2. **GitHub TheSpeedX** (fallback):
   ```
   https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt
   ```

3. **jsDelivr CDN** (fallback 2):
   ```
   https://cdn.jsdelivr.net/gh/TheSpeedX/PROXY-List@master/socks5.txt
   ```

Formato das listas: `IP:PORTA` por linha.

---

## Área de status

Uma linha de texto com ícone colorido, sempre visível:

| Ícone | Texto |
|---|---|
| 🔵 | Conectando... |
| 🟡 | Testando proxies... (X/Y aprovados) |
| 🟢 | Proxy ativo — `IP:Porta` (Xms) |
| 🟠 | Instabilidade — trocando proxy... |
| 🔴 | Erro: [mensagem curta] |
| ⚪ | Desconectado |

---

## Requisitos técnicos

- **Plataforma:** Windows 10/11 64-bit
- **Distribuição:** `.exe` único compilado — sem instalação de dependências pelo usuário
- **Linguagem sugerida:** Python 3.x compilado com PyInstaller (`--onefile`)
  - Dependências internas: `paramiko` (SSH), `tkinter` (UI nativa), `socket`/`socks` (teste de proxy)
- O app solicita elevação UAC automaticamente ao abrir (necessário para SSH funcionar corretamente)
- Detectar automaticamente o Discord.exe mais recente instalado

---

## O que NÃO fazer

- Não usar IPsec, WireGuard, OpenVPN ou qualquer VPN
- Não instalar drivers, serviços do Windows ou software adicional
- Não modificar configurações do sistema operacional
- Não rotear nenhum tráfego além do Discord
- Não armazenar senhas ou dados do usuário em disco

---

## Entregável

- `discord-proxy-launcher.exe` — executável único, sem instalação
- Usuário executa como Administrador (botão direito → "Executar como administrador")
