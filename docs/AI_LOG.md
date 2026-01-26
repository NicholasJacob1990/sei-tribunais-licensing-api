# AI Log - SEI Tribunais Licensing API

## 2026-01-26 - Paridade Playwright/Extensao MCP

### Arquivos Alterados
- `app/services/playwright_automation.py` - Adicionadas 10 novas funcoes SEI
- `app/api/endpoints/mcp_server.py` - Roteamento das novas funcoes Playwright

### Analise Realizada
Comparacao entre funcionalidades da Extensao Chrome vs Playwright:

**Antes:**
- Extensao: 50+ acoes (login, documentos, assinatura, tramitacao, blocos, etc.)
- Playwright: 4 acoes (login, search_process, screenshot, get_page_content)

**Depois:**
- Playwright: 14 acoes implementadas

### Novas Funcoes Playwright
1. `open_process` - Abre/navega para processo
2. `list_documents` - Lista documentos do processo
3. `get_status` - Consulta andamento/historico
4. `create_document` - Cria novo documento
5. `sign_document` - Assina documento eletronicamente
6. `forward_process` - Tramita processo
7. `navigate` - Navega para URL
8. `click` - Clica em elemento
9. `fill` - Preenche campo
10. `logout` - Faz logout

### Decisoes Tomadas
- Playwright usa seletores CSS flexiveis (multiplos fallbacks)
- Cada funcao tem tratamento de erro individual
- Mantida compatibilidade com interface MCP existente

### Gap Restante
A extensao ainda tem mais funcionalidades que o Playwright:
- Blocos de assinatura (create_block, sign_block, release_block)
- Upload de documentos
- Anexacao/relacionamento de processos
- Listagem de usuarios/unidades
- Anotacoes e ciencias

### Proximos Passos Sugeridos
1. Implementar funcoes de bloco no Playwright
2. Adicionar upload de documentos
3. Testes de integracao Playwright
