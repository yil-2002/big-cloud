#!/usr/bin/env bash
# =============================================================================
# verify.sh — ish papkasidagi o'zgarishlarni tekshirish (agy o'zi yugurtiradi)
# Hech narsani o'zgartirmaydi. Chiqadi: OK/XATO + qisqa sabab.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
FAIL=0
# --fast: faqat statik tekshiruvlar (node/CSS/ID/hex). To'liq pytest — darvozada
# (promote.sh) yugurtiriladi, shuning uchun agy ichki tsiklida takrorlanmaydi.
FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

echo "== 1) JS sintaksisi =="
if command -v node >/dev/null 2>&1; then
  python3 - <<'PY'
import re, pathlib
h = pathlib.Path('web/index.html').read_text(encoding='utf-8')
b = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', h, re.S)
pathlib.Path('/tmp/agy_verify.js').write_text("\n;\n".join(b), encoding='utf-8')
PY
  if node --check /tmp/agy_verify.js; then echo "  OK: JS sintaksisi toza"; else echo "  XATO: JS sintaksisi"; FAIL=1; fi
else
  echo "  OGOHLANTIRISH: node yo'q — JS tekshirilmadi"
fi

echo "== 2) CSS / ID / hex =="
python3 - <<'PY'
import re, pathlib, sys
h = pathlib.Path('web/index.html').read_text(encoding='utf-8')
css = re.search(r'<style>(.*?)</style>', h, re.S)
css = css.group(1) if css else ''
c = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
bad = 0
if c.count('{') != c.count('}'):
    print(f"  XATO: CSS qavslari {c.count('{')} vs {c.count('}')}"); bad = 1
else:
    print("  OK: CSS qavslari balansda")
b = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', h, re.S)
script = max(b, key=len) if b else ''
ref = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', script))
ids = set(re.findall(r'\bid="([^"]+)"', h))
missing = sorted(ref - ids)
if missing:
    print(f"  XATO: markupda yo'q ID'lar: {missing}"); bad = 1
else:
    print("  OK: JS'dagi barcha ID'lar markupda")
# hex ranglar (token bloklaridan tashqari)
kept, i = [], 0
while True:
    m = re.search(r":root(?:\[[^\]]*\])?\s*\{", c[i:])
    if not m:
        kept.append(c[i:]); break
    s = i + m.start(); kept.append(c[i:s]); d = 0; cur = i + m.end() - 1
    while cur < len(c):
        if c[cur] == '{': d += 1
        elif c[cur] == '}':
            d -= 1
            if d == 0: break
        cur += 1
    i = cur + 1
leaked = sorted(set(re.findall(r"#[0-9A-Fa-f]{3,8}\b", "".join(kept))))
if leaked:
    print(f"  XATO: komponent CSS'ida hex rang: {leaked}"); bad = 1
else:
    print("  OK: hex rang yo'q")
sys.exit(1 if bad else 0)
PY
[ $? -ne 0 ] && FAIL=1

[ "$FAST" -eq 1 ] && { echo; [ "$FAIL" -eq 0 ] && echo "NATIJA: OK (fast)" || echo "NATIJA: XATO"; exit "$FAIL"; }

echo "== 3) Testlar (ixtiyoriy, jonli repo venv'i bilan) =="
if [ -x /root/big-cloud/venv/bin/python ]; then
  if ( cd /root/big-cloud && ./venv/bin/python -m pytest -p no:warnings -q --no-header tests/test_webapp.py 2>&1 | tail -3 ); then
    echo "  (testlar yuqorida)"
  fi
else
  echo "  OGOHLANTIRISH: jonli venv topilmadi — testlar o'tkazib yuborildi"
fi

echo
[ "$FAIL" -eq 0 ] && echo "NATIJA: OK" || echo "NATIJA: XATO (yuqoriga qara)"
exit "$FAIL"
