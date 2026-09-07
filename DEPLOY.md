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
8. Optioneel — bespaart je de curl uit stap 4 bij de allereerste keer: zet
   ook **SEED_ACCOUNT_EMAIL** en **SEED_ACCOUNT_PASSWORD** (bijv.
   `sales@twikeycampaigns.nl` en hetzelfde wachtwoord dat je in stap 4
   gebruikt). Dat account wordt dan bij de eerste opstart tegen een lege
   database automatisch aangemaakt. Prima om permanent ingesteld te laten
   staan — een wachtwoord dat je later zelf wijzigt, blijft gewoon staan.
9. Klik op **Apply** / **Create**. Render bouwt en start nu beide services —
   dit duurt een paar minuten bij de eerste keer.
10. Controleer (of stel achteraf in bij de backend-service → **Environment**)
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

## Stap 5 — CORS aanscherpen (aanbevolen, niet verplicht)

Standaard staat `CORS_ORIGINS=*` in `render.yaml`, zodat alles meteen werkt.
Voor iets meer veiligheid kun je dit later aanscherpen naar alleen je eigen
frontend-URL:

1. Ga in het Render-dashboard naar de backend-service → **Environment**.
2. Zet `CORS_ORIGINS` op je exacte frontend-URL, bijvoorbeeld:
   `https://twikey-platform-frontend.onrender.com`
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
