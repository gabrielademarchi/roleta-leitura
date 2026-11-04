# Roleta da próxima leitura

Ferramenta do Livros e etc: a pessoa diz o último livro que amou e a roleta sorteia,
entre os livros em alta na semana, um parecido.

```
index.html                  a página (lê data/pool.json; capas e busca fora do catálogo ao vivo)
build_pool.py               o robô: monta data/pool.json a partir do Goodreads + Open Library
data/pool.json              dados (vem com uma amostra de exemplo; o robô substitui)
data/misses.json            títulos que você quiser forçar a entrar no catálogo na próxima rodada
.github/workflows/weekly.yml   agenda: toda segunda (trending) e dia 1 (catálogo de sementes)
```

## Subir no GitHub (uma vez só)

1. Crie um repositório público no GitHub (ex.: `roleta-leitura`) e envie estes arquivos.
2. **Settings → Pages → Source: Deploy from a branch → `main` / root → Save.**
   A página fica em `https://SEU-USUARIO.github.io/roleta-leitura/`.
3. **Settings → Actions → General → Workflow permissions → "Read and write permissions" → Save**
   (é o que deixa o robô salvar o `pool.json` no repositório).
4. **Actions → "Atualizar roleta" → Run workflow**, marque "Reconstruir o catálogo" e rode.
   A primeira rodada demora (uns 2.000 livros, ~1,5 s cada): conte com 1–2 h.
   Nas segundas-feiras só o trending roda (~300 livros, uns 10 min).
5. Abra a página. Pronto — o link vai para a bio.

## Se o Goodreads bloquear o robô

Servidores do GitHub às vezes tomam 403 do Goodreads. O script percebe e cai sozinho no
modo `openlibrary` (API oficial): a roleta continua viva, com perfis um pouco mais pobres.
Se isso virar rotina, duas saídas: rodar o robô no seu computador pessoal de vez em quando
(`python build_pool.py --catalog` e dar `git push`), ou aceitar o modo Open Library como padrão.

Se o Goodreads mudar o HTML, o lugar para olhar é `gr_book()` e `gr_shelves()` em `build_pool.py`
(as duas funções que leem a página do livro e a página de shelves).

## Testar o robô localmente

```
pip install -r requirements.txt
python build_pool.py --limit 15          # só 15 livros, para ver se está lendo certo
python build_pool.py --catalog           # rodada completa
```

## Como funciona a escolha

- Cada livro vira um vetor com a proporção das suas 40 shelves de conteúdo no Goodreads
  (shelves como `to-read`, `kindle`, `2024` são ignoradas).
- A roleta calcula a similaridade de cosseno entre o livro que a pessoa amou e cada livro do
  trending, fica com os 15 mais parecidos, nunca a mesma série, mesmo autor com metade do peso,
  e sorteia ponderando `similaridade² × score de trending`.
- O score de trending combina a posição do livro nas listas "most read this week" por gênero
  do Goodreads (peso 1,0), nas shelves booktok (0,9) e no trending semanal do Open Library (0,5).
- O selinho do dragão mostra o clima do livro: cor pelas duas tags mais fortes, mais escuro
  quanto mais sombrio.

Títulos fora do catálogo são procurados ao vivo no Open Library e ganham um perfil aproximado
pelos *subjects*. Para que um título desses entre de vez no catálogo, acrescente o nome em
`data/misses.json` (ex.: `["The Lies of Locke Lamora"]`) — o robô resolve na próxima rodada.
