# Sync Spotify ↔ YouTube Music

Sincronizza in modo bidirezionale una playlist collaborativa Spotify con una playlist YouTube Music.
Gira in locale, una volta al giorno, e ti scrive su Telegram solo se serve un tuo intervento.

**Come accede ai servizi**
- **Spotify:** Web API ufficiale, con il tuo login (Authorization Code con refresh automatico).
- **YouTube Music, playlist:** API ufficiale YouTube Data v3, con il tuo login OAuth e refresh automatico.
  Le playlist di YouTube Music sono playlist YouTube a tutti gli effetti. Da agosto 2025 YouTube Music
  rifiuta le richieste OAuth fatte con `ytmusicapi`
  ([sigma67/ytmusicapi#813](https://github.com/sigma67/ytmusicapi/issues/813)).
- **YouTube Music, ricerca brani:** `ytmusicapi` senza login. Dà gli stessi risultati dell'app, con il
  numero di ascolti.

## Come si comporta

| Situazione | Azione |
|---|---|
| Brano solo su Spotify | Lo cerca e lo aggiunge su YouTube Music |
| Brano solo su YouTube Music, mai sincronizzato | Lo cerca e lo aggiunge su Spotify |
| Brano tolto da Spotify (era sincronizzato) | Lo toglie da YouTube Music, ma solo se risulta tolto per **2 esecuzioni consecutive** |
| Brano sincronizzato sparito da YouTube Music | Lo ri-aggiunge su YouTube Music (su Spotify lo script non rimuove mai nulla) |

**Riconoscimento dei brani.** Prima confronta titolo e artista principale normalizzati: minuscolo, senza
accenti, senza code tipo Remastered/Deluxe/feat./Prod./Official Video/Lyrics. Sul lato YouTube Music usa i
campi separati quando ci sono (canali "Artista - Topic") e prova anche le letture "Artista - Titolo" del
titolo grezzo. Solo i casi dubbi passano a Claude Haiku 4.5 con ricerca web.
I verdetti vengono salvati in cache, quindi la stessa domanda non viene mai pagata due volte.
Tra candidati a parità sceglie quello con più ascolti su YouTube Music, e l'uscita originale su Spotify
(Spotify non espone più la popolarità). Se non è sicuro, non aggiunge nulla e riprova al giro dopo.

**Cache** (`data/state.json`). Serve solo come ottimizzazione e per capire le rimozioni. Se la cancelli
non si perde nulla: alla prima esecuzione successiva non vengono fatte rimozioni e la cache si ricostruisce.

**Blocchi di sicurezza.** Lo script non scrive nulla e ti avvisa se:
- una playlist risulta vuota;
- dovrebbe aggiungere più di 15 brani (o più del 30%) in un colpo solo;
- dovrebbe togliere più di 10 brani (o più del 20%) in un colpo solo.

Le soglie si cambiano in `config.toml`.

**Quota YouTube.** La quota gratuita è di 10.000 unità al giorno e ogni aggiunta o rimozione ne costa 50,
quindi circa 200 operazioni al giorno. Un'esecuzione senza novità ne usa circa 5. Se la quota finisce,
lo script riprende alla prossima esecuzione.

**Notifiche Telegram**, solo in questi casi:
- login da rifare;
- configurazione errata;
- blocco di sicurezza;
- giudice IA non utilizzabile;
- 3 esecuzioni fallite di fila.

Lo stesso avviso viene ripetuto al massimo ogni 3 giorni.

## Configurazione iniziale (una volta sola)

### 1. App Spotify
1. Vai su <https://developer.spotify.com/dashboard> → **Create app**. Serve un account **Premium**.
2. Redirect URI: `http://127.0.0.1:8888/callback`. API: **Web API**.
3. Copia *Client ID* e *Client secret*.
4. L'account con cui farai login deve essere proprietario o collaboratore della playlist.

### 2. Client OAuth Google (per YouTube Music)
1. Su <https://console.cloud.google.com> crea un progetto.
2. Abilita **YouTube Data API v3**.
3. Configura la **schermata di consenso OAuth** come *External*, poi **pubblica l'app ("In production")**.
   ⚠️ Se resta in "Testing", Google fa scadere il login dopo 7 giorni. Non serve la verifica di Google:
   al login vedrai solo l'avviso "app non verificata", da accettare.
4. Vai su **Credenziali → Crea credenziali → ID client OAuth**, tipo **"TV e dispositivi con input limitato"**.
   Copia ID e secret.

### 3. Chiave Anthropic
Su <https://console.anthropic.com> crea una API key e aggiungi un po' di credito.
Il costo tipico è di pochi centesimi per ogni brano dubbio.

### 4. Bot Telegram
Scrivi a **@BotFather** → `/newbot` e copia il token. Il chat ID lo trova lo script al passo 6.

### 5. File di configurazione
```bash
cd ~/spotify-ytm-sync
cp .env.example .env && chmod 600 .env        # compila i valori
cp config.example.toml config.toml            # metti il link della playlist Spotify
```

### 6. Login
```bash
.venv/bin/python setup_auth.py telegram   # trova il chat ID: mettilo in .env e rilancia per il messaggio di prova
.venv/bin/python setup_auth.py spotify    # login nel browser
.venv/bin/python setup_auth.py ytm        # apre google.com/device: inserisci il codice e accetta
```

### 7. Prova e prima esecuzione
```bash
.venv/bin/python sync.py --dry-run -v   # mostra cosa farebbe, senza scrivere nulla
.venv/bin/python sync.py                # crea la playlist YouTube Music e la riempie
```
La playlist YouTube Music viene creata con lo stesso nome della playlist Spotify.
La copertina non si può impostare via API, quindi quella di Spotify viene salvata in `data/cover.jpg`.
Se vuoi, caricala una volta a mano da YouTube Music (modifica playlist → copertina).

### 8. Esecuzione automatica giornaliera
```bash
deploy/install_timer.sh
```
Installa un timer systemd utente con `Persistent=true`: se il PC era spento all'ora prevista,
l'esecuzione parte alla prima accensione utile.

## Comandi utili
```bash
systemctl --user list-timers spotify-ytm-sync.timer      # prossima esecuzione
systemctl --user start spotify-ytm-sync.service          # esegui subito
journalctl --user -u spotify-ytm-sync -n 50              # output delle ultime esecuzioni
tail -f logs/sync.log                                    # log dettagliato
.venv/bin/python -m pytest -q                            # test
```

## Pagine per Google OAuth
`docs/` contiene la home page e la privacy policy richieste dalla schermata di consenso OAuth, pubblicate
con GitHub Pages su <https://dongio99.github.io/sync-spotify-ytmusic/>.

## File
- `.env`, `secrets/`: credenziali e token (permessi 600, da non condividere)
- `config.toml`: playlist, modello IA, soglie
- `data/ytm_playlist_id.txt`: ID della playlist YouTube Music creata
- `data/state.json`: cache
- `logs/sync.log`: log con rotazione
