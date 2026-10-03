# Biztonsági hibák jelentése

Biztonsági hibát ne nyilvános issue-ban jelents. Használd a GitHub privát jelentési
űrlapját: **Security → Report a vulnerability**
(<https://github.com/trozgan/Konyvhang/security/advisories/new>).

Írd le, mit találtál, hogyan lehet előidézni, és milyen hatása van. A jelentésre egy héten
belül válaszolok. A javítás után a hibát nyilvános advisoryban írom le, és ha kéred, név
szerint megköszönöm.

## Mire figyelj

- A program külső parancsokat futtat (`claude`, `codex`, `ffmpeg`, `ffprobe`, `epubcheck`),
  és nem megbízható bemenetet dolgoz fel: az EPUB-fájlt és a modell válaszát. Az ezekből
  eredő hibák (például parancsbefecskendezés vagy fájlírás a munkamappán kívülre) a
  legfontosabbak.
- Az API-kulcsok csak környezeti változóból jönnek, a program nem írja fájlba őket. Ha
  mégis kulcsot találsz naplóban vagy munkamappában, az biztonsági hiba.
- Jelentéshez ne csatolj jogvédett könyvet; egy kicsi, saját készítésű EPUB elég.

Csak a `main` ág legfrissebb állapota kap javítást.
