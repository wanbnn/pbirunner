# PBI Runner

Plataforma web multiusuário para arquivos Power BI. Ela combina as definições PBIR/TMDL com os dados VertiPaq embutidos para executar medidas, filtros e gráficos sem abrir o Power BI Desktop.

## Executar

O caminho recomendado usa [uv](https://docs.astral.sh/uv/), que instala automaticamente o Python 3.13 e as dependências travadas em `uv.lock`:

```bash
uv run pbi-runner
```

Abra `http://127.0.0.1:8765`. No primeiro acesso, cadastre o administrador inicial. A ferramenta cria automaticamente o primeiro workspace.

Também é possível definir o diretório persistente do banco e dos relatórios e impedir a abertura automática do navegador:

```bash
uv run pbi-runner --data-dir /srv/pbi-runner --host 0.0.0.0 --port 9000 --no-browser
```

Sem `--data-dir`, os dados ficam em `~/.local/share/pbi-runner`. A variável `PBI_RUNNER_DATA` também pode definir esse caminho.

## Docker Compose

Construa e inicie o serviço:

```bash
docker compose up -d --build
```

Depois, acesse `http://localhost:8765`. Para publicar em outra porta:

```bash
PBI_RUNNER_PORT=9000 docker compose up -d
```

O volume nomeado `pbirunner_data` é montado em `/data` e mantém o banco SQLite, sessões, workspaces, projetos PBIP extraídos e arquivos PBIX enviados mesmo após recriar o contêiner.

Para acompanhar o serviço:

```bash
docker compose logs -f pbirunner
docker compose ps
```

Para executar os testes:

```bash
uv run python -m unittest discover -s tests -v
```

## Recursos

- Primeiro acesso com cadastro exclusivo do administrador inicial.
- Autenticação persistente, senhas PBKDF2-SHA256 e sessões HttpOnly.
- Múltiplos workspaces com papéis de proprietário, administrador, editor e visualizador.
- Administração global de usuários e membros dos workspaces.
- Upload de `.pbix` ou `.zip` contendo um projeto `.pbip`.
- Modo de leitura em viewport completa com barra superior, seletor de página e retorno ao workspace.
- Abre projetos `.pbip` modernos e `.pbix` que contenham definição PBIR.
- Mantém páginas, dimensões, posições, ordem de camadas, plano de fundo e imagens.
- Decodifica as tabelas reais do `DataModel`/ABF com PBIXRay e Xpress9.
- Executa as medidas DAX usadas pelo relatório em contexto de filtro.
- Propaga filtros pelas relações muitos-para-um do modelo tabular.
- Renderiza cartões, tabelas, matrizes, segmentações e gráficos com dados reais.
- Usa Apache ECharts 6.1.0 auto-hospedado, sem depender de CDN durante a execução.
- Traduz paleta, cores por categoria, títulos, legendas, eixos, grades, rótulos, linhas e marcadores definidos no tema e nos visuais PBIR.
- Permite filtrar os demais visuais clicando em barras, setores, pontos e categorias dos gráficos.
- Recalcula a página quando uma segmentação é alterada.
- Exibe campos e papéis associados a cada visual, além do JSON PBIR original.
- Lê tabelas, colunas, medidas DAX, pastas e relacionamentos de modelos TMDL.

## Papéis e permissões

- `owner`: gerencia membros, papéis e relatórios do workspace.
- `admin`: gerencia membros e relatórios do workspace.
- `editor`: envia e visualiza relatórios.
- `viewer`: somente navega e interage com relatórios publicados.
- Administrador global: cadastra usuários e administra todos os workspaces.

Uploads ZIP têm validação contra caminhos inseguros e limite de 1 GB para o conteúdo descompactado.

## Runtime DAX

O executor vetorizado atual cobre as construções usadas pelo projeto de referência: `CALCULATE`, `FILTER`, `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `COUNTROWS`, `DISTINCTCOUNT`, `DIVIDE`, `IF`, `IN`, `NOT`, `ISBLANK`, `CONTAINSSTRING`, operadores matemáticos, comparações e operadores booleanos. A cobertura é incremental; funções DAX ainda não implementadas são reportadas por visual, sem inventar valores.

O PBIP normalmente não versiona os dados locais. Quando seu `cache.abf` contém zero linhas, o viewer procura um PBIX irmão com o mesmo nome. Arquivos Power BI usados em testes locais não são enviados ao repositório porque podem conter dados reais.

O decodificador de armazenamento é o projeto MIT [PBIXRay](https://github.com/Hugoberry/pbixray). O executor DAX, o contexto de filtros, a propagação de relacionamentos e o planejador de consultas dos visuais ficam em `pbi_viewer/dax.py` e `pbi_viewer/engine.py`.

## Limites técnicos

- DirectQuery ainda requer acesso à fonte externa; somente modelos Import possuem linhas dentro do arquivo.
- RLS, calculation groups, relações bidirecionais, bookmarks e visuais personalizados ainda não são executados.
- A aparência dos gráficos é reconstruída no ECharts a partir das propriedades PBIR. Recursos visuais exclusivos ou não documentados do Power BI ainda podem apresentar diferenças.

PBIX antigos que armazenam somente `Report/Layout` não têm PBIR aberto. Para visualizá-los, abra no Power BI Desktop e salve como projeto PBIP ou habilite o formato PBIR antes de salvar.

## Estrutura

- `pbi_viewer/parser.py`: leitura PBIP, PBIX, PBIR e TMDL.
- `pbi_viewer/dax.py`: avaliação vetorizada de expressões DAX.
- `pbi_viewer/engine.py`: extração VertiPaq, relacionamentos e consultas por visual.
- `pbi_viewer/platform.py`: usuários, sessões, workspaces, papéis e catálogo SQLite.
- `pbi_viewer/server.py`: servidor HTTP e APIs locais.
- `pbi_viewer/static/`: interface do viewer.
- `pbi_viewer/static/vendor/echarts.min.js`: runtime gráfico auto-hospedado.
- `tests/`: testes de integração com o projeto de referência.
