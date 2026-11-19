# Changelog

## [1.2.0] — 2026-11-19
### Alterado
- A câmera agora abre numa **janela flutuante própria**, que mostra somente a imagem da câmera
  (retângulo ou círculo, 3 tamanhos). Ela é posicionada arrastando a janela e entra no vídeo
  junto com a tela. Clique direito → "Sempre no topo".
- A câmera abre assim que a opção é marcada, para posicionar antes de gravar.
- A gravação usa só a tela (sem compositor), o que deixa o pipeline mais leve.

### Corrigido
- A janela do aplicativo ficava enorme ao gravar com câmera: a prévia mostrava a tela inteira
  (com efeito de "espelho infinito") dentro da janela de configurações.
- A câmera não aparecia quando "Minimizar ao gravar" estava ligado (a prévia ficava na janela minimizada).
- Se o formato preferido da câmera falhar, o app tenta de novo com `decodebin`, e mostra o erro
  na interface em vez de falhar sem aviso.

### Removido
- Sobreposição da câmera via compositor, posições pré-definidas e prévia ao vivo da tela.

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
