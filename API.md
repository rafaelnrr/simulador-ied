# API REST do SimIED

A referência completa, gerada pelo próprio servidor, fica em `http://<SIMIED_HOST>:8080/api/docs` (OpenAPI em
`/api/openapi.json`). Todas as rotas ficam sob `/api/v1` e exigem `Authorization: Bearer <token>` (obtido em
`POST /api/v1/login`) quando a interface não escuta só em 127.0.0.1. Erros voltam como
`{"erro": "<mensagem>", "codigo": "<código>"}` com o status HTTP correspondente.

## Política de acesso por RCB

Cada RCB (BRCB ou URCB) tem uma política que decide como o servidor MMS responde às escritas de um cliente.
O objetivo é reproduzir IEDs como o Ingeteam i15, em que o SAGE só consegue usar o RCB dele. A resposta é
sempre **por item** do Write, na ordem enviada; o Write inteiro nunca é recusado.

Códigos MMS `DataAccessError`: **2** = temporarily-unavailable, **3** = object-access-denied.

| Política | Escrita de atributos | RptEna |
|---|---|---|
| `livre` | tratamento normal (comportamento anterior) | normal |
| `ocupado` | erro 2 em cada item; DatSet erro 3 | erro 3 |
| `negado` | erro 3 em cada item | erro 3 |
| `somente_leitura` | erro 3 em cada item | erro 3; o RCB nunca é habilitado |

Ordem de decisão para cada item escrito:

1. Falha injetada `erro_escrita` (tela Falhas), como antes.
2. `somente_leitura` recusa `RptEna=true` sempre, mesmo para IP permitido ou atributo com `ok`.
3. Mapa `atributos` (`ok`, `erro2` ou `erro3`): prevalece sobre a política. `ok` segue o tratamento normal, que
   ainda pode recusar (por exemplo, atributo `Conf` no perfil, ou RCB habilitado por outro cliente).
4. Política do cliente. Com a lista `ips` vazia, a política vale para todos. Com a lista preenchida, quem está
   nela é tratado como `livre` e quem está fora recebe a política do RCB. Se a política for `livre`, quem está
   fora recebe `negado`; sem isso, a lista não teria efeito.

O RCB cujo RptEna foi recusado nunca envia report. Cada mudança de ponto gera um report por RCB habilitado.

A política é configurada pelo **nome-base** do RCB (o `name` do ReportControl no CID, sem sufixo de instância)
e vale para todas as instâncias. O perfil define o padrão em `rcb.politicas`, e o IED sobrescreve campo a campo
(guardado em `rcb_politicas` na configuração do IED, que persiste entre reinícios).

### `GET /ieds/{id}/rcbs/{nome}/policy`

`nome` aceita o nome-base, o nome da instância (`BRCB_ESTADOS_SAGE03`) ou a referência completa.

```json
{
  "rcb": "BRCB_ESTADOS_SAGE", "perfil": "ingeteam-ef-i15",
  "politica": "livre", "atributos": {"DatSet": "erro3", "GI": "erro2"}, "ips": [],
  "ocultar": false, "instanciar": false,
  "origem": {"politica": "perfil", "atributos": "perfil", "ips": "padrao", "ocultar": "padrao", "instanciar": "perfil"},
  "instancias": [{"nome": "BRCB_ESTADOS_SAGE", "ref": "…CTRL/LLN0$RP$BRCB_ESTADOS_SAGE", "tipo": "URCB",
                  "estado": "livre", "habilitado": true, "cliente": "10.0.0.5:51234", "recusadas": 2}]
}
```

### `PATCH /ieds/{id}/rcbs/{nome}/policy`

Todos os campos são opcionais; só os enviados mudam.

