# Uchinchi tomon fayllari (lokal nusxa)

Tashqi CDN ishlatilmaydi: skriptlar shu yerdan, ilova bilan bir manbadan yuklanadi
(supply-chain xavfi yo'q, Telegram WebView'da tezroq).

| Fayl | Manba | Versiya | Litsenziya |
|---|---|---|---|
| `three.r134.min.js` | npm `three@0.134.0` → `build/three.min.js` | r134 | MIT (c) three.js authors |
| `vanta.clouds.min.js` | npm `vanta@0.5.24` → `dist/` | 0.5.24 | MIT (c) Teng Bao |
| `vanta.clouds2.min.js` | npm `vanta@0.5.24` → `dist/` | 0.5.24 | MIT (c) Teng Bao |
| `noise.png` | `scripts`siz yaratilgan 256×256 RGBA tasodifiy shovqin (seed 20261121) — vantajs.com dagi `gallery/noise.png` o'rnini bosadi | — | loyiha |

Yangilash: `npm pack three@0.134.0 vanta@<versiya>` va fayllarni shu yerga nusxalang
(sha256 ni quyida yangilang).
    74782bdbcf6518f7745ed77035968fcae95ed4ab5c9a0f90cf646a69c20785ec  three.r134.min.js
    97ae7dac61b9d1aae2849e948e0e5c1eac87e4871d65cce580270015ce59d7f9  vanta.clouds.min.js
    a0f944ef40ca2356f8368e714a903d49104d4d39f524e3a597caeabf9cb9a9d9  vanta.clouds2.min.js
    69e864e9164d70e3f99d3d0f5fe3a358260986ac2a4d794969df1283ab88a544  noise.png
