# SimIED — Simulador de IEDs IEC 61850

O SimIED cria **IEDs virtuais a partir do CID real de um relé**. Cada IED simulado escuta num IP próprio da
máquina, na porta 102, fala **MMS (IEC 61850-8-1)** e se comporta como o relé descrito no CID: modelo de dados,
DataSets, RCBs (BRCB/URCB), serviço de controle e as particularidades do fabricante (perfil). Ele foi feito para
**testar o SAGE** (e outros SCADAs) sem depender do relé físico: subir pontos, injetar eventos e falhas de
comunicação, reproduzir recusas de RCB, simular o relógio errado e registrar ensaios.

Tudo é operado por uma **interface web** (e por uma **API REST** equivalente), com login local ou pelo LDAP da
rede.

> ⚠️ **Os dados enviados pelo simulador não vêm de relés reais.** Um SCADA que apontar para o IP de um IED
> simulado recebe dados **simulados**, e os eventos injetados aparecem para o operador e entram nos arquivos
> `.alr` e `.sde` reais do SAGE. Use apenas IEDs e pontos reservados para teste, em janela combinada com a COT.
> O botão **■ PARADA RÁPIDA**, sempre visível no topo da tela, desfaz tudo de uma vez (veja
> [Parada rápida](#parada-rápida)).

---

## Sumário

1. [Recursos](#recursos)
2. [Requisitos](#requisitos)
3. [Instalação](#instalação)
4. [Configuração (`simied.conf`)](#configuração-simiedconf)
5. [Usuários, perfis de acesso e LDAP](#usuários-perfis-de-acesso-e-ldap)
6. [Rede: IP da interface web e IPs dos IEDs](#rede-ip-da-interface-web-e-ips-dos-ieds)
7. [Primeiros passos](#primeiros-passos)
8. [Manual da interface](#manual-da-interface)
   - [Barra superior e parada rápida](#barra-superior-e-parada-rápida)
   - [IEDs](#ieds)
   - [Página do IED](#página-do-ied) — Painel, Sinais digitais, Sinais analógicos, Vínculos, RCBs e DataSets,
     Comandos, Lógicas, Subida de pontos, Reports e histórico, Falhas
   - [Rede](#rede)
   - [Perfis](#perfis)
   - [Cenários](#cenários)
   - [Ensaios](#ensaios)
   - [Visualizador de CID](#visualizador-de-cid)
   - [Editor de CID](#editor-de-cid)
   - [OPMSK](#opmsk)
   - [Configuração](#configuração)
9. [API REST e WebSocket](#api-rest-e-websocket)
10. [Linha de comando](#linha-de-comando)
11. [Dados, persistência e backup](#dados-persistência-e-backup)
12. [Testes automáticos](#testes-automáticos)
13. [Estrutura do projeto](#estrutura-do-projeto)
14. [Solução de problemas](#solução-de-problemas)
15. [Glossário](#glossário)

---

## Recursos

- **Importação de CID, ICD, SCD e IID** (SCL Edição 1 e 2). Num SCD, escolhe-se qual IED do arquivo simular.
  Um mesmo CID pode virar vários IEDs, com nomes e IPs diferentes.
- **Servidor MMS próprio** (TPKT, COTP, sessão, apresentação, ACSE e MMS), um por IED: Initiate, GetNameList,
  GetVariableAccessAttributes, Read, Write, GetNamedVariableListAttributes, Define/DeleteNamedVariableList
  (conforme o perfil), Information Reports e Conclude.
- **Reports BRCB e URCB** conforme IEC 61850-7-2/8-1: TrgOps (dchg, qchg, dupd, period, gi), OptFields,
  BufTm, IntgPd, EntryID, buffer, reserva por cliente e ConfRev.
- **Política de acesso por RCB** (livre, ocupado, negado, somente leitura, com erros por atributo e lista de
  IPs), para reproduzir relés como o Ingeteam i15, em que o SAGE só consegue usar o RCB dele.
- **Sinais digitais e analógicos** editáveis com qualidade e estampa de tempo, pulso, oscilação, alteração em
  lote e importação por CSV.
- **Vínculo com o relé real**: o ponto simulado espelha (ou espelha invertido) um ponto lido do relé de campo,
  só com leitura.
- **Serviço de controle**: Operate, Select (SBO), SelectWithValue (SBOw), Cancel, LastApplError e
  CommandTermination, conforme o ctlModel, com eco automático ou decidido por lógica.
- **Lógicas em diagrama de blocos** (como um CLP): AND, OR, XOR, NOT, temporizadores, pulso, SR, comparação,
  comando, saída e bloqueio (intertravamento).
- **Subida de pontos**: lista de sinais levados de um estado inicial a um final para conferir no SAGE, com
  planilha Excel de modelo e de resultado.
- **Relógio por IED**: hora do sistema, com deslocamento ou congelada, e bits de qualidade da estampa de tempo.
- **Injeção de falhas**: atraso, descarte e duplicação de reports, ConfRev/RptID errados, recusa ou queda de
  associação, erro na escrita de RCB, silêncio no Read e report só ao primeiro cliente.
- **Cenários** (estados e sequências de pontos) e **registro de ensaios** com o SAGE.
- **Monitor de rede**: mostra quem está tentando conectar, inclusive em IPs ou portas onde nenhum IED escuta.
- **Visualizador e editor de CID** com três visões (modelo, tipos e XML), validação, diff, comparação entre
  arquivos e exportação em CSV.
- **Gerador e decodificador de OPMSK** do SAGE.
- **Registro MMS e exportação PCAP** do tráfego de cada IED.
- **Usuários locais e LDAP**, com três papéis: leitura, operação e administrador.
- **Backup** em ZIP pela própria interface.

---

## Requisitos

| Item | Detalhe |
|---|---|
| Sistema | Ubuntu (ou outro Linux com `ip` do iproute2 e systemd) |
| Python | 3.10 ou mais novo (`python3-venv`) |
| Rede | Uma placa com o IP da interface web e um IP adicional (alias) para cada IED |
| Permissões | Porta 102 exige `CAP_NET_BIND_SERVICE`. Criar IPs pela tela Rede exige `CAP_NET_ADMIN`. O monitor de conexões exige `CAP_NET_RAW`. O serviço systemd já concede as três. |
| LDAP (opcional) | Acesso a `192.168.75.108:389` (rede TOCOT), com StartTLS e a CA em `data/ldap-ca.pem` |
| Navegador | Qualquer navegador moderno (Chrome, Firefox, Edge) |

Dependências Python (`requirements.txt`): `fastapi`, `uvicorn[standard]`, `python-multipart`, `ldap3` e
`openpyxl`. Os testes usam também `httpx` e `pytest`.

---

## Instalação

### 1. Obter o código

```bash
git clone https://github.com/rafaelnrr/simulador-ied.git sim_ied
cd sim_ied
```

(Ou extraia o pacote `simied-*.tar.gz` / `sim_ied.zip` e entre na pasta `sim_ied`.)

### 2. Instalar como serviço (recomendado)

```bash
./instalar.sh --servico
```

O script:

1. confere a versão do Python e instala o `python3-venv`, se faltar (pede `sudo`);
2. cria o ambiente virtual `.venv` e instala as dependências;
3. cria `simied.conf` a partir de `simied.conf.exemplo`, se ele ainda não existir;
4. cria o serviço systemd `simied` (`/etc/systemd/system/simied.service`), que sobe no boot, reinicia sozinho
   se cair e recebe só as permissões de rede necessárias (`CAP_NET_BIND_SERVICE`, `CAP_NET_ADMIN` e
   `CAP_NET_RAW`);
5. liga o serviço.

Comandos úteis do serviço:

```bash
sudo systemctl status simied        # estado
sudo systemctl restart simied       # reiniciar (após mudar simied.conf)
sudo systemctl stop simied          # parar
journalctl -u simied -f             # acompanhar o log
```

### 3. Instalar sem o serviço

```bash
./instalar.sh
./iniciar.sh
```

Rodando à mão, a porta 102 precisa de permissão. Conceda as capacidades ao Python do ambiente virtual (isso
vale para todo uso desse Python):

```bash
sudo setcap cap_net_bind_service,cap_net_admin,cap_net_raw=+ep "$(readlink -f .venv/bin/python)"
```

### 4. Criar o primeiro usuário

Fora de `127.0.0.1`, o login é obrigatório. Se o LDAP não estiver configurado, crie um usuário local antes de
subir:

```bash
.venv/bin/python -m simied usuario admin --papel administrador
```

A senha é pedida duas vezes no terminal.

### 5. Firewall

Se o `ufw` estiver ativo:

```bash
sudo ufw allow 8080/tcp   # interface web / API
sudo ufw allow 102/tcp    # MMS dos IEDs
```

### 6. Acessar

Abra `http://<IP-da-interface-web>:8080/` no navegador e entre com o usuário local ou de rede.

---

## Configuração (`simied.conf`)

O arquivo fica ao lado do `iniciar.sh` e é lido por ele a cada partida. Variáveis de ambiente com o mesmo nome
têm prioridade sobre o arquivo.

| Variável | Exemplo | Descrição |
|---|---|---|
| `SIMIED_IFACE` | `enp0s3` | Interface com DHCP. A interface web escuta no IP que o DHCP der a ela. Rodando como serviço, se o IP mudar, o simulador reinicia sozinho no IP novo (confere a cada 10 s). |
| `SIMIED_HOST` | `192.168.74.10` | IP fixo da interface web. Se definido, tem prioridade sobre `SIMIED_IFACE`. |
| `SIMIED_PORTA` | `8080` | Porta da interface web e da API. |
| `LDAP_URL` | `ldap://192.168.75.108:389` | Servidor LDAP. Vazio = só usuários locais. |
| `LDAP_TLS_NOME` | `srv-radius-01` | Nome esperado no certificado do LDAP, quando difere do host da URL. |
| `LDAP_BASE` | `dc=redetocot,dc=local` | Base DN do diretório. |
| `LDAP_CA` | `data/ldap-ca.pem` | CA do servidor LDAP (padrão `data/ldap-ca.pem`). |

Regras importantes:

- O IP da interface web **não pode ser `0.0.0.0`** e **não pode ser usado por nenhum IED** (RN-NET-08).
  Reserve outros IPs para os IEDs.
- Para descobrir as interfaces e IPs da máquina: `ip -br addr`.
- Depois de mudar o arquivo, reinicie: `sudo systemctl restart simied`.

Com o LDAP ligado, o `iniciar.sh` usa StartTLS e mapeia os grupos `simied_admin`, `simied_operacao` e
`simied_leitura` (veja a próxima seção).

---

## Usuários, perfis de acesso e LDAP

### Papéis

| Papel | O que pode fazer |
|---|---|
| **leitura** | Só consulta: vê IEDs, pontos, reports, registros e CIDs. Pode trocar a própria senha. |
| **operação** | Opera os IEDs: inicia e para, muda pontos, falhas, relógio, cenários, ensaios, subida de pontos, comandos de teste e políticas de RCB. |
| **administrador** | Tudo o que a operação faz e também altera a configuração: cria e exclui IEDs, perfis, lógicas, edita e aplica CIDs, gerencia usuários, preferências do servidor e backup. |

Mudar o papel ou a senha de um usuário, ou removê-lo, encerra os logins abertos dele. O último administrador
não pode ser rebaixado nem removido, e ninguém remove o próprio usuário.

### Usuários locais

- Criar ou alterar pela linha de comando:
  `.venv/bin/python -m simied usuario <nome> --papel leitura|operacao|administrador`
- Ou pela tela **Configuração › Usuários** (administrador).
- Cada usuário troca a própria senha em **Configuração e minha conta** (menu do usuário, no canto superior
  direito).

Usuários locais têm prioridade: se o nome existe no banco local, a senha é conferida localmente.

### LDAP (rede TOCOT)

Se o nome não existir localmente e o LDAP estiver configurado, a senha é validada por *bind* no diretório e o
papel vem dos grupos (`posixGroup` ou `groupOfNames`, procurados pelo `cn`) em `ou=Grupos,dc=redetocot,dc=local`:

| Grupo | Papel |
|---|---|
| `simied_admin` | administrador |
| `simied_operacao` | operação |
| `simied_leitura` | leitura |

Para dar acesso, inclua o login como `memberUid` no grupo. O arquivo `data/simied-grupos.ldif` recria os grupos,
se preciso. Sem TLS (sem `ldaps://` nem StartTLS), o simulador avisa no log que as senhas trafegam em claro.

---

## Rede: IP da interface web e IPs dos IEDs

O SAGE conecta sempre na **porta 102** do IP do relé. Por isso **cada IED simulado precisa de um IP próprio**
na máquina, todos na porta 102. A interface web usa outro IP (o de `SIMIED_HOST`/`SIMIED_IFACE`).

Exemplo de uma placa com três IPs:

```
enp0s3  192.168.74.10/23   ← interface web (DHCP ou SIMIED_HOST)
        192.168.74.51/23   ← IED 1 (alias)
        192.168.74.52/23   ← IED 2 (alias)
```

Criar um IP secundário:

- pela tela **Rede › Criar IP secundário** (precisa de `CAP_NET_ADMIN`, que o serviço já tem; sem permissão,
  a tela mostra o comando para rodar à mão); ou
- no terminal: `sudo ip addr add 192.168.74.51/23 dev enp0s3`.

O IP criado com `ip addr add` some no reboot. Para deixá-lo permanente, coloque-o no netplan
(`/etc/netplan/*.yaml`) e rode `sudo netplan apply`.

Antes de usar um IP, a aplicação confere se ele já responde na rede e **recusa um IP em uso**, para não
conflitar com o relé real (RN-NET-03). Para testar só na própria máquina, use `--teste-local` (permite IEDs em
`127.0.0.1`).

---

## Primeiros passos

Roteiro mínimo para colocar um IED no ar e vê-lo no SAGE:

1. **Entre** em `http://<IP>:8080/` com um usuário administrador.
2. **Crie o IP do IED** em **Rede › Criar IP secundário** (por exemplo, o mesmo IP do relé real numa rede de
   teste isolada, ou um IP reservado).
3. Em **IEDs**, clique em **Novo IED a partir do CID** e escolha o arquivo `.cid` (há exemplos na pasta
   `exemplos/`).
   - Num SCD, escolha qual IED do arquivo usar.
   - Defina o **nome** do IED, o **perfil** (ou `auto`), a **porta** (102), o **máximo de associações** e a
     **interface/IP de escuta**.
   - Se o CID tiver erros, a importação lista cada um. Você pode corrigir o arquivo ou marcar
     "Subir o IED só com o que é válido".
4. Abra o IED e clique em **▶ Iniciar**. O sistema pede confirmação, porque a partir daí os SCADAs que
   apontarem para esse IP recebem dados simulados.
5. No SAGE, aponte o IED para o IP escolhido e ligue a comunicação. Em **Painel › Clientes associados** o SAGE
   deve aparecer. Se não aparecer, veja **Rede › Quem está tentando conectar**.
6. Vá em **Sinais digitais**, mude um ponto (por exemplo, a posição de um disjuntor) e confira o evento no SAGE
   e em **Reports e histórico**.
7. Ao terminar, clique em **■ Parar** no IED ou em **■ PARADA RÁPIDA** para parar tudo.

---

## Manual da interface

A interface tem um menu lateral (botão ☰ para recolher) dividido em **Simulação** (IEDs, Rede, Perfis,
Cenários, Ensaios), **CID** (Visualizador, Editor), **SAGE** (OPMSK) e **Sistema** (Configuração).

### Barra superior e parada rápida

- **Faixa vermelha "MODO SIMULAÇÃO"**: lembra que os dados não vêm de relés reais.
- **Relógios LOCAL e UTC** e o seletor **Local primeiro / UTC primeiro**, que define qual horário aparece em
  destaque nas tabelas (o outro aparece embaixo).
- **Tema** (claro/escuro) e o **menu do usuário** (minha conta, troca de senha, sair).

#### Parada rápida

O botão **■ PARADA RÁPIDA** (RN-SEG-05):

1. para todos os IEDs;
2. fecha todas as associações com os SCADAs;
3. cancela os cenários em execução;
4. devolve os pontos ao estado de antes da partida.

Use-o sempre que um teste sair do controle ou ao fim da janela combinada com a COT.

### IEDs

Lista os IEDs simulados, com estado (parado, rodando, erro), IP:porta, associações e reports enviados.
Iniciar ou parar um IED não afeta os demais.

Ações:

- **Novo IED a partir do CID**: importa um CID, ICD ou SCD. O CID é a fonte do modelo de dados: o servidor
  expõe só o que está nele.
- **Iniciar / Parar**: liga ou desliga o servidor MMS do IED. Escolha a interface e o IP no painel do IED antes
  de iniciar (RN-NET-13).
- **Duplicar**: cria outro IED com o mesmo CID, para testar vários relés iguais (com nome e IP diferentes).
- **Excluir**: remove o IED e sua configuração (administrador).

### Página do IED

O cabeçalho mostra nome, estado, IP:porta, associações e reports enviados, além dos botões **Editor de CID** e
**▶ Iniciar / ■ Parar**. As abas estão descritas abaixo.

#### Painel

- **Relógio do IED** (RN-TMP-01 a 06):
  - **Modo**: `sistema` (hora da máquina), `deslocamento` (hora da máquina + N segundos, por exemplo
    −10800 para simular um relé 3 h atrasado) ou `congelado` (hora parada num instante).
  - **Qualidade de t (byte)**: bits de TimeQuality enviados nas estampas (por exemplo, `0x8b`:
    LeapSecondsKnown + 11 bits de precisão).
  - **Relógio com falha**: liga os bits ClockFailure e ClockNotSynchronized.
  - O painel mostra a hora local da máquina, a hora do IED (UTC no fio) e a diferença entre elas.
- **Configuração para o SAGE e rede**:
  - nome do IED (nome MMS / `/etc/hosts`), perfil, porta (102 é a do SAGE; outra porta só serve a clientes que
    a aceitem), máximo de associações;
  - **Interface de escuta** e IP (RN-NET-12 a 17), com o IP do relé real lido do CID como referência;
  - "Ao reiniciar a aplicação, voltar os pontos ao último valor salvo".
  - Trocar o perfil ou o nome com o IED rodando pede confirmação e derruba as associações (RN-PRF-04).
- **Subir novo CID do mesmo relé**: troca o CID mantendo o estado dos pontos, as lógicas e a configuração dos
  comandos. Antes de confirmar, mostra uma prévia com os pontos mantidos, removidos e novos, e sugere o
  equivalente para cada referência de lógica que não existe mais.
- **Clientes associados** (RN-NET-11): IP:porta de cada SCADA conectado.
- **Falhas ativas**, com atalho para a aba Falhas, e **decodificador de OPMSK**.

#### Sinais digitais

Todos os pontos digitais do modelo (CDCs **SPS, SPC, DPS, DPC, ACT, ACD**; o valor é `stVal` ou `general`).

- **Busca** por caminho ou descrição. Há filtros, e a opção "incluir somente leitura" mostra também os pontos
  de CDC não tratado (valor padrão, só leitura; RN-CID-05).
- **Mudar valor**: clique no valor. DPS/DPC aceitam `intermediate-state`, `off`, `on` e `bad-state`.
- **Qualidade**: validity (`good`, `invalid`, `reserved`, `questionable`) e os detalhes (overflow, oscillatory,
  test, operatorBlocked…). Mudar só a qualidade gera **qchg**. Mudar valor e qualidade juntos gera um único
  evento com **dchg + qchg** (RN-DIG-02).
- **Seleção em lote**: marque vários pontos e use **Valor para os selecionados**. Eles mudam na mesma
  requisição e entram juntos no mesmo report quando estão no mesmo DataSet (RN-DIG-09).
- **Pulso** (liga e desliga após N ms) e **Oscilação** (N ciclos com período): servem para testar pontos que
  alternam rápido (RN-DIG-08). No fim, o valor volta ao atual.
- **Modo do ponto**:
  - `manual`: valor definido na tela;
  - `espelhado`: valor vindo do relé real (veja Vínculos);
  - `invertido`: espelhado com o valor invertido;
  - `forcado`: valor fixado à mão, mesmo com vínculo. Um ponto espelhado precisa passar para forçado para ser
    alterado (RN-UI-02).
- **Vínculo**: liga o ponto a um ponto do relé real.
- **Salvar estado como cenário**: grava os valores atuais num cenário.

#### Sinais analógicos

Pontos analógicos e enumerados: **MV, CMV** (FC=MX, `mag`/`cVal`), **INS, INC, ENS, ENC** (`stVal`),
**BCR** (`actVal`) e **ISC** (`valWTr`).

Funcionam como os digitais e trazem também:

- valor em ponto flutuante (`mag.f`) e o inteiro equivalente (`mag.i`), quando o tipo tem os dois;
- **manter t ao editar** (RN-ANA-06), que altera o valor sem mudar a estampa de tempo;
- **Importar CSV** (RN-ANA-07): uma linha por ponto, com caminho e valor separados por `;` (vírgula decimal)
  ou por `,`. Valor fora da faixa entra como exceção confirmada.

Exemplo de CSV:

```
CTRL/MMXU1$MX$TotW$mag$f;12,5
CTRL/MMXU1$MX$Hz$mag$f;60,02
```

#### Vínculos

Liga pontos do simulador a pontos do **relé real**, para o simulador repetir o que acontece no campo
(RN-DIG-04 a 07).

- O cliente do vínculo **só lê** (RN-SEG-02): faz associação e Read periódico, sem nunca escrever no relé,
  nem em atributos de RCB. Por isso a leitura é sempre periódica, e não por report.
- Cada relé vinculado recebe **uma associação a mais** (a do simulador), além das dos SCADAs. Confira o limite
  de associações do relé e evite horários críticos (RN-SEG-06).
- Configuração por ponto: IP do relé real (sugerido a partir do CID), caminho remoto (vazio = mesmo caminho do
  ponto), intervalo de leitura (ms), replicar `t` do relé (padrão, para comparar com o que o SAGE recebe do relé
  real), replicar a qualidade e marcar qualidade inválida quando a comunicação cair (RN-DIG-07).
- A aba mostra as conexões com relés reais e os pontos vinculados. O vínculo só funciona com o IED rodando.

#### RCBs e DataSets

Lista os **Report Control Blocks** (BRCB/URCB) e os **DataSets** do IED.

Para cada RCB: tipo, instâncias, DataSet, RptID, TrgOps, OptFlds, IntgPd, BufTm, quem habilitou (cliente),
estado da política e escritas recusadas.

- **DataSets e RCBs extras de teste**: crie DataSets e RCBs que não estão no CID, para testes. Eles não fazem
  parte do CID; para levá-los ao arquivo, use **Exportar CID com extras**.
- **Política de acesso por RCB**: define como o servidor responde às escritas MMS de cada cliente.

  | Política | Escrita de atributos | RptEna |
  |---|---|---|
  | `livre` | tratamento normal | normal |
  | `ocupado` | erro 2 (temporarily-unavailable) em cada item; DatSet erro 3 | erro 3 |
  | `negado` | erro 3 (object-access-denied) em cada item | erro 3 |
  | `somente_leitura` | erro 3 em cada item | erro 3; nunca é habilitado |

  Você também pode definir:
  - **Erros por atributo** (`ok`, `erro2`, `erro3`), que prevalecem sobre a política;
  - **IPs tratados como livre**: com a lista preenchida, quem está nela é livre e quem está fora recebe a
    política (se ela for `livre`, quem está fora recebe `negado`);
  - **Ocultar**: o RCB some do GetNameList e do GetVariableAccessAttributes, mas Read e Write por nome continuam
    funcionando;
  - **Instanciar**: sufixo `01…max`, sem sufixo, ou a regra do perfil. Essa opção reinicia o IED e é só do
    administrador.
- Ações: **Restaurar perfil**, **Derrubar** (desabilita o RCB que um cliente já tinha habilitado e libera a
  reserva) e alteração **em lote** (por exemplo, deixar todos livres ou todos ocupados).
- O histórico de **escritas recusadas** mostra horário, RCB, atributo, código, IP e motivo.

A mudança de política vale já para as próximas escritas, sem reiniciar o IED. Os detalhes estão em
[`API.md`](API.md#política-de-acesso-por-rcb).

#### Comandos

Objetos de controle do IED: cada DO com atributos FC=CO (`Oper`, `SBOw`, `Cancel`, `SBO`).

- Mostra o ctlModel (lido do CF do DO), os serviços disponíveis, o estado, a seleção, os bloqueios ativos e se
  o objeto está em **eco** ou é controlado por **lógica**.
- **Eco** (sem lógica): o `stVal` do próprio DO passa a valer o comando, opcionalmente com **tempo de manobra**
  (passa por intermediário antes de chegar à posição).
- **Ajustes** (administrador): tempo de manobra, *timeout* da terminação (prazo para chegar à posição pedida),
  validade da seleção (SBO) e "rejeitar comando para a mesma posição".
- **Comando de teste pela tela**: passa pelas mesmas verificações de um comando MMS.
- **Registro**: cada comando recebido, com objeto, endereço MMS, serviço, ctlVal, origem (orCat/orIdent),
  ctlNum, T, Test, Check, resultado/AddCause e terminação.

Comportamento MMS: as recusas mandam **LastApplError** (com AddCause) antes da resposta do Write. Com segurança
reforçada (ctlModel 3 e 4), sai **CommandTermination** positiva quando o estado chega à posição, ou negativa
(Time-limit-over) no fim do operTimeout.

#### Lógicas

Editor gráfico de diagramas de blocos, que funciona como um CLP dentro do relé simulado.

- **Como montar**: arraste um bloco da lista à esquerda para a área de desenho. Ligue a bolinha de saída
  (direita) à de entrada (esquerda) de outro bloco. Para remover, clique no fio ou bloco e aperte Delete. Para
  religar, arraste a ponta de um fio.
- **Blocos**:

  | Bloco | Função |
  |---|---|
  | `entrada` | lê um ponto do IED (digital ou analógico) |
  | `comando` | recebe um comando de um objeto de controle; o eco automático desliga e a lógica decide |
  | `const` | valor constante |
  | `and`, `or`, `xor`, `not` | lógica booleana |
  | `ton`, `tof` | temporizador na ligação / no desligamento |
  | `pulso` | pulso de duração fixa |
  | `sr` | flip-flop set/reset |
  | `comparar` | compara analógicos (>, <, =…) |
  | `saida` | escreve num ponto (só na mudança do sinal, para não brigar com quem mexe à mão) |
  | `bloqueio` | recusa o comando (LastApplError Blocked-by-interlocking) enquanto o sinal estiver ligado |

- **Salve** para a lógica rodar. Com ela salva, os fios ficam verdes quando o sinal está ligado, e os valores
  são lidos do simulador a cada 0,4 s.
- O motor varre as lógicas ativas a cada 50 ms e também na hora em que um ponto muda ou chega um comando.
- Há um modelo pronto (comando → disjuntor com tempo de manobra e bloqueio de fechamento) e o botão
  **Subir novo CID**, que troca o CID mantendo as lógicas.

#### Subida de pontos

Lista de sinais que o simulador leva de um **estado inicial** a um **final**, para a equipe conferir no SAGE
ponto a ponto.

1. Monte a lista: acrescente linhas (ponto, estado inicial, estado final, observação, ID do CGF no SAGE) ou
   **importe uma planilha**.
2. Planilhas:
   - **Planilha modelo**: em branco, com a lista dos pontos do IED e listas suspensas dos estados aceitos;
   - **Planilha preenchida**: já com os sinais do IED e estados sugeridos (DPS fechado → aberto, digitais
     0 → 1, medidas e enumerados a partir do valor atual). Revise e marque N na coluna Incluir para o que não
     for testar;
   - opções: só digitais, só analógicos, ou só os pontos que estão em algum DataSet (os que o SAGE recebe).
3. Na importação, as linhas com erro são mostradas com o número da linha do Excel. Corrija e importe de novo,
   ou importe só as válidas. Você pode acrescentar ao fim da lista ou substituir a lista atual.
4. **Execute** linha a linha ou a lista toda. A execução roda no servidor e continua mesmo com o navegador
   fechado. Opção: voltar ao estado inicial depois do final.
5. Marque a **conferência** de cada linha e exporte a **planilha de resultado**, que pode ser importada de
   novo.

#### Reports e histórico

- **Reports enviados**: cada Information Report, com RCB, cliente, motivo de inclusão (reason code), EntryID e
  valores.
- **Histórico de pontos**: cada mudança, com valor, qualidade, `t` e origem (manual, cenário, lógica, vínculo,
  subida…).
- **Registro MMS**: requisições e respostas MMS por associação, para depurar o que o SAGE pediu.
- **Exportar PCAP**: gera um PCAP sintético dos quadros TPKT capturados, para abrir no Wireshark.
- **Sessões**: associações abertas e encerradas.
- **Limpar registros** (`DELETE /ieds/{id}/logs`), que também apaga o histórico de recusas de RCB.

#### Falhas

Injeção de falhas de comunicação (RN-FLT-01 a 08). Ative uma por vez para ver como o SAGE reage.

| Falha | Regra | Parâmetros | Efeito |
|---|---|---|---|
| Atrasar reports / entregar fora de ordem | RN-FLT-01 | `ms` (500), `fora_de_ordem` | segura cada report N ms, ou troca a ordem |
| Descartar reports | RN-FLT-02 | `um_a_cada` (2), `ponto` | não envia 1 de cada N reports (opcionalmente só de um ponto) |
| Duplicar report ou repetir EntryID | RN-FLT-03 | `modo` | envia o report duas vezes ou repete o EntryID |
| ConfRev diferente do CID | RN-FLT-04 | `valor` (999) | ConfRev lido/enviado diferente do configurado |
| RptID diferente do escrito | RN-FLT-04 | `valor` | RptID do report diferente do que o cliente escreveu |
| Omitir t | RN-FLT-04 | — | estampa zerada nos reports |
| Recusar associação | RN-FLT-05 | — | rejeita o Initiate |
| Fechar associação após N segundos | RN-FLT-05 | `segundos` (30) | derruba a associação depois de N s |
| Erro fixo na escrita de atributo de RCB | RN-FLT-06 | `atributo` (RptEna), `codigo` (2), `rcb` | responde erro à escrita |
| Não responder Read | RN-FLT-07 | — | mantém o TCP aberto, mas não responde ao Read |
| Report só ao primeiro cliente | RN-FLT-08 | — | num RCB compartilhado, só o primeiro cliente recebe |

As falhas ativas aparecem no cabeçalho do IED e no Painel.

### Rede

- **Interfaces** (RN-NET-12): placas da máquina, com IPs, máscara e quais IEDs usam cada IP.
- **Criar IP secundário** (RN-NET-02): adiciona um alias numa interface. Antes, verifica se o IP já responde na
  rede. Sem permissão, mostra o comando para criar à mão.
- **Quem está tentando conectar**: monitor de conexões com duas fontes:
  - a própria aplicação: TCP aceito, associação aceita ou recusada, conexão fechada antes de associar e LD
    pedido que pertence a outro IED;
  - captura de pacotes (`CAP_NET_RAW`): pedidos ARP, SYN TCP para as portas MMS e pings, **mesmo para IP ou
    porta onde nenhum IED escuta**. É aqui que aparece, por exemplo, "o SAGE tenta 10.0.0.5:102, que não
    existe".
  - Os pedidos são agrupados por origem e destino. Retransmissões do mesmo SYN indicam que o cliente não recebeu
    resposta. Há filtro por IP, IED ou texto.

### Perfis

Um **perfil** agrupa as particularidades de um modelo de relé que o CID sozinho não descreve (RN-PRF-01 a 04).
É um arquivo JSON versionado.

Perfis fornecidos (pasta `profiles/`, somente leitura):

| Perfil | Descrição |
|---|---|
| `generico-ed1` | IED genérico Edição 1 |
| `generico-ed2` | IED genérico Edição 2 |
| `ingeteam-ef-i15` | Ingeteam INGEPAC EF (i15): RCBs só no LLN0, DatSet `Conf`, sem DataSet dinâmico, até 16 DataSets e 32 RCBs, gatilhos dchg/qchg (sem dupd), Edição 1, BRCB_ESTADOS_N2/N3 ocupados e BRCB_ESTADOS_SAGE sem sufixo |
| `referencia-UCDQSA1` | Referência UCDQSA1, com RCB por LN |

Para alterar um perfil fornecido, **crie uma cópia** (RN-PRF-03). As cópias ficam em `data/profiles` e cada
gravação incrementa a versão. Mudar o perfil de um IED parado vale na próxima partida; com o IED rodando, pede
confirmação e derruba as associações.

Principais seções do JSON:

```jsonc
{
  "id": "ingeteam-ef-i15",
  "nome": "Ingeteam INGEPAC EF (i15)",
  "versao": 1,
  "edicao": 1,                                  // 1 ou 2
  "identificacao": {"fabricante": "...", "modelo": "...", "revisao": "..."},
  "servicos": {                                 // serviços MMS opcionais
    "defineNamedVariableList": false,
    "deleteNamedVariableList": false,
    "getNamedVariableListAttributes": true
  },
  "rcb": {
    "posicao": "LLN0",                          // onde ficam os RCBs
    "atributos": {"RptID": "Dyn", "DatSet": "Conf", "OptFlds": "Dyn", "BufTm": "Dyn", "TrgOps": "Dyn", "IntgPd": "Dyn"},
    "indexacao": "se_max_maior_1",              // regra do sufixo de instância
    "trgops_permitidos": ["dchg", "qchg", "period", "gi"],
    "max_datasets": 16, "max_rcbs": 32, "buffer_max": 1000,
    "erro_reservado": 2, "erro_conf": 3, "erro_habilitado": 2,
    "reasoncode_bits": 6,
    "politicas": {"BRCB_ESTADOS_N2": {"politica": "ocupado"}}
  },
  "tempo": {"qualidade_t": "0x8b"},
  "qualidade": {"inicial": {"validity": "good"}},
  "limites": {"max_associacoes": 3, "max_pdu": 65000, "max_serv_outstanding": 10, "nesting": 10}
}
```

### Cenários

Conjuntos de valores digitais e analógicos e **sequências com atraso entre passos**. Passos sem atraso entram
na mesma operação (e no mesmo report).

- **Criar vazio** ou **Cenário a partir do estado do IED** (também disponível nas telas de sinais como "Salvar
  estado como cenário").
- Cada passo tem o ponto, o valor (em JSON: `true`/`false`, número, ou objeto com valor e qualidade) e o atraso.
- **Aplicar** num IED: roda no servidor. Se aplicar outro cenário no mesmo IED, o anterior é cancelado. A
  parada rápida cancela todos.

### Ensaios

Registro dos testes feitos com o **SAGE físico** (seção 13). Eventos e alarmes injetados aparecem para o
operador e entram nos `.alr` e `.sde` reais, por isso use IED e pontos reservados para teste, combinados antes
com a COT.

Cada ensaio registra objetivo, procedimento, resultado esperado e **resultado obtido** (o que o SAGE gravou no
`.alr`/`.sde`, reports, capturas). A tela sugere um roteiro, por exemplo:

1. Importar o CID do i15 e iniciar o IED no IP da máquina → o SAGE associa, lê os DataSets e mostra os pontos
   do AQS.lst com valor atual.
2. Ligar e desligar `sAutGGIO2$ST$Ind3` (MOLA) com o perfil `ingeteam-ef-i15`, URCB e OPMSK `10C830` → evento
   no `.alr` e no `.sde`.
3. Repetir com o perfil `referencia-UCDQSA1` e RCB por LN → se funcionar, a diferença entre perfis é a causa.
4. Três SCADAs associados ao mesmo IED → cada um habilita seu RCB e recebe o mesmo report.
5. Relógio correto contra 3 h atrasado → diferença no resultado do SAGE.
6. Ponto em dois DataSets (N2 e SAGE) → mostra qual DataSet o SAGE usa.
7. Variação dos bits 5, 9, 10, 11, 14 e 20 do OPMSK → tabela de resultados por combinação.
8. Espelhar um ponto do relé real e acioná-lo no campo → o SAGE mostra o mesmo evento nos dois.
9. Falhas RN-FLT-01 a 08, uma por vez → o SAGE reage (reconexão, alarme de comunicação, evento perdido).
10. Dois IEDs com CIDs diferentes em IPs distintos → os dois operam ao mesmo tempo, e parar um não afeta o
    outro.

### Visualizador de CID

Abre o CID de um IED ou um arquivo avulso **só para leitura**:

- **Visão geral**: cabeçalho e histórico, IEDs e serviços, comunicação (IP, GOOSE, SMV), blocos de controle,
  DataSets com membros (referência MMS e descrição), ExtRef, ClientLN, subestação, Private e namespaces.
- **Exportar CSV** (separador `;`, UTF-8 com BOM, abre direto no Excel):
  - **pontos**: todo atributo simples (DA/BDA) com FC, bType, CDC, referência MMS, descrição do DOI e valor no
    CID (útil para montar a base do SAGE);
  - **datasets**, **controles** e **comunicação**.
- **Comparar** com o CID original ou com outro arquivo: diferenças por categoria (IED, LN, instâncias DOI/DAI,
  DataSet, bloco de controle, comunicação, tipo). Há a opção de ignorar o nome do IED.

### Editor de CID

Edita o CID de um IED (ou um arquivo avulso) num **rascunho** guardado em `data/cid_editor`. O IED só muda
quando o rascunho é **aplicado**. Ler é permitido a todos; alterar, aplicar, descartar e enviar arquivo é só do
administrador.

Três visões do mesmo documento:

- **Modelo**: IED → LD → LN → DO/SDO → DA/BDA, expandido dos DataTypeTemplates e combinado com as instâncias
  (DOI/SDI/DAI), como num navegador de IED. DataSets e ReportControls aparecem dentro do LN.
- **Tipos**: LNodeType, DOType, DAType e EnumType, com quem usa cada um.
- **XML**: todos os elementos do arquivo.

O que dá para fazer:

- alterar, criar ou remover **atributos** e **texto** (Val, EnumVal, P…);
- gravar o **valor** de uma instância (cria DOI/SDI/DAI se preciso, com validação pelo bType; Enum aceita texto
  ou ordinal) e a **descrição**;
- **adicionar** elementos na posição prevista pelo esquema SCL, **duplicar**, **mover** (a ordem das FCDAs é a
  ordem dos membros no report), **substituir** por XML e **excluir** (pede confirmação para tipo em uso ou
  DataSet usado por RCB);
- **incluir FCDA** num DataSet a partir de um DO/DA do modelo;
- **formulário de RCB**: name, desc, datSet, rptID, buffered, intgPd, confRev, bufTime, indexed, TrgOps,
  OptFields, RptEnabled e ClientLN, com avisos (por exemplo, quantos membros disparam por dchg/qchg/dupd e se
  o IED permite as opções);
- **Limpar tipos sem uso**;
- **Desfazer/refazer** (até 30 passos, enquanto o servidor não reinicia);
- **Buscar** por nome, referência, descrição ou atributo;
- **Validar**: erros e avisos do editor (tipos, referências, FCDAs, nomes repetidos, valores) e do carregador
  do simulador;
- **Diff**, **Baixar** o rascunho e **Aplicar no IED** (reimportação que mantém o estado dos pontos; pede
  confirmação se o IED estiver rodando ou se o CID do IED mudou depois que o rascunho foi aberto);
- **Criar IED com este rascunho**.

O arquivo gravado tem o mesmo conteúdo do original. A formatação muda pouco: a ordem das declarações de
namespace na raiz e, em casos raros, CDATA virando texto escapado.

### OPMSK

Gerador e decodificador da máscara de opções (**OPMSK**) que o SAGE usa para IEDs IEC 61850.

- **Decodificar**: informe o valor hexadecimal (por exemplo, `10C830`) e veja cada um dos 24 bits ligados, com
  resumo e efeito.
- **Gerar**: marque os bits e obtenha o valor. É possível copiar de um lado para o outro e comparar.
- **Sugestão para um IED**: indica os bits que dependem do CID. O bit 4 segue o DynDataSet nos Services e o
  bit 5 indica se todos os RCBs estão no LLN0. Também traz observações sobre BRCB/URCB (bits 17 e 19) e
  Edição 2 (bit 21).
- O valor padrão vem de **Configuração › SAGE**.

### Configuração

Só para o administrador, exceto a troca da própria senha.

- **Padrões do servidor**: valores para um novo IED (perfil `auto` ou fixo, porta, máximo de associações),
  OPMSK padrão do SAGE e visão inicial do editor. São gravados em `data/config.json`.
- **Usuários**: criar, mudar papel e senha, remover.
- **Minha conta**: trocar a própria senha (todos os papéis, só usuários locais).
- **Sistema**: versão, endereço, pasta de dados, login/LDAP, IEDs e uso de disco.
- **Backup**: baixa um ZIP com o banco (cópia consistente), CIDs, perfis, rascunhos do editor e `config.json`.

---

## API REST e WebSocket

Tudo o que a interface faz também está disponível pela API.

- **Documentação interativa (OpenAPI)**: `http://<IP>:8080/api/docs` (JSON em `/api/openapi.json`).
- Todas as rotas ficam sob **`/api/v1`**.
- **Autenticação**: `POST /api/v1/login` devolve um token. Envie-o em `Authorization: Bearer <token>`. Escutando
  só em 127.0.0.1, o login não é exigido.
- **Erros**: `{"erro": "<mensagem>", "codigo": "<código>"}` com o status HTTP correspondente (404 inexistente,
  409 precisa confirmar, 422 valor inválido).
- **Eventos em tempo real**: WebSocket em `/api/v1/ws` (mudança de ponto, reports, RCB, comandos, cenários,
  estado do IED…).

Exemplo com `curl`:

```bash
H=192.168.74.10:8080
TOKEN=$(curl -s -X POST http://$H/api/v1/login -H 'Content-Type: application/json' \
        -d '{"usuario":"admin","senha":"***"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s -H "Authorization: Bearer $TOKEN" http://$H/api/v1/ieds                 # lista os IEDs
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://$H/api/v1/ieds/1/start  # inicia o IED 1
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://$H/api/v1/panic         # parada rápida
```

O login responde `{"token": "...", "papel": "..."}`, ou **401** com usuário ou senha inválidos.

Principais grupos de rotas:

| Grupo | Rotas |
|---|---|
| Sessão | `POST /login`, `POST /logout`, `GET /eu`, `GET /info`, `POST /panic` |
| IEDs | `GET/POST /ieds`, `GET/PATCH/DELETE /ieds/{id}`, `POST /ieds/{id}/start\|stop\|duplicate\|reimport`, `GET /ieds/{id}/export-cid`, `POST /cid` (upload) |
| Pontos | `GET /ieds/{id}/points`, `GET/PATCH /ieds/{id}/points/{pid}`, `POST /ieds/{id}/points:batch`, `POST …/points/{pid}/pulse`, `POST /ieds/{id}/points:csv` |
| Modelo | `GET /ieds/{id}/model`, `GET/POST /ieds/{id}/datasets`, `GET/POST /ieds/{id}/rcbs` |
| Política de RCB | `GET/PATCH /ieds/{id}/rcbs/{nome}/policy`, `PATCH /ieds/{id}/rcbs/policy`, `POST …/disable-client`, `GET /ieds/{id}/rcb-denied` |
| Controle e lógicas | `GET /ieds/{id}/controles`, `POST …/controles/comando`, `PATCH …/controles/config`, `GET/DELETE /ieds/{id}/comandos`, `/ieds/{id}/logicas…` |
| Subida de pontos | `/ieds/{id}/subida…`, `POST /ieds/{id}/subida-importar`, `GET /ieds/{id}/subida-planilha` |
| Relógio e falhas | `GET/PUT /ieds/{id}/clock`, `GET/PUT /ieds/{id}/faults` |
| Registros | `GET /ieds/{id}/reports\|history\|mms-log\|sessions\|links`, `DELETE /ieds/{id}/logs` |
| Rede | `GET /net/interfaces`, `POST /net/alias`, `GET/DELETE /net/monitor` |
| Cenários, perfis, ensaios | `/scenarios…` (inclui `POST /scenarios/{id}/apply`), `/profiles…` (inclui `POST /profiles/{id}/copy`), `/ensaios…` |
| CID | `/cid-editor…` (editor e visualizador) |
| OPMSK | `GET /opmsk?valor=`, `GET /opmsk/bits`, `GET /opmsk/sugestao?ied=` |
| Sistema | `GET/PUT /settings`, `/users…`, `POST /me/password`, `GET /system`, `GET /backup` |

A referência detalhada da política de RCB, do editor de CID, dos comandos/lógicas e da reimportação está em
[`API.md`](API.md).

---

## Linha de comando

```
python -m simied [--dados DIR] serve [opções]     # inicia (padrão)
python -m simied [--dados DIR] usuario NOME [--papel leitura|operacao|administrador]
```

| Opção | Padrão | Descrição |
|---|---|---|
| `--dados` | `./data` | Pasta de dados (banco, CIDs, perfis) |
| `--admin-host` | `127.0.0.1` | IP da interface web/API (nunca `0.0.0.0`; precisa existir na máquina) |
| `--admin-port` | `8080` | Porta da interface web/API |
| `--teste-local` | — | Permite IEDs em 127.0.0.1 e mostra o loopback nas interfaces |
| `--log` | `info` | Nível de log (`debug`, `info`, `warning`…) |
| `--ldap-url` | — | `ldaps://servidor:636` ou `ldap://servidor:389` com `--ldap-starttls` |
| `--ldap-base` | — | Base DN (obrigatório com `--ldap-url`) |
| `--ldap-usuarios-dn` / `--ldap-grupos-dn` | `ou=Usuarios,<base>` / `ou=Grupos,<base>` | Onde procurar usuários e grupos |
| `--ldap-grupo-admin` / `--ldap-grupo-operacao` / `--ldap-grupo-leitura` | — | `cn` dos grupos de cada papel (podem ser repetidos) |
| `--ldap-starttls` | — | Usa StartTLS |
| `--ldap-ca` | — | PEM da CA do LDAP |
| `--ldap-tls-nome` | — | Nome esperado no certificado |

Exemplo para testar na própria máquina, sem login:

```bash
.venv/bin/python -m simied serve --teste-local
# interface em http://127.0.0.1:8080/
```

O `iniciar.sh` monta essa linha a partir do `simied.conf`. Argumentos extras passados a ele vão direto para o
`serve` (por exemplo, `./iniciar.sh --log debug`).

---

## Dados, persistência e backup

Tudo o que muda em execução fica em `data/` (fora do controle de versão):

| Caminho | Conteúdo |
|---|---|
| `data/simied.db` (+ `-wal`, `-shm`) | Banco SQLite: IEDs e sua configuração, estado dos pontos, usuários, cenários, ensaios, históricos |
| `data/cids/` | CIDs importados |
| `data/cid_editor/` | Rascunhos do editor de CID |
| `data/profiles/` | Perfis criados pelo usuário |
| `data/config.json` | Padrões do servidor (tela Configuração) |
| `data/ldap-ca.pem` | CA do servidor LDAP |
| `data/simied-grupos.ldif` | LDIF para recriar os grupos no LDAP |
| `data/simied.log` | Log |

**Backup**: use **Configuração › Backup** (ou `GET /api/v1/backup`). O ZIP traz uma cópia consistente do
banco, os CIDs, os perfis, os rascunhos e o `config.json`. Para restaurar, pare o serviço, extraia o conteúdo em
`data/` e suba de novo.

> O repositório git guarda **só o código**. A pasta `data/`, o `.venv/` e o arquivo `CREDENCIAIS_ADMIN.txt`
> estão no `.gitignore` e não devem ser publicados, porque contêm usuários, senhas e dados de operação.

---

## Testes automáticos

```bash
.venv/bin/python -m pytest -q
```

Os testes sobem o simulador real num subprocesso (loopback, `--teste-local`) e conversam com ele por um cliente
MMS próprio. Cobrem comandos, editor de CID, ensaios, LDAP, monitor de rede, papéis, política de RCB, sistema,
subida de pontos e testes de unidade.

---

## Estrutura do projeto

```
sim_ied/
├── simied/                 # aplicação
│   ├── __main__.py         # linha de comando (serve, usuario)
│   ├── api.py              # API REST /api/v1 e WebSocket
│   ├── manager.py          # IEDs, uploads, rede, cenários, parada rápida
│   ├── ied.py              # IED em execução: pontos, modos, persistência
│   ├── model.py            # modelo de dados MMS, DataSets, catálogo de pontos
│   ├── scl.py              # leitor SCL (CID/ICD/SCD/IID, Ed. 1 e 2)
│   ├── reports.py          # motor de reports BRCB/URCB
│   ├── rcbpolicy.py        # política de acesso por RCB
│   ├── control.py          # serviço de controle (Operate, SBO, SBOw, Cancel)
│   ├── logic.py            # motor das lógicas em blocos
│   ├── subida.py           # subida de pontos e planilhas
│   ├── mirror.py           # vínculo (somente leitura) com o relé real
│   ├── clock.py            # relógio por IED e qualidade de tempo
│   ├── faults.py           # catálogo e estado das falhas
│   ├── netif.py, netmon.py # interfaces de rede e monitor de conexões
│   ├── pcap.py             # exportação PCAP
│   ├── cidedit.py          # editor/visualizador de CID
│   ├── profiles.py         # perfis de IED
│   ├── auth.py             # usuários locais e LDAP
│   ├── sistema.py          # configuração, usuários, sistema, backup
│   ├── storage.py          # SQLite
│   ├── ber.py              # codificação BER
│   └── mms/                # pilha ISO/MMS: iso.py, pdu.py, types.py, server.py, client.py
├── web/                    # interface (index.html, app.js, app.css)
├── profiles/               # perfis fornecidos (somente leitura)
├── exemplos/               # CIDs de exemplo e gerador
├── tests/                  # testes (pytest)
├── instalar.sh             # instalação (+ serviço systemd com --servico)
├── iniciar.sh              # partida, lendo simied.conf
├── simied.conf.exemplo     # modelo de configuração
├── requirements.txt
├── INSTALAR.md             # guia curto de instalação
├── API.md                  # referência detalhada da API
└── README.md
```

---

## Solução de problemas

| Sintoma | Causa provável e solução |
|---|---|
| `RN-UI-06: fora de 127.0.0.1 a autenticação é obrigatória` | Não há usuário nem LDAP. Crie um usuário: `.venv/bin/python -m simied usuario admin --papel administrador`. |
| `RN-NET-08: a administração não pode escutar em todas as interfaces` | `SIMIED_HOST` está como `0.0.0.0`. Use o IP de gerenciamento. |
| `IP de administração X não existe nesta máquina` | O IP de `SIMIED_HOST` não está em nenhuma placa. Confira com `ip -br addr`. |
| `interface enp0s3 sem IP do DHCP` | A interface de `SIMIED_IFACE` não recebeu IP em 60 s. Confira o nome da interface e o DHCP, ou use `SIMIED_HOST`. |
| IED não inicia: permissão negada na porta 102 | Falta `CAP_NET_BIND_SERVICE`. Use o serviço (`./instalar.sh --servico`) ou o `setcap` descrito na instalação. |
| IED não inicia: IP em uso | Outro equipamento (talvez o relé real) responde nesse IP. Escolha outro IP ou isole a rede. |
| O SAGE não aparece em Clientes associados | Veja **Rede › Quem está tentando conectar**. SYN repetido sem resposta indica firewall (`ufw allow 102/tcp`), IP errado no SAGE ou IED parado. "LD pedido que não é deste IED" indica nome de IED diferente no SAGE. |
| "Captura de pacotes desligada" na tela Rede | Falta `CAP_NET_RAW`. O serviço já concede; rodando à mão, use o `setcap`. |
| O SAGE associa mas não recebe eventos | Confira em **RCBs e DataSets** se o RCB foi habilitado (RptEna) e se não há recusas por política. Confira se o ponto está num DataSet, os TrgOps, o OPMSK e se há falha ativa (descarte, atraso). |
| Escrita de RCB recusada com erro 2/3 | É a política do RCB (perfil ou IED) ou a falha "Erro fixo na escrita". Veja o histórico de recusas, que traz o motivo. |
| Não consigo mudar um ponto | Ele está em modo espelhado: passe para forçado. Ou o CDC não é tratado (somente leitura). |
| Login LDAP falha | Confira `LDAP_URL`, a CA em `data/ldap-ca.pem`, `LDAP_TLS_NOME` e se o usuário é `memberUid` de um dos grupos `simied_*`. |
| O IP do IED sumiu após o reboot | Alias criado com `ip addr add` não é permanente. Coloque-o no netplan. |

Logs: `journalctl -u simied -f` (serviço) ou a saída do terminal. Use `--log debug` para mais detalhes. O
registro MMS de cada IED fica em **Reports e histórico › Registro MMS**.

---

## Glossário

| Termo | Significado |
|---|---|
| **IED** | Intelligent Electronic Device: relé de proteção/controle digital |
| **CID / ICD / SCD / IID** | Arquivos SCL (IEC 61850-6): configuração de um IED, capacidade de um IED, subestação inteira, instância de IED |
| **SCL** | Substation Configuration Language (XML) |
| **MMS** | Manufacturing Message Specification (ISO 9506), protocolo do IEC 61850-8-1 sobre TCP/102 |
| **LD / LN / DO / DA** | Logical Device, Logical Node, Data Object, Data Attribute |
| **FC** | Functional Constraint (ST, MX, CO, CF, SP…) |
| **CDC** | Common Data Class (SPS, DPS, MV, SPC, DPC…) |
| **DataSet** | Lista de FCDAs enviada num report |
| **RCB / BRCB / URCB** | Report Control Block (com buffer / sem buffer) |
| **TrgOps** | Gatilhos do report: dchg (dado), qchg (qualidade), dupd (atualização), period (integridade), gi (interrogação geral) |
| **OptFlds** | Campos opcionais do report (seqNum, timeStamp, reasonCode, dataSet, entryID, confRev…) |
| **ctlModel** | Modelo de controle: status-only, direct/SBO com segurança normal ou reforçada |
| **SBO / SBOw** | Select Before Operate / Select With Value |
| **OPMSK** | Máscara de opções do SAGE para IEDs IEC 61850 |
| **SAGE** | Sistema Aberto de Gerenciamento de Energia (SCADA do CEPEL) |
| **COT** | Centro de Operação |
| **.alr / .sde** | Arquivos de alarmes e de sequência de eventos do SAGE |
| **RN-xxx-NN** | Regras de negócio do documento de requisitos do simulador, citadas na interface e no código |

---

**Versão**: 1.0.0 · **Testes**: `pytest -q` · **API**: `/api/docs`