| Campo | Tipo | Efeito |
|---|---|---|
| `politica` | `livre` \| `ocupado` \| `negado` \| `somente_leitura` | política do RCB |
| `atributos` | `{atributo: "ok" \| "erro2" \| "erro3"}` | substitui o mapa inteiro; `{}` limpa |
| `ips` | lista de IPs (ou texto separado por vírgula/espaço) | `[]` = todos os clientes |
| `ocultar` | bool | some do GetNameList e do GetVariableAccessAttributes; continua no modelo (Read/Write por nome funcionam) |
| `instanciar` | `true` \| `false` \| `null` | `true` = sufixo `01`…`max`; `false` = sem sufixo; `null` = regra de indexação do perfil |
| `restaurar_perfil` | bool | apaga a sobrescrita do IED antes de aplicar os demais campos |
| `derrubar` | bool | depois de salvar, desabilita o RCB que um cliente já tinha habilitado |
| `confirmar` | bool | necessário para mudar `instanciar` com o IED rodando |

A mudança vale para as próximas escritas, sem reiniciar o IED. A exceção é `instanciar`: ela altera o modelo,
reinicia o IED (com o IED rodando, responde **409** com `codigo: "confirmar"` até vir `confirmar: true`) e só o
perfil administrador pode alterá-la. As demais mudanças são permitidas aos perfis operação e administrador.

Erros: **404** para RCB inexistente e **422** para valor inválido, com a mensagem indicando o campo. Exemplos:
`política 'xpto' inválida; use [...]`, `atributo de RCB desconhecido 'Foo'`, `IP inválido: 'abc'`.

Resposta: o mesmo objeto do `GET`.

### `PATCH /ieds/{id}/rcbs/policy` (em lote)

Aplica a mesma mudança em vários RCBs, por exemplo para deixar todos livres ou ocupados:

```json
{"rcbs": ["BRCB_ESTADOS_N2", "BRCB_ESTADOS_N3"], "politica": "livre", "derrubar": false}
```

Aceita os mesmos campos do `PATCH` individual, exceto `instanciar` (que reiniciaria o IED a cada RCB) e
`confirmar`. Tudo é validado antes de aplicar: um nome inexistente (**404**) ou um valor inválido (**422**) não
altera nenhum RCB. Resposta: `{"rcbs": [<objeto do GET de cada RCB>]}`.

### `POST /ieds/{id}/rcbs/{nome}/disable-client`

Desabilita na hora o RCB (todas as instâncias, se `nome` for o nome-base) e libera a reserva.
Resposta: `{"derrubados": [{"rcb": "<ref>", "cliente": "ip:porta"}]}`.

### `GET /ieds/{id}/rcb-denied?limit=500&rcb=<trecho>`

Histórico das escritas recusadas em RCBs, mais recentes primeiro:
`{"recusas": [{"id", "ts", "rcb", "atributo", "codigo", "ip", "cliente", "politica", "motivo"}]}`.
Inclui toda recusa de escrita em RCB, não só as causadas pela política; o campo `motivo` diz por quê.
`DELETE /ieds/{id}/logs` também apaga este histórico.

### `GET /ieds/{id}/rcbs`

Cada RCB passa a trazer também `politica` (o objeto efetivo), `estado_politica` e `recusadas` (escritas
recusadas desde a última carga do modelo).

### Eventos no WebSocket (`/api/v1/ws`)

- `rcb.write_denied`: `{"rcb", "base", "atributo", "codigo", "ip", "cliente", "politica", "motivo", "ts", "ied_nome", "recusadas"}`.
- `rcb.changed`: também é emitido quando a política muda ou a habilitação é derrubada.

### Perfil

```json
"rcb": {
  "politicas": {
    "BRCB_ESTADOS_N2": {"politica": "ocupado"},
    "BRCB_ESTADOS_SAGE": {"politica": "livre", "atributos": {"DatSet": "erro3", "GI": "erro2"}, "instanciar": false}
  }
}
```

O perfil `ingeteam-ef-i15` traz BRCB_ESTADOS_N2 e BRCB_ESTADOS_N3 como `ocupado`. BRCB_ESTADOS_SAGE,
URCB_MEDIDAS_N2 e URCB_MEDIDAS_N3 ficam `livre` com DatSet = erro 3 e GI = erro 2. BRCB_ESTADOS_SAGE fica sem
sufixo, como no relé. RCBs sem entrada em `politicas` ficam `livre`, que é o comportamento anterior.

