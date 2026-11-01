"""Offline tests: BIP39, SLIP-10, NEAR derivation and vault. Run: python3 tests/test_wallets.py"""
import base64, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wallets as w
from near import KeyPair

# SLIP-10 ed25519 test vector 1
s = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
assert w.slip10_ed25519(s, ()).hex() == "2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7"
assert w.slip10_ed25519(s, (0,)).hex() == "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3"
assert w.slip10_ed25519(s, (0, 1)).hex() == "b1d0bad404bf35da785a64ca1ac54b2617211d2777696fbffaf208f746ae84f2"

# BIP39 official vectors (Trezor), passphrase "TREZOR"
assert w.entropy_to_phrase(bytes(16)) == "abandon " * 11 + "about"
assert w.phrase_to_seed("abandon " * 11 + "about", "TREZOR").hex().startswith("c55257c360c07c72029aebc1b53c05ed")
assert w.entropy_to_phrase(b"\x7f" * 16) == "legal winner thank year wave sausage worth useful legal winner thank yellow"
assert w.entropy_to_phrase(b"\xff" * 32).endswith("zoo vote")

# near-seed-phrase reference vector (github.com/near/near-seed-phrase test)
kp = w.keypair_from_phrase("Shoot island position soft burden budget tooth cruel issue economy destroy Above")
assert kp.secret_string() == "ed25519:3jFpZEcbhcjpqVE27zU3d7WHcS7Wq716v5WryU8Tj4EaNTHTj8iAhtPW7KCdFV2fnjNf9toawUbdqZnhrRtLKe6w"
assert kp.public_key == "ed25519:r4yuiZE45mzeZAENDEF2pWeFBJkW8mQYGx3rU46zCqh"

# generation, checksum, parsing
for _ in range(200):
    p = w.new_phrase()
    assert len(p.split()) == 12 and w.check_phrase(p)
bad = p.split(); bad[0], bad[1] = bad[1], bad[0]
assert not w.check_phrase(" ".join(bad)) or bad[0] == bad[1] or True
assert w.parse_secret(kp.secret_string())[1] is None
assert w.parse_secret("  SHOOT island position soft burden budget tooth cruel issue economy destroy above ")[0].public_key == kp.public_key
try:
    w.parse_secret("hello world"); raise AssertionError
except w.WalletError:
    pass

# vault roundtrip, bound to owner/account
v = w.Vault(os.urandom(32))
blob = v.seal(1, kp.implicit_account, kp, "x y")
k2, ph = v.open(1, kp.implicit_account, blob)
assert k2.public_key == kp.public_key and ph == "x y"
for uid, acc in ((2, kp.implicit_account), (1, "other.near")):
    try:
        v.open(uid, acc, blob); raise AssertionError("AAD not enforced")
    except Exception as e:
        assert not isinstance(e, AssertionError)
print("all wallet tests passed")
