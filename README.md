# Oliver Screen Record

Programa simples de gravação de tela para Ubuntu/Debian modernos (Ubuntu 25.04, 25.10 e 26.x).
Grava junto o **áudio do sistema**, o **microfone** e a **câmera** sobreposta (arrastável), e permite **pausar/continuar**.
Funciona em **Wayland** (padrão) e em X11.

## Instalação

```bash
chmod +x install.sh
./install.sh            # instala as dependências via apt, o comando e o atalho no menu
./install.sh --uninstall
```

Para rodar sem instalar: `python3 oliver-screen-record.py` (as dependências do `install.sh` precisam estar instaladas).
Para conferir plugins e câmeras detectadas: `python3 oliver-screen-record.py --check`.

## Recursos

| Recurso | Detalhes |
|---|---|
| Vídeo | Tela inteira, uma janela ou escolha na hora; 10–60 FPS; 3 níveis de qualidade |
| Formatos | **MKV** (H.264 + Opus, recomendado e resistente a falhas), **MP4** (H.264 + AAC) e **WebM** (VP8 + Opus) |
| Áudio | Sistema (monitor da saída) e/ou microfone, mixados numa única faixa; dá para escolher o dispositivo |
| Câmera | Sobreposta ao vídeo, com 4 cantos pré-definidos ou posição livre (arrastando na prévia), 3 tamanhos e opção de espelhar. Aceita câmeras V4L2 (USB/UVC), PipeWire e libcamera |
| Pausa | Pausa e continua sem cortar o arquivo: o trecho pausado simplesmente não entra no vídeo. A opção "Começar pausado" deixa ajustar a câmera antes de gravar |
| Outros | Cursor visível ou oculto, minimizar ao gravar, lembrar a tela escolhida, configurações salvas |

Os arquivos são salvos em `~/Vídeos/Oliver Screen Record/` (a pasta pode ser trocada na interface), com nomes do tipo
`gravacao_AAAA-MM-DD_HH-MM-SS.mkv`.

### Posicionando a câmera

1. Escolha a posição inicial (um dos cantos) e o tamanho antes de gravar.
2. Durante a gravação, a janela mostra uma **prévia ao vivo**. Arraste a câmera dentro dela para movê-la. A posição personalizada fica salva para as próximas gravações.
3. Para ajustar sem que isso apareça no vídeo, use **Pausar** (a prévia continua ao vivo), mova a câmera e clique em **Continuar**. Outra opção é marcar **Começar pausado**.

> Gravando a tela inteira com a janela do programa visível, ela aparece no vídeo. Por isso o "Minimizar ao gravar" vem ligado por padrão.

A gravação pode ser encerrada pelo botão **Parar** ou pelo indicador de gravação do sistema (o ícone vermelho do GNOME).
Nos dois casos o arquivo é finalizado corretamente.

## Arquitetura

```
GTK 4 (interface + prévia via gtk4paintablesink)
 ├─ xdg-desktop-portal (D-Bus) — ScreenCast: CreateSession → SelectSources → Start → OpenPipeWireRemote
 └─ GStreamer
     Tela   pipewiresrc → videorate → I420 → videoscale (par) ─┐
     Câmera v4l2src/pipewiresrc/libcamerasrc → videorate      ├→ compositor → tee ─┬→ [portão] → x264/vp8 ─┐
            → videoflip → videoscale ──────────────────────────┘                    └→ prévia             ├→ mux → arquivo
     Áudio  pulsesrc(monitor) + pulsesrc(mic) → audiomixer → [portão] → opus/aac ──────────────────────────┘
```

* No Wayland nenhum app captura a tela diretamente: a permissão passa pelo portal, que entrega um fluxo do PipeWire.
* O H.264 4:2:0 exige dimensões pares. A saída é fixada no primeiro quadro, arredondada para par.
* **Câmera**: a posição e o tamanho são propriedades do pad do `compositor`, alteradas ao vivo pelo arraste.
* **Pausa**: o pipeline continua rodando. Os "portões" (probes antes dos encoders) descartam os buffers e, ao continuar, aplicam um offset negativo (`Gst.Pad.set_offset`) igual ao tempo pausado. Por isso vídeo e áudio continuam sincronizados e a prévia não congela.
* Ao parar, o programa envia um EOS ao pipeline, para que o MP4 seja finalizado e não fique corrompido.

## Problemas comuns

* **Nenhum diálogo de seleção aparece**: confira se `xdg-desktop-portal` e o backend do seu ambiente (`-gnome`, `-kde` ou `-wlr`) estão instalados e rode `systemctl --user restart xdg-desktop-portal`.
* **Vídeo sem áudio do sistema**: rode `pactl get-default-sink` e verifique se a saída padrão é a que está tocando o som. Se não for, escolha a saída certa na lista.
* **Câmera não listada**: rode `--check`. Câmeras de notebooks recentes (MIPI/IPU6) precisam do `gstreamer1.0-libcamera`. Se a câmera estiver em uso por outro app (Meet, Zoom), feche-o.
* **Sem prévia**: instale `gstreamer1.0-gtk4`.
* **Logs detalhados**: `GST_DEBUG=2 oliver-screen-record`.