## Editor de CID

Edita o CID de um IED (sessão `ied-<id>`) ou um arquivo avulso (sessão `arq-<hex>`) num rascunho guardado em
`data/cid_editor`. Ler é permitido a todos os perfis. Alterar, aplicar, descartar e enviar arquivo são ações só do
perfil administrador. O IED só muda quando o rascunho é aplicado.

Identificadores de nó:

- `m/<IED>/<LD>/<LN>/<DO>/<SDO|DA>/…`: nó do modelo, expandido dos DataTypeTemplates e combinado com DOI/SDI/DAI.
- `x.3.0.1`: elemento XML pela posição entre os filhos-elemento (a raiz `<SCL>` é `x`). Muda depois de inserir,
  excluir ou mover; cada operação devolve o `no` a selecionar.
- `t/LNodeType`, `t/DOType`, `t/DAType`, `t/EnumType`: grupos da visão de tipos.

| Rota | Efeito |
|---|---|
| `GET /cid-editor` | rascunhos guardados |
| `POST /cid-editor/files` (multipart `arquivo`) | abre um arquivo avulso; devolve o resumo |
| `GET /cid-editor/{sessao}` | resumo: `alterado`, `desfazer`/`refazer`, `contagem`, `historico`, `base_mudou` |
| `DELETE /cid-editor/{sessao}` | descarta o rascunho (arquivo avulso: apaga) |
| `GET /cid-editor/{sessao}/children?vista=modelo\|tipos\|xml&no=&offset=` | filhos de um nó (`no` vazio = primeiro nível); até 1000 por chamada |
| `GET /cid-editor/{sessao}/node?no=` | detalhes: atributos, tipo, instância, valor, FCDAs, quem usa, XML |
| `GET /cid-editor/{sessao}/locate?no=&vista=auto` | caminho na árvore para revelar o nó |
| `GET /cid-editor/{sessao}/search?q=&vista=` | busca por nome, referência, descrição ou atributo (nomes exatos primeiro) |
| `GET /cid-editor/{sessao}/validate` | erros e avisos do editor (tipos, referências, FCDAs, nomes repetidos, valores) e do carregador do simulador |
| `GET /cid-editor/{sessao}/diff` | unified diff entre o CID original e o rascunho |
| `GET /cid-editor/{sessao}/download` | o rascunho como arquivo |
| `POST /cid-editor/{sessao}/ops` | uma operação (tabela abaixo); devolve `{no, mensagem, alterado, sessao}` |
| `POST /cid-editor/{sessao}/undo`, `/redo` | desfaz ou refaz (até 30 passos, só enquanto o servidor não reinicia) |
| `POST /cid-editor/{sessao}/apply` | aplica no IED por reimportação (mantém o estado dos pontos); **409** `confirmar` se o IED está rodando ou se o CID do IED mudou depois que o rascunho foi aberto |
| `POST /cid-editor/{sessao}/upload-token` | prepara o rascunho como upload para `POST /ieds` (criar IED com ele) |

Operações (`op`):

