# Legendas YT — PT ⇄ EN

App desktop em Python que toca um vídeo do YouTube e mostra, sincronizadas, a
legenda original e a tradução (inglês → português ou português → inglês).

## Instalar e rodar

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |  Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
# ou já abrindo um vídeo:
python app.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"
```

**Windows 10/11:** funciona direto (usa o WebView2 do Edge, já instalado).
**Ubuntu:** instale um motor web para o pywebview, por exemplo
`sudo apt install python3-gi gir1.2-webkit2-4.1 gstreamer1.0-plugins-good gstreamer1.0-libav`
(ou `pip install "pywebview[qt]"`).

## Como usar

1. Cole o link do vídeo — ele abre e começa a tocar sozinho.
2. **Automático** escolhe a melhor legenda disponível (prefere a feita por humanos);
   force **Inglês → Português** ou **Português → Inglês** se quiser.
3. A tradução do começo chega em 1–2 s; o resto vai sendo preenchido enquanto o vídeo toca.
4. Extras: A−/A+ (tamanho da legenda), "Só tradução"/"Só original",
   ☰ lista de falas (clique para pular até a fala), ⛶ tela cheia com legendas (Esc sai).
   A janela pode ficar bem pequena (até 320×200): abaixo de uns 720×460, o menu e a lista de
   falas somem e fica só o vídeo, com a legenda proporcional a ele. Aumente a janela para o
   menu e a lista voltarem.
5. **Recentes** lista os últimos 30 vídeos abertos, com o ponto onde você parou; clique para
   continuar dali. O **×** ao lado de cada vídeo o tira da lista.
6. Quando um vídeo termina, o app toca o de baixo na lista de Recentes (marcado **a seguir**),
   de onde ele tinha parado, e assim por diante até o fim da lista. Um link novo entra no topo;
   um vídeo aberto pela lista fica no lugar dele, para a sequência seguir dali. Se um vídeo da
   sequência não puder tocar (ex.: o dono não permite reprodução fora do YouTube), o app pula
   para o seguinte.
7. Ao abrir o app de novo, ele volta no último vídeo assistido, parado onde estava,
   com o mesmo tamanho de legenda e o mesmo modo (original, tradução ou os dois).
   O histórico e essas preferências ficam em `%APPDATA%\LegendasYT\` (Windows) ou
   `~/.local/share/LegendasYT/` (Linux).

## Como funciona

- `youtube-transcript-api` baixa a legenda do vídeo (manual ou automática), com tempos.
- `tradutor.py` traduz as falas em lotes pelo Google Tradutor (endpoint gratuito, sem chave),
  sempre uma tradução por fala:
  1. lote com uma fala por linha (o Google vê o contexto das frases);
  2. se o Google juntar duas falas numa linha, o mesmo lote vai de novo num pedido só, com cada
     fala num item separado: a resposta traz sempre uma tradução por fala;
  3. se isso também falhar, fala por fala (um pedido por fala, mais lento).
  O trecho onde o vídeo está é traduzido primeiro. Lotes que falharem são tentados de novo
  após 5, 15 e 30 s, e o erro aparece em vermelho no topo da janela.
- Se o Google falhar, entra a reserva: o MyMemory (gratuito, sem cadastro e sem e-mail). Ele aceita
  até 5.000 caracteres por dia (umas 200 falas, alguns minutos de vídeo) e 500 bytes por pedido,
  então as falas vão em lotes menores, a partir do trecho onde o vídeo está; se as linhas não
  baterem, o lote é dividido até casar. Quando a cota do dia acaba, o app avisa e espera o Google.
- Se o Google bloquear por excesso de pedidos (HTTP 429), o app para de mandar pedidos a ele,
  mostra a hora da próxima tentativa (depois de 2, 5, 10 e então a cada 15 min, ou do tempo que o
  Google pedir) e retoma sozinho; daí em diante, manda no máximo 1 pedido a cada 2 s.
- A reserva antiga, da Microsoft, saiu: o endpoint gratuito usado pelo Edge passou a responder
  404 (novembro de 2026), e o Bing Tradutor web junta as falas.
- A interface (`ui/index.html`) usa a API oficial do player do YouTube e consulta o tempo
  do vídeo 10×/s para mostrar a fala certa.
- A interface é servida por um servidor local e aberta em `http://localhost:8765` — por nome,
  não por IP: com a página em `127.0.0.1` o YouTube recusa alguns vídeos (ex.: compilações
  de música) com "Este vídeo não está disponível", mesmo quando o dono permite incorporar.

## Se a tradução não aparecer

1. Leia a mensagem em vermelho no topo da janela.
2. Rode o diagnóstico (não abre janela; testa os dois modos do Google, a reserva MyMemory e as
   legendas do vídeo):
   ```bash
   python app.py --diagnostico "https://www.youtube.com/watch?v=XXXXXXXXXXX"
   ```
3. Os detalhes de cada erro ficam em `legendas-yt.log`, na pasta do app.
4. "O Google bloqueou temporariamente as traduções por excesso de pedidos (HTTP 429)": o Google
   barrou este IP por um tempo, que pode passar de uma hora (vídeos muito longos ou muitos vídeos
   seguidos). Não precisa fazer nada: enquanto isso o MyMemory traduz o trecho que está passando
   (até acabar a cota do dia), e o app mostra a hora da próxima tentativa no Google e volta a
   usá-lo sozinho.

## Limitações

- Precisa que o vídeo tenha legenda (manual ou automática). Sem legenda nenhuma, não há o que traduzir.
- Vídeos cujo dono bloqueou reprodução fora do YouTube (alguns clipes de gravadoras)
  não tocam no app — aparece "não permite reprodução fora do YouTube".
- As legendas vêm de uma biblioteca não oficial e a tradução usa os endpoints gratuitos do
  Google Tradutor e, na reserva, do MyMemory (5.000 caracteres por dia: não dá para um vídeo
  inteiro). Se algo parar de funcionar, atualize com
  `pip install -U -r requirements.txt` e rode o diagnóstico.

## Gerar um .exe (opcional)

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name LegendasYT --add-data "ui;ui" app.py   # Windows
pyinstaller --noconsole --onefile --name LegendasYT --add-data "ui:ui" app.py   # Linux
```
O executável fica em `dist/`.
