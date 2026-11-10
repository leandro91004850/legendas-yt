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

## Como funciona

- `youtube-transcript-api` baixa a legenda do vídeo (manual ou automática), com tempos.
- `tradutor.py` traduz as falas em lotes, sempre uma tradução por fala:
  1. Google Tradutor (lote com uma fala por linha);
  2. se falhar, Microsoft Translator (o mesmo serviço do tradutor do Edge);
  3. se o Google juntar falas e a Microsoft falhar, Google fala por fala.
  O trecho onde o vídeo está é traduzido primeiro. Lotes que falharem são tentados de novo
  após 5, 15 e 30 s, e o erro aparece em vermelho no topo da janela.
- A interface (`ui/index.html`) usa a API oficial do player do YouTube e consulta o tempo
  do vídeo 10×/s para mostrar a fala certa.
- A interface é servida por um servidor local e aberta em `http://localhost:8765` — por nome,
  não por IP: com a página em `127.0.0.1` o YouTube recusa alguns vídeos (ex.: compilações
  de música) com "Este vídeo não está disponível", mesmo quando o dono permite incorporar.

## Se a tradução não aparecer

1. Leia a mensagem em vermelho no topo da janela.
2. Rode o diagnóstico (não abre janela; testa os dois tradutores e as legendas do vídeo):
   ```bash
   python app.py --diagnostico "https://www.youtube.com/watch?v=XXXXXXXXXXX"
   ```
3. Os detalhes de cada erro ficam em `legendas-yt.log`, na pasta do app.

## Limitações

- Precisa que o vídeo tenha legenda (manual ou automática). Sem legenda nenhuma, não há o que traduzir.
- Vídeos cujo dono bloqueou reprodução fora do YouTube (alguns clipes de gravadoras)
  não tocam no app — aparece "não permite reprodução fora do YouTube".
- As legendas vêm de uma biblioteca não oficial e os tradutores são os endpoints gratuitos
  do Google e da Microsoft. Se algum deles mudar, atualize com
  `pip install -U -r requirements.txt` e rode o diagnóstico.

## Gerar um .exe (opcional)

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name LegendasYT --add-data "ui;ui" app.py   # Windows
pyinstaller --noconsole --onefile --name LegendasYT --add-data "ui:ui" app.py   # Linux
```
O executável fica em `dist/`.
