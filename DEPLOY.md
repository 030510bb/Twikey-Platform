# Deployen naar Render (via GitHub)

Deze gids gaat ervan uit dat je geen ervaring hebt met cloud-deployment, maar
wel al met GitHub werkt (zoals bij Claimate). We gebruiken **Render**
(render.com): je koppelt je GitHub-repo, Render leest het meegeleverde
`render.yaml`-bestand en zet automatisch twee services voor je klaar:

- `twikey-platform-backend` — de FastAPI-server (Gmail-koppeling). Draait op
  Render's "Starter"-plan, ongeveer €7/maand, altijd online.
- `twikey-platform-frontend` — het HTML-dashboard, als gratis statische
  website.

## Stap 0 — Vereisten

- Rond eerst de domeinverificatie en het service-account/domeinbrede
  delegatie-gedeelte uit `README.md` af (stappen 1 en 2 daar). Zonder een
  werkend service-account heeft deployen weinig zin — je krijgt dan een
  werkende website die alleen foutmeldingen teruggeeft.
- Rond ook "Database (Supabase)" in `README.md` af — je hebt de
  connection-string van je Supabase-project nodig bij stap 2 hieronder. De
  backend start niet op zonder een geldige `DATABASE_URL`.
- Een GitHub-account (heb je al) en een Render-account (gratis aan te maken
  op [render.com](https://render.com), bijv. met "Sign up with GitHub").

## Stap 1 — Code naar GitHub pushen

Als je dit project nog niet in een GitHub-repo hebt staan:

```bash
cd twikey-platform
git init
git add .
git commit -m "Initial commit: Twikey platform met echte Gmail-backend"
```

Maak daarna op [github.com/new](https://github.com/new) een nieuwe (private)
repository aan, bijvoorbeeld `twikey-platform`, en volg de instructies die
GitHub toont om je lokale project ernaartoe te pushen (iets als):

```bash
git remote add origin https://github.com/<jouw-gebruikersnaam>/twikey-platform.git
git branch -M main
git push -u origin main
```

Het meegeleverde `.gitignore`-bestand zorgt ervoor dat je `.env` en
`service-account.json` (als je die lokaal in de map hebt staan) nooit worden
meegepusht — die horen nooit in git te staan.

## Stap 2 — Render koppelen aan je GitHub-repo

1. Log in op [dashboard.render.com](https://dashboard.render.com).
2. Klik rechtsboven op **New +** → **Blueprint**.
3. Koppel je GitHub-account als dat nog niet is gebeurd, en selecteer de
   `twikey-platform`-repo.
4. Render herkent automatisch het `render.yaml`-bestand en toont een
   overzicht van de twee services die het gaat aanmaken.
5. Bij het veld **DATABASE_URL** vul je de Postgres-connection-string van je
   Supabase-project in (zie "Database (Supabase)" in `README.md` voor waar
   je die vindt). Zonder dit veld start de backend niet op.
6. Bij het veld **GOOGLE_SERVICE_ACCOUNT_JSON** moet je zelf een waarde
   invullen (Render vraagt hier expliciet om, want dit is een geheime
   waarde die niet in `render.yaml` staat): open het JSON-sleutelbestand dat
   je bij Google Cloud hebt gedownload in een teksteditor, kopieer de
   **volledige inhoud** (van `{` tot en met de laatste `}`), en plak dat
   in dit veld.
7. Bij het veld **ADMIN_SECRET** vul je zelf een lange, willekeurige waarde
   in (bijv. gegenereerd met `openssl rand -hex 32`). Bewaar deze waarde
   ergens veilig (wachtwoordmanager) — je hebt hem nodig om je eerste
   klantaccount aan te maken (stap 4 hieronder).
8. Render zet automatisch ook een **ENCRYPTION_KEY** neer (met een
   willekeurig gegenereerde waarde) — je hoeft daar zelf niets voor in te
   vullen. Die versleutelt de SMTP-wachtwoorden die klanten later eventueel
   invullen op het tabblad "Mail-instellingen" (zie README.md). Verander deze
   waarde later niet meer als er al klanten hun eigen mailaccount hebben
   ingesteld — dan kan hun opgeslagen wachtwoord niet meer ontsleuteld
   worden en moeten ze het opnieuw invullen.
9. Optioneel — bespaart je de curl uit stap 4 bij de allereerste keer: zet
   ook **SEED_ACCOUNT_EMAIL** en **SEED_ACCOUNT_PASSWORD** (bijv.
   `sales@twikeycampaigns.nl` en hetzelfde wachtwoord dat je in stap 4
   gebruikt). Dat account wordt dan bij de eerste opstart tegen een lege
   database automatisch aangemaakt. Prima om permanent ingesteld te laten
   staan — een wachtwoord dat je later zelf wijzigt, blijft gewoon staan.
10. Optioneel — nodig voor AI-conceptantwoorden op inkomende replies (tabblad
    Replies, zie README.md): zet **ANTHROPIC_API_KEY** op een geldige
    Anthropic API-key. Dit is één gedeelde, platform-brede key (jij betaalt,
    niet de klant) — zonder deze key werkt alles verder gewoon, maar vallen
    conceptantwoorden terug op de standaard bezwaar-suggestie in plaats van
    een AI-gegenereerde tekst.
11. Klik op **Apply** / **Create**. Render bouwt en start nu beide services —
   dit duurt een paar minuten bij de eerste keer.
12. Controleer (of stel achteraf in bij de backend-service → **Environment**)
    dat `BACKEND_PUBLIC_URL` en `FRONTEND_PUBLIC_URL` overeenkomen met de
    werkelijke URLs die Render aan je services heeft gegeven (zichtbaar bovenin
    elke service-pagina). Render voegt soms een suffix toe als de standaardnaam
    al bezet is — als dat zo is, moeten deze twee variabelen worden aangepast,
    anders wijzen de open/click-tracking-links in campagnemails en de knop
    "Quick Start" naar de verkeerde URL.

**Data-persistentie:** contacten, campagnes, accounts/wachtwoorden en de
LinkedIn-log worden allemaal opgeslagen in je Supabase Postgres-database
(`DATABASE_URL` hierboven) — niet meer op de eigen schijf van de
backend-service. Dat betekent dat niets verdwijnt bij een herstart,
slaapstand, of nieuwe `git push`/deploy, in tegenstelling tot de oude
lokale-SQLite-opzet.

## Stap 3 — URLs opzoeken en testen

Zodra beide services de status "Live" hebben:

1. Open de backend-service in het Render-dashboard en kopieer de URL
   bovenaan (iets als `https://twikey-platform-backend.onrender.com`).
2. Test die in de browser of met curl:
   ```bash
   curl https://twikey-platform-backend.onrender.com/api/health
   # {"status":"ok","send_as":"sales@twikeycampaigns.nl"}
   ```
3. Open de frontend-service-URL (iets als
   `https://twikey-platform-frontend.onrender.com`) in je browser — dit is nu
   je publieke homepage, deelbaar met (potentiele) klanten. Log nog niet in;
   dat kan pas na stap 4 hieronder (nog geen account aangemaakt).
4. Als de backend-URL niet exact overeenkomt met de standaardnaam in
   `render.yaml` (`twikey-platform-backend`) — Render voegt soms een suffix
   toe als de naam al bezet is — pas dan de regel `PRODUCTION_API_BASE`
   bovenin het `<script>`-blok aan in **zowel** `frontend/login.html` **als**
   `frontend/dashboard.html` naar de echte backend-URL, commit en push die
   wijziging — Render deployt dan automatisch opnieuw.

## Stap 4 — Eerste klantaccount aanmaken (twikeycampaigns.nl)

Er is bewust geen publieke "Registreren"-pagina. Je maakt het eerste account
(en elk volgend account) aan via een curl-commando met de `ADMIN_SECRET` die
je in stap 2.6 hebt ingesteld:

```bash
curl -X POST https://twikey-platform-backend.onrender.com/api/admin/accounts \
  -H "X-Admin-Secret: <de ADMIN_SECRET-waarde die je in Render hebt ingesteld>" \
  -H "Content-Type: application/json" \
  -d '{"company_name":"Twikey Campaigns","login_email":"sales@twikeycampaigns.nl","password":"<kies een echt, sterk wachtwoord>"}'
```

Een geslaagd antwoord ziet er zo uit: `{"success":true,"account":{"id":1,...}}`.
Ga daarna naar de frontend-URL, klik op **Inloggen**, en log in met het
e-mailadres en wachtwoord hierboven.

Wil je dat een collega ook kan inloggen en meewerkt aan dezelfde contacten/
campagnes? Dat hoeft niet via deze curl — eenmaal ingelogd kun je in het
dashboard op het tabblad **Team** zelf een teamlid uitnodigen (ze krijgen een
mail om hun eigen wachtwoord in te stellen). Zie de sectie "Teamleden
toevoegen" in `README.md`. Dit is iets anders dan de curl hierboven: die
maakt een heel nieuw, apart account/tenant aan (bijv. voor een andere klant),
terwijl de Team-tab een extra login toevoegt aan het account waar je al
op bent ingelogd.

Dit account blijft nu gewoon bestaan, ook na een herstart of nieuwe deploy —
je hoeft deze curl dus maar één keer te draaien (tenzij je `SEED_ACCOUNT_*`
had ingesteld, dan gebeurde dat zelfs automatisch). Bewaar het commando
sowieso ergens waar je het makkelijk terugvindt, voor als je ooit een tweede
klantaccount aanmaakt.

Wil je zelf (of samen met collega's) een overzicht van alle klantaccounts,
inclusief nieuwe accounts aanmaken en wachtwoorden resetten zonder curl?
Maak dan een beheerderslogin aan met dezelfde `ADMIN_SECRET` en log in via
`https://twikey-platform-frontend.onrender.com/admin-login.html` — zie
"Support / beheerpagina (superadmin)" in `README.md`.

Wil een klant (of jullie zelf) campagnes en losse mails vanaf hun eigen
domein versturen in plaats van het gedeelde Twikey-adres? Dat regelt de
klant zelf, na inloggen, via het tabblad **Mail-instellingen** in het
dashboard — daar hoef jij in Render niets voor aan te passen. Zie "Eigen
mailaccount / domein instellen (per account SMTP)" in `README.md`.

## Cron: opvolgsequenties laten versturen

Opvolgsequenties (tabblad Sequenties, zie README.md) plannen zelf wanneer de
volgende mail van een ingeschreven contact verstuurd moet worden, maar er
draait niets vanzelf op de achtergrond om dat moment ook echt af te vuren —
dat moet periodiek (bijv. elk uur, of elke 15 minuten voor kortere
wachttijden tussen stappen) van buitenaf getriggerd worden via:

```bash
curl -X POST https://api.justmeet.tech/api/cron/process-sequences \
  -H "X-Admin-Secret: <dezelfde ADMIN_SECRET als hierboven>"
```

Dit endpoint verwerkt alle accounts in één keer (het is geen per-klant
aanroep) en is bewust idempotent/onschadelijk als je het vaker aanroept dan
nodig — contacten waarvan de volgende stap nog niet due is, worden gewoon
overgeslagen.

De makkelijkste manier om dit op Render in te richden is een aparte **Cron
Job**-service:

1. In het Render-dashboard: **New +** → **Cron Job**.
2. Kies **Command** als runtime (geen eigen repo/Dockerfile nodig) en vul als
   command in:
   ```bash
   curl -fsS -X POST https://api.justmeet.tech/api/cron/process-sequences -H "X-Admin-Secret: $ADMIN_SECRET"
   ```
3. Zet **Schedule** op bijvoorbeeld `*/15 * * * *` (elke 15 minuten) of
   `0 * * * *` (elk uur) — kies een interval dat past bij de kortste
   wachttijd die je tussen sequence-stappen gebruikt.
4. Voeg bij **Environment** dezelfde `ADMIN_SECRET`-waarde toe als bij de
   backend-service (kopieer 'm handmatig over — Render deelt secrets niet
   automatisch tussen services).
5. Sla op. Render logt elke run; een `{"processed": N}`-achtig antwoord
   betekent dat de aanroep gelukt is.

Elke externe scheduler die op een cron-achtig interval een HTTPS-POST met een
header kan doen werkt hiervoor (Render Cron Jobs, GitHub Actions met een
`schedule`-trigger, cron-job.org, etc.) — Render Cron Jobs hierboven is puur
de laagdrempeligste optie omdat je dan alles op één plek beheert.

Dit is dezelfde `ADMIN_SECRET` als voor `/api/admin/accounts` — geen aparte
credential nodig.

## Cron: verzendwachtrij (domain warm-up) afwerken

Fase 3c voegde een optionele dagelijkse verzendlimiet per account toe
("domain warm-up", zie `crm-roadmap.md` en het tabblad Instellingen in het
dashboard — standaard uit). Ontvangers die bij het lanceren van een
campagne niet meer binnen de limiet van die dag pasten, blijven gewoon "nog
niet verstuurd" staan; dit endpoint werkt die wachtrij periodiek verder af
zodra er weer ruimte is:

```bash
curl -X POST https://api.justmeet.tech/api/cron/process-campaign-queue \
  -H "X-Admin-Secret: <dezelfde ADMIN_SECRET als hierboven>"
```

Zelfde opzet als de sequenties-cron hierboven: één aanroep verwerkt alle
accounts, en is onschadelijk als je 'm vaker aanroept dan nodig (een account
zonder wachtrij of zonder resterend dagbudget wordt gewoon overgeslagen).
Richt 'm op dezelfde manier in als hierboven beschreven — een tweede Render
Cron Job met dit endpoint als **Command**, bijvoorbeeld elk uur
(`0 * * * *`), met dezelfde `ADMIN_SECRET` bij **Environment**. Dit
endpoint is alleen relevant voor accounts die de verzendlimiet daadwerkelijk
aanzetten — voor de rest is het een no-op.

## Cron: dagelijkse samenvatting-mail (digest)

Ook uit Fase 3c: elk teamlid krijgt (standaard aan, uitzetbaar in
Instellingen) 's ochtends een mail met de activiteiten/resultaten van de
afgelopen dag. Dat vereist één dagelijkse trigger:

```bash
curl -X POST https://api.justmeet.tech/api/cron/process-digests \
  -H "X-Admin-Secret: <dezelfde ADMIN_SECRET als hierboven>"
```

Idempotent per (UTC-)dag — een account dat vandaag al een digest kreeg
wordt bij een herhaalde aanroep overgeslagen, dus vaker draaien dan nodig
is onschadelijk. Richt in als een derde Render Cron Job, **Schedule**
bijvoorbeeld `0 6 * * *` (06:00 UTC, dus 08:00 zomertijd/07:00 wintertijd
in Nederland) — of een ander tijdstip naar smaak, zolang het dagelijks
draait — met dezelfde `ADMIN_SECRET` bij **Environment**.

## Stap 5 — Eigen domein koppelen (justmeet.tech)

Dit platform draait op zichzelf prima op de Render-URLs
(`*.onrender.com`), maar je kunt het ook koppelen aan je eigen domein, bijv.
`justmeet.tech` — met per klant een eigen, herkenbare subdomein-URL zoals
`twikeycampaigns.justmeet.tech`, naast een vast `app.justmeet.tech` voor het
dashboard en `api.justmeet.tech` voor de backend.

**Bewust géén wildcard-domein (`*.justmeet.tech`).** Render ondersteunt dat
technisch wel, maar vereist dan dat het kale hoofddomein (`justmeet.tech`
zonder subdomein) ook naar Render wijst. Omdat daar nu je bestaande
commerciële website draait, zou dat die website kunnen verstoren. In plaats
daarvan voeg je per subdomein een los, veilig CNAME-record toe — dat raakt
het hoofddomein totaal niet aan, dus je bestaande website blijft precies
zoals hij nu is. Het kost een paar minuten extra per nieuwe klant, en zodra
je meer dan 2 custom domains gebruikt ook $0,25/maand per extra domein (zie
Render's pricing), maar is zonder risico voor je bestaande site.

**De front-end code in deze levering gaat er al van uit dat je dit doet**:
elke pagina praat met de backend via `https://api.justmeet.tech`, en
`render.yaml` zet `BACKEND_PUBLIC_URL`/`FRONTEND_PUBLIC_URL` op
`api.justmeet.tech`/`app.justmeet.tech`. Gebruik je (nog) geen eigen domein, pas
dan eerst deze waarden weer aan naar je `*.onrender.com`-URLs, anders wijst
alles naar een domein dat nog niet bestaat.

**Stappen:**

1. **In Render, backend-service → Settings → Custom Domains**: voeg
   `api.justmeet.tech` toe. Render laat je daarna de exacte CNAME-waarde zien
   die je moet instellen (meestal de eigen `onrender.com`-naam van de
   service, bijv. `twikey-platform-backend.onrender.com`) — gebruik precies
   die waarde, niet een aanname.
2. **In Render, frontend-service → Settings → Custom Domains**: voeg
   `app.justmeet.tech` toe, én — voor elke klant die een eigen subdomein
   krijgt — bijvoorbeeld `twikeycampaigns.justmeet.tech`. Dit is dezelfde
   statische site voor iedereen; het subdomein is puur cosmetisch, dus je
   hoeft dit niet per klant opnieuw te bouwen of te deployen. Render laat
   ook hier de exacte CNAME-waarde zien.
3. **Bij je DNS-provider voor `justmeet.tech`** (waarschijnlijk je
   domeinregistrar, of Cloudflare als je dat ervoor hebt gezet): voeg voor
   elk van de bovenstaande domeinen een CNAME-record toe met exact de
   waarde die Render toonde, bijvoorbeeld:

   | Naam | Type | Waarde |
   |---|---|---|
   | `api` | CNAME | (waarde die Render toont voor de backend-service) |
   | `app` | CNAME | (waarde die Render toont voor de frontend-service) |
   | `twikeycampaigns` | CNAME | (dezelfde waarde als `app`) |

4. Wacht tot Render het domein als geverifieerd markeert (meestal enkele
   minuten tot een uur, afhankelijk van DNS-propagatie) — Render regelt het
   SSL-certificaat vanaf dat moment zelf, automatisch.
5. **Nieuwe klant erbij?** Herhaal alleen stap 2 (nieuw custom domain op de
   frontend-service) en stap 3 (één nieuw CNAME-record) met hun gekozen
   subdomeinnaam. Geen codewijziging, geen nieuwe deploy nodig.

Zodra `api.justmeet.tech` en `app.justmeet.tech` bevestigd werken, kun je de
oude `*.onrender.com`-URLs gewoon laten voortbestaan als fallback (Render
verwijdert ze niet) of negeren — beide blijven naar dezelfde services wijzen.

## Stap 6 — CORS aanscherpen (aanbevolen, niet verplicht)

Standaard staat `CORS_ORIGINS=*` in `render.yaml`, zodat alles meteen werkt.
Voor iets meer veiligheid kun je dit later aanscherpen:

- **Eén vaste frontend-URL** (geen eigen domein, of geen per-klant
  subdomeinen): zet `CORS_ORIGINS` op die exacte URL, bijvoorbeeld
  `https://twikey-platform-frontend.onrender.com` of `https://app.justmeet.tech`.
- **Eigen domein mét per-klant subdomeinen** (zoals hierboven): gebruik in
  plaats daarvan `CORS_ORIGIN_REGEX`, die al klaarstaat in `render.yaml` op
  `https://([a-zA-Z0-9-]+\.)?justmeet\.tech` — dat dekt `justmeet.tech` zelf,
  `app.justmeet.tech`, `api.justmeet.tech` én elk toekomstig klantsubdomein in
  één keer, zonder dat je 'm per nieuwe klant hoeft bij te werken. Zet in dat
  geval `CORS_ORIGINS` op leeg (of verwijder de env var) zodat alleen de
  regex nog geldt.

Stappen om aan te scherpen:

1. Ga in het Render-dashboard naar de backend-service → **Environment**.
2. Pas `CORS_ORIGINS` en/of `CORS_ORIGIN_REGEX` aan zoals hierboven.
3. Sla op — Render herstart de service automatisch met de nieuwe instelling.

## Updates uitrollen

Voor elke volgende wijziging: pas de code lokaal aan, commit, en `git push`.
Render bouwt en deployt automatisch opnieuw bij elke push naar de
`main`-branch — je hoeft verder niets te doen.

## Kosten

- Backend (Starter-plan, altijd online): ongeveer €7/maand.
- Frontend (statische site): gratis.
- Er zijn geen extra kosten van Google voor het versturen van mail via de
  Gmail API binnen normale gebruikslimieten van Workspace.

## Problemen oplossen

- **Dashboard laadt wel op `app.justmeet.tech`, maar alle data/inloggen geeft
  een netwerkfout**: check de browserconsole op CORS-foutmeldingen. Meestal
  betekent dit dat `CORS_ORIGINS`/`CORS_ORIGIN_REGEX` op de backend-service
  het nieuwe domein nog niet toestaat (zie Stap 6), of dat
  `PRODUCTION_API_BASE` in de frontend-bestanden nog naar de oude
  `onrender.com`-URL wijst in plaats van `api.justmeet.tech`.
- **Custom domain blijft "Pending"/"Not verified" in Render**: het
  CNAME-record bij je DNS-provider ontbreekt, staat verkeerd, of moet nog
  propageren (kan tot een uur duren). Controleer met
  `dig CNAME app.justmeet.tech` (of een online DNS-checker) of het record
  daadwerkelijk naar de waarde wijst die Render toonde.
- **"No Gmail credentials configured"**: `GOOGLE_SERVICE_ACCOUNT_JSON` staat
  niet of onjuist ingesteld. Check bij de backend-service → Environment of
  de volledige JSON-inhoud (inclusief accolades) daar correct is geplakt.
- **CORS-foutmeldingen in de browserconsole**: `CORS_ORIGINS` op de backend
  komt niet overeen met de URL waar de frontend vanaf draait. Zet het terug
  op `*` om te testen, en verfijn daarna.
- **Backend geeft 500 bij `/api/inbox` of `/api/send`**: controleer dat het
  domein twikeycampaigns.nl geverifieerd is in Workspace, en dat de scopes
  in de domeinbrede delegatie exact overeenkomen met wat in
  `backend/gmail_client.py` staat (`gmail.send` en `gmail.readonly`).
- **Campagne-mails komen aan, maar opens/clicks tellen niet mee**:
  `BACKEND_PUBLIC_URL`/`FRONTEND_PUBLIC_URL` op de backend-service kloppen
  niet met de echte Render-URLs. Zet ze gelijk aan wat je bovenin de service-
  pagina's ziet staan, en launch de campagne opnieuw.
- **Contacten/campagnes/accounts zijn plotseling verdwenen**: zou nu niet
  meer moeten gebeuren, want die data staat in Supabase, niet meer op de
  ephemere schijf van de backend-service. Check eerst of `DATABASE_URL` op
  de backend-service nog naar hetzelfde Supabase-project wijst (bijv. per
  ongeluk aangepast, of gewijzigd database-wachtwoord in Supabase zonder dat
  hier bij te werken) — dat zou een lege of onbereikbare database opleveren.
- **Backend start niet op / crasht meteen met een `DATABASE_URL`-foutmelding
  in de logs**: `DATABASE_URL` staat niet of onjuist ingesteld op de
  backend-service → **Environment**. Kopieer de connection-string opnieuw
  vanuit Supabase (Project Settings → Database → Connection string → URI) en
  vul het echte database-wachtwoord in op de plek van `[YOUR-PASSWORD]`.
- **Inloggen geeft "E-mailadres of wachtwoord onjuist" terwijl je zeker weet
  dat het klopt**: meestal gewoon een typefout, of je hebt het wachtwoord
  intussen via de reset-flow gewijzigd. Als het account recent nooit is
  aangemaakt (nieuwe Supabase-database, of `DATABASE_URL` per ongeluk naar
  een ander/leeg project gewezen), draai dan het curl-commando uit Stap 4
  opnieuw.
- **Testmail versturen op het tabblad "Mail-instellingen" mislukt**: de
  foutmelding daar is direct de echte SMTP-foutmelding van de mailprovider
  van de klant zelf — meestal een verkeerd wachtwoord (bij Gmail/Google
  Workspace en Microsoft 365 moet dit vrijwel altijd een *app-wachtwoord*
  zijn, niet het normale inlogwachtwoord), een verkeerde host/poort, of een
  mailprovider die inloggen vanaf een onbekende server blokkeert. Dit heeft
  niets met `ADMIN_SECRET`, `DATABASE_URL` of `GOOGLE_SERVICE_ACCOUNT_JSON`
  te maken — dat blijven aparte, gedeelde instellingen.
- **Wachtwoord vergeten**: gebruik de "Wachtwoord vergeten?"-link op het
  inlogscherm (`login.html` → `forgot-password.html`) — die mailt een
  eenmalige resetlink naar het opgegeven adres via de gedeelde Gmail-mailbox.
  Komt die mail niet aan (bijv. `GOOGLE_SERVICE_ACCOUNT_JSON` staat niet
  goed, zie hierboven), reset het dan zelf als beheerder:
  ```bash
  curl -X POST https://twikey-platform-backend.onrender.com/api/admin/accounts/reset-password \
    -H "X-Admin-Secret: <jouw ADMIN_SECRET>" \
    -H "Content-Type: application/json" \
    -d '{"login_email":"sales@twikeycampaigns.nl","new_password":"<nieuw wachtwoord>"}'
  ```
  Beide routes loggen daarna oude sessies automatisch uit — log opnieuw in
  met het nieuwe wachtwoord.
- **`/api/admin/accounts` geeft altijd 503 "ADMIN_SECRET is niet ingesteld"**:
  controleer bij de backend-service → Environment of `ADMIN_SECRET`
  daadwerkelijk een waarde heeft (niet leeg). Sla op — Render herstart de
  service automatisch.
- **`/api/admin/accounts` geeft 403**: de waarde in je `X-Admin-Secret`-header
  komt niet exact overeen met de `ADMIN_SECRET` op de backend-service (let op
  spaties/aanhalingstekens die per ongeluk zijn meegekopieerd).
