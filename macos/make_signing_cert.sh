#!/bin/sh
# Tworzy lokalny certyfikat podpisu „LiveDub Local” (raz na Maca).
# Dzięki niemu każda wersja LiveDub ma ten sam podpis i macOS pamięta zgody
# (Nagrywanie ekranu, dźwięk) także po automatycznej aktualizacji.
set -eu

NAME="LiveDub Local"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -v -p codesigning | grep -q "$NAME"; then
  echo "Certyfikat „${NAME}” już jest — nic do zrobienia."
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

# systemowy openssl (LibreSSL) — jego .p12 macOS importuje bez problemów
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -config "$TMP/cert.cnf" -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
/usr/bin/openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -name "$NAME" -out "$TMP/id.p12" -passout pass:livedub

security import "$TMP/id.p12" -k "$KEYCHAIN" -P livedub -T /usr/bin/codesign >/dev/null
echo "Za chwilę macOS zapyta o hasło — to zgoda na zaufanie certyfikatowi do podpisu kodu."
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$TMP/cert.pem"

if security find-identity -v -p codesigning | grep -q "$NAME"; then
  echo "Gotowe: certyfikat „${NAME}” utworzony."
  echo "Teraz podpisz aplikację:  codesign --force --deep --sign \"$NAME\" /Applications/LiveDub.app"
  echo "i jeszcze raz daj zgodę w Ustawieniach → Prywatność → Nagrywanie ekranu (ostatni raz)."
else
  echo "Nie udało się — certyfikat nie jest widoczny do podpisu." >&2
  exit 1
fi
