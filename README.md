# Oliver Screen Record

<img src="icons/com.oliver.ScreenRecord.svg" width="96" alt="Ícone do Oliver Screen Record">

Programa simples de gravação de tela para Ubuntu/Debian modernos (Ubuntu 25.04, 25.10 e 26.x).
Grava junto o **áudio do sistema**, o **microfone** e a **câmera** (numa janela flutuante), e permite **pausar/continuar**.
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
| Câmera | Janela flutuante só com a imagem da câmera (retângulo ou círculo, 3 tamanhos, espelhável), posicionada arrastando. Aceita câmeras V4L2 (USB/UVC), PipeWire e libcamera |
| Pausa | Pausa e continua sem cortar o arquivo: o trecho pausado simplesmente não entra no vídeo. A opção "Começar pausado" inicia a gravação já pausada |
| Outros | Cursor visível ou oculto, minimizar ao gravar, lembrar a tela escolhida, configurações salvas |

Os arquivos são salvos em `~/Vídeos/Oliver Screen Record/` (a pasta pode ser trocada na interface), com nomes do tipo
`gravacao_AAAA-MM-DD_HH-MM-SS.mkv`.

### Posicionando a câmera

1. Marque **Câmera**: abre uma janela pequena, sem bordas, mostrando só a câmera.
2. Arraste a janela até onde ela deve aparecer no vídeo. Tamanho, formato (retângulo/círculo) e espelho podem ser trocados a qualquer momento, inclusive durante a gravação.
3. Para ela ficar acima das outras janelas, clique com o botão direito nela → **Sempre no topo** (ou `Alt+Espaço` no GNOME).
4. Ao encerrar a gravação, a câmera fecha sozinha. Para fechar antes, desmarque a opção ou pressione `Alt+F4` sobre a janela.

> A câmera aparece no vídeo porque a janela dela é gravada junto com a tela. Se você gravar **uma janela** específica (em vez da tela inteira), a câmera não entra no vídeo.
> "Minimizar ao gravar" minimiza só a janela de configurações. A janela da câmera continua visível.

A gravação pode ser encerrada pelo botão **Parar** ou pelo indicador de gravação do sistema (o ícone vermelho do GNOME).
Nos dois casos o arquivo é finalizado corretamente.

## Arquitetura

```
GTK 4 (interface + janela da câmera via gtk4paintablesink)
 ├─ xdg-desktop-portal (D-Bus) — ScreenCast: CreateSession → SelectSources → Start → OpenPipeWireRemote
 └─ GStreamer
     Gravação  pipewiresrc → videorate → I420 → videoscale (par) → [portão] → x264/vp8 ─┐
               pulsesrc(monitor) + pulsesrc(mic) → audiomixer → [portão] → opus/aac ──┴→ mux → arquivo
     Câmera    v4l2src/pipewiresrc/libcamerasrc → videoflip → videoscale → gtk4paintablesink (janela flutuante)
```

* No Wayland nenhum app captura a tela diretamente: a permissão passa pelo portal, que entrega um fluxo do PipeWire.
* O H.264 4:2:0 exige dimensões pares. A saída é fixada no primeiro quadro, arredondada para par.
* **Câmera**: pipeline separado da gravação, exibido numa janela sem bordas. O `Gtk.Picture` tem tamanho fixo (`do_measure`) para a resolução da câmera não aumentar a janela. Se a negociação de formato falhar, o app tenta de novo com `decodebin`.
* **Pausa**: o pipeline continua rodando. Os "portões" (probes antes dos encoders) descartam os buffers e, ao continuar, aplicam um offset negativo (`Gst.Pad.set_offset`) igual ao tempo pausado. Por isso vídeo e áudio continuam sincronizados.
* Ao parar, o programa envia um EOS ao pipeline, para que o MP4 seja finalizado e não fique corrompido.

## Problemas comuns

* **Nenhum diálogo de seleção aparece**: confira se `xdg-desktop-portal` e o backend do seu ambiente (`-gnome`, `-kde` ou `-wlr`) estão instalados e rode `systemctl --user restart xdg-desktop-portal`.
* **Vídeo sem áudio do sistema**: rode `pactl get-default-sink` e verifique se a saída padrão é a que está tocando o som. Se não for, escolha a saída certa na lista.
* **Câmera não listada**: rode `--check`. Câmeras de notebooks recentes (MIPI/IPU6) precisam do `gstreamer1.0-libcamera`. Se a câmera estiver em uso por outro app (Meet, Zoom), feche-o.
* **Câmera não abre / "instale o pacote gstreamer1.0-gtk4"**: a janela da câmera precisa do `gstreamer1.0-gtk4`. Outros erros aparecem na linha de status. Para detalhes, rode pelo terminal.
* **Vídeo não aparece na pasta**: o caminho completo aparece no status. O log fica em `~/.cache/oliver-screen-record/oliver-screen-record.log`.
* **Logs detalhados**: `GST_DEBUG=2 oliver-screen-record`.