| `op` | Campos | Efeito |
|---|---|---|
| `atributos` | `no`, `atributos: {nome: valor \| null}` | altera, cria ou remove (`null`) atributos |
| `texto` | `no`, `texto` | conteúdo de Val, EnumVal, P etc. |
| `excluir` | `no`, `confirmar` | remove o elemento; tipo em uso ou DataSet usado por RCB pede confirmação (409) |
| `mover` | `no`, `direcao: -1 \| 1` | troca com o irmão (a ordem das FCDAs é a ordem dos membros no report) |
| `duplicar` | `no` | cópia logo depois, com nome/id/inst novo |
| `adicionar` | `pai` e `tag` + `atributos` (+ `texto`), ou `pai` + `xml` | insere na posição prevista pelo esquema SCL; elemento fora do esquema pede confirmação |
| `substituir` | `no`, `xml` | troca o elemento pelo XML dado |
| `valor` | `no` (DA/BDA do modelo), `valor` (`null` volta ao valor do tipo) | grava o Val da instância, criando DOI/SDI/DAI; valida pelo bType (Enum aceita texto ou ordinal) |
| `descricao` | `no` (modelo ou XML), `desc` | desc do LD/LN ou da instância DOI/SDI/DAI |
| `fcda` | `no` (DO/SDO/DA do modelo), `dataset` (`x.…` do DataSet), `fc` (para DO) | inclui a FCDA no fim do DataSet |
| `rcb` | `no` (`x.…` do ReportControl), `atributos` (name, desc, datSet, rptID, buffered, intgPd, confRev, bufTime, indexed; `null` remove), `trgops`/`optfields` (`{opção: true/false/null}`), `rptenabled` (`{max, desc}`), `clientlns` (lista que substitui os ClientLN) | grava o formulário do RCB de uma vez; cria TrgOps/OptFields/RptEnabled que faltarem. O detalhe do nó (`/node`) de um ReportControl traz `rcb` com valores efetivos, padrões do esquema, DataSets do LN, quantos membros disparam por dchg/qchg/dupd, Services/ReportSettings do IED e `avisos` (erro/aviso/info) |

O arquivo gravado pelo editor tem o mesmo conteúdo do original. A formatação muda pouco: a ordem das declarações
de namespace na raiz e, em casos raros, CDATA virando texto escapado. DataSets e RCBs extras de teste (tela RCBs)
não fazem parte do CID; para levá-los ao arquivo, use "Exportar CID com extras".

### Visualizador (mesmas sessões do editor, só leitura)

| Rota | Efeito |
|---|---|
| `GET /cid-editor/{sessao}/overview` | cabeçalho e histórico, IEDs e serviços, comunicação (IP, GOOSE/SMV), blocos de controle, DataSets com membros (referência MMS e descrição), ExtRef, ClientLN, subestação, Private e namespaces |
| `GET /cid-editor/{sessao}/export?tipo=pontos\|datasets\|controles\|comunicacao` | CSV (`;`, UTF-8 com BOM) |
| `GET /cid-editor/{sessao}/compare?com=base\|<sessao>&sem_nome=` | diferenças por categoria (IED, LN, instâncias DOI/DAI, DataSet, bloco de controle, comunicação, tipo). `sem_nome` ignora o nome do IED; o padrão é ignorar quando cada arquivo tem um IED com nomes diferentes. O texto `a → b` vai do outro arquivo para este |

Operação nova do editor: `{"op": "limpar"}` remove os tipos sem uso, repetindo até não sobrar nenhum.

## OPMSK

- `GET /opmsk?valor=10C830`: decodifica um valor.
- `GET /opmsk/bits`: os 24 bits com resumo, efeito e valor hexadecimal de cada um. A tela OPMSK usa esta rota para
  gerar o valor.
- `GET /opmsk/sugestao?ied=<id>`: bits que dependem do CID do IED. O bit 4 segue o DynDataSet nos Services; o
  bit 5 indica se todos os RCBs estão no LLN0. Também traz observações sobre BRCB/URCB (bits 17 e 19) e Edição 2
  (bit 21). `ligar: null` significa "avaliar".

## Configuração

| Rota | Quem | Efeito |
|---|---|---|
| `GET /settings` | todos | padrões do servidor: `novo_ied` (perfil `auto` ou id, porta, max_associacoes), `sage.opmsk_padrao`, `editor.vista_inicial` |
| `PUT /settings` | administrador | grava (parcial; valida cada campo) em `data/config.json` |
| `GET /users`, `POST /users`, `PATCH /users/{nome}`, `DELETE /users/{nome}` | administrador | usuários locais. Mudar o papel ou a senha, ou remover, encerra os logins abertos daquele usuário. O último administrador não pode ser rebaixado nem removido, e ninguém remove o próprio usuário |
| `POST /me/password` `{atual, nova}` | todos (inclusive leitura) | troca a própria senha (só usuários locais) |
| `GET /system` | administrador | versão, endereço, pasta de dados, login/LDAP, IEDs e uso de disco |
| `GET /backup` | administrador | ZIP com banco (cópia consistente), CIDs, perfis, rascunhos do editor e config.json |

