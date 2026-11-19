# Changelog

## [1.1.0] — 2026-11-19
### Adicionado
- Câmera sobreposta ao vídeo, com cantos pré-definidos, 3 tamanhos e opção de espelhar.
- Prévia ao vivo durante a gravação; a câmera pode ser arrastada na prévia para mudar de posição.
- Pausar/continuar a gravação sem cortar o arquivo, com a prévia ao vivo durante a pausa.
- Opção "Começar pausado".
- `--check` também lista as câmeras detectadas.
- `.gitignore` e versionamento com git.

### Removido
- Captura de imagem (o programa agora é focado em vídeo).

### Corrigido
- Quadros com timestamp duplicado no início (câmera lenta para iniciar) corrompiam o MP4.

## [1.0.0] — 2026-11-19
- Versão inicial: gravação de tela via portal + PipeWire, áudio do sistema e microfone, MKV/MP4/WebM.
