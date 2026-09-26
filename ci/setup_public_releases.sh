#!/bin/sh
# Jednorazowo: publiczne wydania LiveDub bez płatnego certyfikatu Apple.
#  1) publiczne repo knehrybecki/livedub-releases — tylko paczki + instrukcja instalacji (kod zostaje prywatny),
#  2) stały certyfikat podpisu „LiveDub Release” dla CI (sekrety MAC_SIGN_P12, MAC_SIGN_PASSWORD) —
#     każda wersja ma ten sam podpis, więc macOS pamięta zgody (nagrywanie ekranu) po aktualizacji,
#  3) token RELEASES_TOKEN, którym CI publikuje wydania w publicznym repo.
# Każdy krok pomija się, jeśli już jest zrobiony.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_REPO="knehrybecki/gamereader"
PUB_REPO="knehrybecki/livedub-releases"
CERT_NAME="LiveDub Release"
BACKUP="$HOME/Library/Application Support/LiveDub/release-signing"

has_secret() {
  gh secret list -R "$SRC_REPO" --json name -q '.[].name' | grep -qx "$1"
}

# 1) publiczne repo z instrukcją
if gh repo view "$PUB_REPO" >/dev/null 2>&1; then
  echo "✓ $PUB_REPO już jest"
else
  gh repo create "$PUB_REPO" --public --description "LiveDub — polski lektor na żywo (Mac i Windows) — wydania do pobrania"
  echo "✓ utworzono $PUB_REPO"
fi
if ! gh api "repos/$PUB_REPO/contents/README.md" >/dev/null 2>&1; then
  gh api -X PUT "repos/$PUB_REPO/contents/README.md" \
    -f message="Instrukcja instalacji" \
    -f content="$(base64 < "$ROOT/ci/INSTALACJA.md" | tr -d '\n')" >/dev/null
  echo "✓ README z instrukcją instalacji"
fi

# 2) certyfikat podpisu dla CI
if has_secret MAC_SIGN_P12 && has_secret MAC_SIGN_PASSWORD; then
  echo "✓ certyfikat „$CERT_NAME” już jest w sekretach (nie twórz nowego — zmiana podpisu = wszyscy jeszcze raz dają zgody)"
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $CERT_NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF
  PASSWORD="$(uuidgen)"
  # systemowy openssl (LibreSSL) — jego .p12 macOS importuje bez problemów
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 7300 \
    -config "$TMP/cert.cnf" -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
  /usr/bin/openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
    -name "$CERT_NAME" -out "$TMP/id.p12" -passout "pass:$PASSWORD"
  base64 < "$TMP/id.p12" | tr -d '\n' | gh secret set MAC_SIGN_P12 -R "$SRC_REPO"
  printf '%s' "$PASSWORD" | gh secret set MAC_SIGN_PASSWORD -R "$SRC_REPO"
  # kopia zapasowa: sekretów GitHuba nie da się odczytać, a nowy certyfikat = wszyscy od nowa dają zgody
  mkdir -p "$BACKUP"
  chmod 700 "$BACKUP"
  cp "$TMP/id.p12" "$BACKUP/livedub-release.p12"
  printf '%s\n' "$PASSWORD" > "$BACKUP/password.txt"
  chmod 600 "$BACKUP/livedub-release.p12" "$BACKUP/password.txt"
  echo "✓ certyfikat „$CERT_NAME” w sekretach $SRC_REPO (kopia: $BACKUP)"
fi

# 3) token do publikowania w publicznym repo
if has_secret RELEASES_TOKEN; then
  echo "✓ RELEASES_TOKEN już jest"
else
  cat <<EOF

Ostatni krok — token, którym CI wrzuca wydania do $PUB_REPO:
  1. Otwórz https://github.com/settings/personal-access-tokens/new
  2. Repository access: Only select repositories → livedub-releases
  3. Permissions → Repository permissions → Contents: Read and write
  4. Generate token i wklej go poniżej (nie będzie widoczny).
EOF
  gh secret set RELEASES_TOKEN -R "$SRC_REPO"
  echo "✓ RELEASES_TOKEN zapisany"
fi

echo
echo "Gotowe. Następne wydanie z main trafi też do https://github.com/$PUB_REPO/releases"