`POST /api/v1/logout` encerra a sessão do token enviado (vale para todos os papéis).

## Comandos (serviço de controle) e lógicas

O simulador atende Operate (`…$CO$<DO>$Oper`), Select (leitura de `$SBO`), SelectWithValue (`$SBOw`) e Cancel,
conforme o ctlModel do CF. Recusas mandam LastApplError (AddCause) antes da resposta do Write; com segurança
reforçada (ctlModel 3 e 4) sai CommandTermination positiva quando o estado chega à posição pedida, ou negativa
(Time-limit-over) no fim do operTimeout. Sem lógica, o estado do próprio DO faz eco do comando.

| Rota | Papel | O que faz |
|---|---|---|
| `GET /ieds/{id}/controles` | todos | objetos de controle: ctlModel, serviços, estado, seleção, bloqueios ativos, eco ou lógica |
| `POST /ieds/{id}/controles/comando` | operação | `{chave, valor}`: comando de teste com as mesmas verificações de um comando MMS |
| `PATCH /ieds/{id}/controles/config` | administrador | `{chave: {tempo_ms, timeout_ms, rejeitar_mesma_posicao}}` |
| `GET /ieds/{id}/comandos` | todos | registro: objeto, endereço MMS, serviço, ctlVal, origem (orCat/orIdent), ctlNum, T, Test, Check, resultado/AddCause, terminação |
| `DELETE /ieds/{id}/comandos` | administrador | apaga o registro |
| `GET /ieds/{id}/logicas` | todos | lógicas, catálogo de blocos, pontos referenciados e objetos de controle |
| `POST /ieds/{id}/logicas`, `PUT /ieds/{id}/logicas/{lid}`, `DELETE …/{lid}` | administrador | cria, grava (valida e já põe para rodar) ou apaga |
| `GET /ieds/{id}/logicas/estado` | todos | saída de cada bloco ao vivo, último comando por bloco, bloqueios ativos |

### Novo CID para o mesmo relé (`POST /ieds/{id}/reimport`)

Trocar o CID do IED mantém as lógicas, a configuração dos comandos e o estado dos pontos. Corpo:
`{token, ied_scl?, simular?, mapa?, confirmar?}` (`token` vem de `POST /cid`).

- `simular: true` não muda nada e devolve a prévia: `mantidos`, `removidos`, `novos`, `logicas` e `referencias`,
  ou seja, cada ponto ou objeto de controle usado por lógica ou por `controles/config` que não existe no CID novo:
  `{tipo: "ponto"|"controle", ref, cdc, desc, usos: [{logica, bloco}], sugestao, candidatos}`. A sugestão é o item
  só do CID novo com o mesmo DO/FC num LN da mesma classe (mudou prefixo, instância ou LD) ou com a mesma descrição
  e CDC, quando há um único candidato.
- `mapa: {antigo: novo}` reescreve essas referências nas lógicas, leva a configuração do comando e o estado salvo do
  ponto para o endereço novo e então carrega o CID. A resposta traz também `logicas`, `remapeados` e `orfas`
  (referências que ficaram sem ponto).

A tela do IED ("Subir novo CID do mesmo relé") e a tela Lógicas ("Subir novo CID") mostram a prévia com a
sugestão já escolhida antes de confirmar.

Blocos: `entrada`, `comando`, `const`, `and`, `or`, `xor`, `not`, `ton`, `tof`, `pulso`, `sr`, `comparar`, `saida`,
`bloqueio`. Evento de WebSocket novo: `comando`.
