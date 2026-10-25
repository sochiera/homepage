# Homepage release runbook

This procedure replaces only `/var/www/sochiera`. It does not edit nginx and does not deploy or remove the paint-by-number application.

1. Start from a clean tracked worktree. Create `.venv`, install `.[test]`, run pytest, and build with the command in the README (include `--protected-file content/protected.toml` and the operator's local `--password-file`).
2. Inspect `.build/site`, `build-manifest.json`, its hashes, and the archive listing. Confirm `git status --short` and `git ls-files` contain no generated story HTML and no password material. Protected entries must appear only as `data-protected` ciphertext supplied by `static/js/privacy.js` decryption. Compare every live `/pobierz/` file with the bundled snapshot (APK 1.2–1.7 and version metadata); preserve all downloads and link the version named by `mealspire-wersja.json`. Refresh the public snapshot if Mealspire has published a newer version. The switch checks download hashes, metadata and the homepage anchor under the deployment lock and refuses drift or unknown docroot files before moving the current tree.
3. Run `scripts/deploy.sh --host ubuntu@51.83.199.206`. The helper connects with `/home/jan/.ssh/pbn_vps` (or `$HOME/.ssh/pbn_vps`), so no SSH host alias or `~/.ssh/config` entry is required. For non-interactive operator automation, add `--yes`.
4. Record the printed release ID. Verify these resources on both public hostnames: `/`, `/mikroblog/`, `/opowiadania/`, `/opowiadania/dobry-ojciec/`, `/opowiadania/kartka/`, `/styles.css`, `/js/privacy.js`, and `/favicon.svg`. Confirm unpublished German routes and the retired `/v2/` preview return 404; check `/poker/`, `/biblioteka/` and `/malowanie-po-numerach/` remain reachable and all download hashes match. On `/mikroblog/` verify protected entries expose only the blurred lockbox (no plaintext markers) and that unlocking with the password shows the entry locally. Never log the password, plaintext, or browser input parameters.

The helper refuses DNS, target, nginx docroot, manifest, or worktree drift. It stages on the docroot filesystem, retains the previous complete tree below `/var/www/sochiera-releases` as `backup-<release-id>`, switches with renames under a lock, verifies, and automatically restores that backup if verification fails.

To restore a retained exact release, preview the identifier from prior operator output and run:

```bash
scripts/deploy.sh --host ubuntu@51.83.199.206 --rollback 20260907T120000Z-012345abcdef
```

Rollback accepts only the constrained release-ID form, preserves the replaced tree, and repeats the verification matrix. The connection target and private-key filename are intentionally fixed in the helper; never put key contents, credentials, passwords, or other secrets in this repository.

## Isolated preview (`/v2/`)

A redesign can be reviewed live without touching production. Build the whole site below a prefix (every page gets `noindex,nofollow` and a preview banner; links to `/poker/`, `/malowanie-po-numerach/` and `/pobierz/` stay at the root):

```bash
.venv/bin/python3 scripts/build_site.py --writing-root /home/jan/Sources/writing --output .build/preview-v2 --base-path /v2 \
  --protected-file content/protected.toml --password-file /home/jan/.config/sochiera/blog-password
scripts/deploy-preview.sh   # add --yes for operator automation
```

The helper replaces only `/var/www/sochiera/v2` (the previous preview is kept as `/var/www/sochiera-releases/preview-backup-<id>`), checks the preview routes and confirms the production homepage bytes did not change. nginx already serves `/v2/` through the static `location /`. The next production release (`scripts/deploy.sh`) swaps the whole docroot and therefore removes the preview; to remove it earlier, move `/var/www/sochiera/v2` into the release directory.

## Grupa tematy — osobna aplikacja (po review i rehearsal)

Ta procedura jest oddzielna od etapu implementacji. Nie wdrażaj aplikacji ani strony produkcyjnej, dopóki niezależny przegląd kodu i pełny rehearsal na osobnym VPS/stagingu nie zakończą się PASS. Nie używaj `/v2/` jako środowiska backendu. Staging ma własny katalog danych i osobne, syntetyczne poświadczenie.

Przed stagingiem i ponownie przed produkcją zinwentaryzuj `systemctl`, `ss -ltnp` oraz konfigurację HTTPS w `/etc/nginx/sites-enabled/pbn`. Sprawdź właścicieli portów 8790–8794 i 8501–8503; port 8793 musi być wolny. Potwierdź `root /var/www/sochiera` i punkt include po `mealspire-api.conf`, przed `location /`. Jeśli stan nie zgadza się z rekonesansem, przerwij i uzgodnij zmianę punktu integracji.

Wydanie backendu umieść jako osobne, niezmienne źródło pod `/opt/sochiera/grupa-tematy/releases/<release-id>`; `current` ma wskazywać aktywne wydanie. Przy pierwszej instalacji sprawdź `getent passwd grupa-tematy`; jeśli konto nie istnieje, utwórz je jako systemowe bez katalogu domowego: `sudo useradd --system --no-create-home --user-group --home-dir /nonexistent --shell /usr/sbin/nologin grupa-tematy`. Dane pozostają pod `/var/lib/sochiera/grupa-tematy`, z prawami 0700 i właścicielem usługi; release i publiczny docroot nie mogą zawierać bazy, klucza ani zaszyfrowanego poświadczenia. Przy pierwszej instalacji uruchom interaktywnie (bez przekazywania hasła w argumentach lub środowisku):

```bash
sudo install -d -o root -g root -m 0700 /etc/credstore.encrypted
sudo systemd-creds setup
sudo .venv/bin/python3 scripts/grupa_tematy_admin.py init
```

Narzędzie pyta o hasło bez echa, zapisuje wyłącznie weryfikator scrypt i losowy klucz danych w poświadczeniu zaszyfrowanym `systemd-creds --with-key=host`. `LoadCredentialEncrypted` w unicie jest wymagane; jeśli host nie potrafi odszyfrować poświadczenia, zatrzymaj instalację i napraw obsługę systemd. Nie zastępuj tego ustawieniem `LoadCredential`, plikiem env ani hasłem aplikacji jako kluczem bazy. Na upgrade zachowaj aktywne poświadczenie i dane.

Zainstaluj `deploy/grupa-tematy.service` do `/etc/systemd/system/`, sprawdź jego ścieżki wydania, wykonaj `systemctl daemon-reload`, a na stagingu uruchom usługę. Zweryfikuj listener `127.0.0.1:8793`, `systemctl is-active grupa-tematy` oraz lokalny status `GET /api/grupa-tematy/csrf` bez zapisywania body. API wyłącza access log, przyjmuje treść JSON do 4 KiB i zapisuje prywatne pola wyłącznie jako AES-256-GCM z nowym nonce i AAD związanym z ID rekordu.

Kopia przed upgrade zawiera oba wymagane składniki poza webrootem:

```bash
sudo .venv/bin/python3 scripts/grupa_tematy_admin.py backup \
  --output /var/backups/sochiera/grupa-tematy/<timestamp>
```

Zachowaj katalog kopii z prawami 0700; sprawdź na stagingu odtworzenie SQLite oraz odpowiadającego mu zaszyfrowanego poświadczenia. Nie publikuj kopii ani credentialu w repozytorium, release statycznym lub logach.

Przy odtwarzaniu zatrzymaj usługę. Credential przywracaj wyłącznie na hoście, którego klucz systemd może go odszyfrować; dla innego hosta najpierw wykonaj kontrolowaną re-encryption. Zainstaluj pliki z odpowiednimi właścicielami i prawami, a następnie uruchom usługę:

```bash
sudo install -d -o grupa-tematy -g grupa-tematy -m 0700 /var/lib/sochiera/grupa-tematy
sudo install -o grupa-tematy -g grupa-tematy -m 0600 <backup>/data.sqlite3 /var/lib/sochiera/grupa-tematy/data.sqlite3
sudo install -o root -g root -m 0600 <backup>/credential.encrypted /etc/credstore.encrypted/grupa-tematy
```

Konfigurację `deploy/grupa-tematy.nginx.conf` instaluj jako `/etc/nginx/snippets/grupa-tematy.conf`. Dodaj jej include wyłącznie w potwierdzonym HTTPS vhoście, po `mealspire-api.conf` i przed catch-all. Nie zmieniaj `location /`, innych vhostów, usług ani guardów release. Snippet ustawia limit 4 KiB, proxy do loopbacku i nadpisuje `X-Real-IP`. Przed reloadem wykonaj rzeczywiste `sudo nginx -t`; reload jest dozwolony dopiero po sukcesie.

Rehearsal musi przejść formularz publiczny, błędne i brakujące hasło, brak listy bez sesji, przypisanie po ponownym pobraniu listy, restart API, odtworzenie backupu, brak plaintextu w bazie/WAL/kopii/logach, zachowanie istniejących tras i rollback strony/usługi/snippetu z zachowaniem danych. Dopiero po obu bramkach zatwierdź manifest i wydanie, ponownie zinwentaryzuj listenerów i uruchom standardowy builder oraz `scripts/deploy.sh`. Rollback backendu przywraca poprzedni symlink i konfigurację; nie usuwa trwałego katalogu danych. Produkcyjnego hasła nie zapisuj w historii powłoki, Git, ticketach ani wynikach testów.
