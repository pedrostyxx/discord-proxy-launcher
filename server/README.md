# dconnect — o servidor dedicado do app

O **dconnect** é o servidor que o modo *Proxy público* do app usa antes das listas gratuitas. Ele roda numa VPS
fora do Brasil, atrás de um **Cloudflare Tunnel**, e só atende o app — e só para o Discord.

```
Discord ──► proxy local do app (127.0.0.1) ──► wss://dconnect.styxx.cloud ──► Cloudflare
        ──► cloudflared (VPS) ──► dconnect (127.0.0.1:47787) ──► discord.com, gateway.discord.gg...
```

## Por que ele não serve para outra coisa

O token vai dentro de cada `.exe` e pode ser extraído, então nenhuma proteção depende só dele:

| Camada | O que barra |
|---|---|
| Cloudflare Tunnel | O dconnect não abre porta nenhuma, e `dconnect.styxx.cloud` aponta para a Cloudflare, não para a VPS. Mas, como em qualquer proxy, quem tiver o token pode descobrir o IP de saída (veja abaixo) |
| Caminho aleatório + token | Varreduras: qualquer outra coisa recebe 404 |
| Lista de destinos | Só domínios do Discord (`discord.com`, `discord.gg`, `discordapp.com`, `discordapp.net`, `discord.media`, `hcaptcha.com`), só a porta 443; o nome é validado byte a byte |
| Nomes que apontam para fora | Um nome da lista cujo CNAME leva a outro lugar (os sites de suporte no Zendesk, a página de status do hCaptcha) é recusado: nessas plataformas o mesmo IP atende outros clientes |
| Conferência do SNI | O primeiro pacote precisa ser um TLS ClientHello para exatamente o host pedido — os IPs do Discord são da Cloudflare e serviriam qualquer outro site da Cloudflare pelo SNI |
| Filtro de IP | Nada de redes internas, da própria VPS, NAT64, metadata de nuvem |
| Firewall do kernel (`firewall.sh`) | O usuário `dconnect` (uid 47787, se estiver livre) só abre conexões HTTPS para a internet e DNS — nada da própria máquina (bancos, Redis, Docker, VPN), nenhuma outra porta. Refeito a cada início do serviço e conferido a cada 5 minutos (`dconnect-firewall.timer`) |
| systemd | Serviço sem privilégios, sistema de arquivos só leitura, sem capacidades, memória limitada (2 GB) |
| Limites por IP de origem | Conexões novas por segundo, simultâneas, de gateway (tudo em `discord.gg`: fazendas de bots) e bytes por segundo e por dia (a cota encerra até túneis já abertos). No IPv6, por /64 e também pelo /48 inteiro |
| Limites de protocolo | Frames de até 64 KiB, que precisam terminar em 30 s; o ClientHello em no máximo 16 frames e 4 registros TLS |

O servidor não grava destinos nem conteúdo. O log (`journalctl -u dconnect`) só tem números agregados a cada 10
minutos e avisos (por exemplo, se o Discord parar de atender a VPS).

## Instalação

Na VPS, como root, com os arquivos desta pasta:

```bash
bash install.sh --public-host dconnect.styxx.cloud --port 47787
```

O instalador cria o usuário `dconnect` (uid 47787; se ele estiver ocupado, avisa e usa um uid automático, e uma
instalação antiga com outro uid só é migrada quando o 47787 está livre), copia o código
para `/opt/dconnect`, cria **uma única vez** `/etc/dconnect/config.json` (token e caminho aleatórios), instala a
unidade `dconnect` e o timer `dconnect-firewall`, libera os resolvedores DNS do `/etc/resolv.conf` na unidade
(`dconnect.service.d/resolvers.conf`) e faz um teste de ponta a ponta local (que precisa terminar com
`saída FR`, ou o país da VPS).

### O túnel da Cloudflare

O túnel do dconnect tem a própria pasta e a própria unidade:

```bash
export HOME=/root/.dconnect-cf              # pasta própria para o login (cert.pem) deste túnel
cloudflared tunnel login                    # abra o link e autorize o domínio styxx.cloud
cloudflared tunnel create dconnect
cloudflared tunnel route dns <ID-do-túnel> dconnect.styxx.cloud
```

`/etc/cloudflared/dconnect/config.yml`:

