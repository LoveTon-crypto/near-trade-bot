"""Every user-facing Russian fragment in the code has an English translation. Run: python3 tests/test_i18n.py"""
import json, os, re, subprocess, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import i18n
CYR = re.compile(r"[А-Яа-яЁё]")
out = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "_extract.py")] +
                     [os.path.join(ROOT, f) for f in ("bot.py", "ref.py", "near.py", "wallets.py")],
                     capture_output=True, text=True, check=True).stdout
frags = [f for f in json.loads(out) if f not in i18n.NOT_UI]
missing = [f for f in frags if CYR.search(i18n.to_en(f))]
for f in missing:
    print("MISSING:", repr(f))
assert not missing, f"{len(missing)} untranslated fragments"
bad = [v for v in i18n.EN.values() if CYR.search(v)]
assert not bad, bad
print(f"i18n ok: {len(frags)} fragments translated")