```yaml
tunnel: <ID-do-túnel>
credentials-file: /etc/cloudflared/dconnect/<ID-do-túnel>.json
ingress:
  - hostname: dconnect.styxx.cloud
    service: http://127.0.0.1:47787
  - service: http_status:404
```

E a unidade `cloudflared-dconnect.service` (`cloudflared tunnel --config ... --no-autoupdate run dconnect`).

### Ligar o app ao servidor

O build do app precisa do `dconnect.json` (endereço + token) na raiz do projeto — ele está no `.gitignore` e
**nunca** vai para o GitHub:

```bash
python3 -I /opt/dconnect/dconnect_server.py --config /etc/dconnect/config.json --client-config
```

Copie a saída para `dconnect.json` e rode `build.ps1`. Sem esse arquivo o build para (use `-NoDedicated` para
compilar uma versão só com as listas públicas).

## Manutenção

- **Atualizar o código:** copie os arquivos desta pasta e rode `bash install.sh` de novo. O token e o caminho
  ficam como estão; os apps já distribuídos continuam funcionando. Reiniciar o serviço derruba as conexões abertas
  e deixa o servidor fora por uns 4 segundos (o app reconecta sozinho, sem trocar para as listas).
- **Ver o estado:** `systemctl status dconnect cloudflared-dconnect` e `journalctl -u dconnect -n 50`. As regras
  de saída: `iptables -S DCONNECT_OUT`; a conferência periódica: `journalctl -u dconnect-firewall`.
- **Teste local:** `python3 -I /opt/dconnect/dconnect_server.py --config /etc/dconnect/config.json --check`.
- **Trocar o token** (por exemplo, se uma versão do app estiver sendo abusada). O `config.json` só é lido quando o
  serviço inicia, e o `--client-config` exporta o **primeiro** token da lista:
  1. Ponha o token novo **no começo** de `tokens`: `{"label": "app-2", "token": "<32+ caracteres>"}` (gere com
     `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), e rode `systemctl restart dconnect`.
  2. Gere o `dconnect.json` com `--client-config`, compile e publique o app.
  3. Quando quiser desligar a versão antiga, troque a entrada dela em `tokens` por
     `{"label": "app-1", "sha256": "<hash>"}` dentro de `retired_tokens` — o hash é só o texto hexadecimal de
     `printf %s TOKEN_ANTIGO | sha256sum`, sem o `  -` do final — e rode `systemctl restart dconnect` de novo.

  Apps com o token aposentado recebem "atualize o app" e passam a usar as listas públicas.
- **Backups:** cada execução do instalador guarda uma cópia da configuração em `/var/backups/dconnect`. Sem ela,
  uma VPS nova teria outro token — e todos os apps distribuídos deixariam de usar o servidor dedicado.
- **Desligar:** `systemctl stop cloudflared-dconnect`. O app passa sozinho para as listas públicas, sem fechar o
  Discord: quem abrir o app vai direto para elas; quem já está conectado troca em cerca de um minuto, depois de
  tentar o servidor de novo por uns 15 s.

## O IP da VPS

O endereço público é da Cloudflare, mas o IP de saída da VPS não é segredo para quem usa o servidor: com o token
(que vai dentro de cada `.exe`), qualquer um consegue abrir um túnel até um host do Discord e perguntar à própria
Cloudflare de onde a conexão vem (`/cdn-cgi/trace`). Não há como o servidor impedir isso, porque o pedido viaja
dentro do TLS. Então rode o dconnect numa máquina — ou com um IP de saída — que possa ficar ligada a ele; para não
expor uma máquina com outros serviços, use uma VPS só para ele (os apps não guardam o IP: mudar de máquina é copiar
o `config.json` e apontar o túnel para a nova).

## O que fica visível para o dono do servidor

O servidor vê o IP de quem usa o app e quais hosts do Discord são acessados — **nunca o conteúdo**, que continua
criptografado de ponta a ponta entre o Discord do usuário e o Discord. Nada disso é gravado por padrão (o log só
tem totais). A Cloudflare, no meio do caminho, também vê o IP de quem usa o app e o destino pedido (o cabeçalho
`X-DPL-Target`). Quem oferece um proxy público deve se informar sobre as próprias obrigações (Marco Civil da
Internet, LGPD) e sobre os termos de uso da Cloudflare e do provedor da VPS.
