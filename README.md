# Twikey Sales Platform — backend

Dit geeft alle tabs van het dashboard echte, werkende functionaliteit in
plaats van hardcoded voorbeeldcijfers. De front-end praat met een
FastAPI-backend die:

- écht mail verstuurt/leest via de Gmail API namens `sales@twikeycampaigns.nl`
  (Email Sync-tab);
- contacten, campagnes en trackinggegevens opslaat in een echte database
  (A/B Test- en Analytics-tab);
- berichten tegen een reeks regel-gebaseerde kwaliteitschecks aanhoudt
  (Validation-tab);
- een handmatige log van je eigen LinkedIn-outreach bijhoudt (LinkedIn-tab —
  zie de uitleg hieronder over waarom dit bewust geen automatisering is);
- **elke klant achter een eigen login zet.** Het platform is multi-tenant:
  elk bedrijf ("account") logt in met e-mail + wachtwoord en ziet alleen zijn
  eigen contacten, campagnes en LinkedIn-data. Zie "Inloggen en accounts"
  hieronder.

**Dit bestand (README.md) beschrijft lokaal testen op je eigen computer.**
Wil je het platform online/cloud-hosted draaien (aanbevolen zodra je het
écht gaat gebruiken), volg dan **`DEPLOY.md`** — dat behandelt deployen naar
Render via GitHub, inclusief een kant-en-klare `render.yaml`, `Dockerfile`
en `.gitignore` die al in dit project zitten.

## Hoe het werkt

- `backend/app.py` — de API-server (FastAPI). Zie "Alle endpoints" hieronder
  voor het volledige overzicht.
- `backend/gmail_client.py` — praat met de Gmail API via een Google Cloud
  service-account met domeinbrede delegatie (domain-wide delegation), zodat
  de backend zelfstandig mag versturen/lezen namens sales@twikeycampaigns.nl
  zonder dat iemand handmatig hoeft in te loggen. Kan zowel platte tekst als
  HTML-mail versturen (het laatste is nodig voor de open-tracking pixel en
  click-tracked links in campagnes).
- `backend/database.py` — alle opslag (accounts/gebruikers, contacten,
  campagnes, tracking, LinkedIn-log) in Postgres via Supabase — zie
  "Database (Supabase)" hieronder voor hoe je dat instelt.
- `backend/validation.py` — de regel-gebaseerde berichtvalidatie (spam-
  woorden, lengte, personalisatie-placeholders, een lichte grammatica-
  heuristiek, professionele toon, compliance/PII-patronen, platform-
  tekenlimiet).
- `frontend/index.html` — de publieke, commerciele homepage (geen login
  nodig). Vertelt eerlijk wat het platform doet, met een "Inloggen"-knop.
- `frontend/login.html` — het inlogscherm (e-mail + wachtwoord), praat met
  `POST /api/auth/login` en bewaart het sessietoken in `localStorage`. Bevat
  een "Wachtwoord vergeten?"-link.
- `frontend/forgot-password.html` — vraagt een e-mailadres, roept
  `POST /api/auth/forgot-password` aan (altijd dezelfde generieke reactie).
- `frontend/reset-password.html` — de pagina waar de gemailde resetlink naar
  toe wijst (`?token=...`); stelt via `POST /api/auth/reset-password` een
  nieuw wachtwoord in.
- `frontend/dashboard.html` — het eigenlijke dashboard (alle tabs), met echte
  `fetch()`-aanroepen op alle tabs in plaats van nep-cijfers. Toont alleen
  gegevens van het ingelogde account en stuurt niet-ingelogde bezoekers terug
  naar `login.html`. Bevat ook de Team-tab (teamleden uitnodigen/verwijderen —
  zie "Teamleden toevoegen" hieronder).
- `frontend/lead-magnet.html` — een kleine landingspagina die de campagne-
  links naar toe leiden: toont de aangeboden lead magnet en een kort
  formulier, dat bij versturen een "form fill"-event registreert voor de
  Analytics-tab. Blijft bewust publiek/zonder login, want anonieme
  prospects landen hier via een link in een e-mail.

## LinkedIn: optionele browser-extensie voor sneller loggen

`browser-extension/` bevat een Chrome-extensie die op LinkedIn-profielpagina's
een klein paneel toont: template invullen, naar klembord kopiëren, en na het
zelf versturen op LinkedIn met één klik loggen naar `/api/linkedin/log`. Omdat
die endpoints nu ook achter de login zitten, moet je eerst inloggen in de
extensie zelf: klik het extensie-icoon in Chrome en log in met hetzelfde
e-mailadres/wachtwoord als op het dashboard (dit is een apart sessietoken,
losstaand van je browser-sessie op `dashboard.html`). Zie
`browser-extension/README.md` voor installatie en gebruik, en de uitleg
hieronder voor waarom dit bewust geen automatisering is.

## LinkedIn: bewust geen automatisering

Geautomatiseerde connectieverzoeken of berichten versturen via de LinkedIn-
tab zou LinkedIn's gebruiksvoorwaarden schenden en kan tot een
accountblokkering leiden. Daarom praat deze backend nooit rechtstreeks met
LinkedIn. In plaats daarvan is de LinkedIn-tab een **handmatige tracker**:
jij voert zelf de connectieverzoeken/berichten uit op LinkedIn, en logt die
actie in het dashboard — de tellingen (verstuurd vandaag, acceptatiegraad,
etc.) zijn dus jouw eigen, echte geschiedenis in plaats van voorbeelddata.

## Inloggen en accounts (multi-tenant)

Het platform is multi-tenant: elk bedrijf dat het gebruikt ("account") heeft
zijn eigen login en ziet alleen zijn eigen contacten, campagnes en
LinkedIn-data — nooit die van een ander account. Er is bewust geen publieke
"Registreren"-pagina; nieuwe accounts worden handmatig aangemaakt via een
admin-only endpoint.

**Nieuw account aanmaken** (bijv. de eerste klant, twikeycampaigns.nl zelf):

```bash
curl -X POST http://localhost:8000/api/admin/accounts \
  -H "X-Admin-Secret: <jouw ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"company_name":"Twikey Campaigns","login_email":"sales@twikeycampaigns.nl","password":"<kies een echt wachtwoord>"}'
```

`ADMIN_SECRET` is een environment variable die je zelf instelt (lokaal in
`.env`, op Render via het dashboard — zie `render.yaml`/`DEPLOY.md`). Zonder
een juiste `X-Admin-Secret`-header geeft dit endpoint een 403, dus dit kan
niet per ongeluk door een buitenstaander gebruikt worden.

**Inloggen**: open `frontend/login.html`, log in met het e-mailadres en
wachtwoord van hierboven. Je komt dan op `dashboard.html` met een sessie die
30 dagen geldig blijft (opgeslagen als bearer-token in `localStorage`).

Accounts en wachtwoorden staan (net als contacten en campagnes) in je
Supabase Postgres-database (zie "Database (Supabase)" hierboven) — die
verdwijnen dus niet meer bij een herstart of nieuwe deploy, in tegenstelling
tot de oude lokale-SQLite-opzet.

**Optioneel — automatisch een eerste account aanmaken.** Zet in Render (of
lokaal in `.env`) de variabelen `SEED_ACCOUNT_EMAIL` en
`SEED_ACCOUNT_PASSWORD` — dan wordt dat account bij de eerste opstart tegen
een lege database automatisch aangemaakt, zonder dat je de curl hierboven
handmatig hoeft te draaien. Verander je je wachtwoord later zelf (via
inloggen of de reset-flow), dan blijft dat gewoon staan — de seed maakt het
account alleen aan als het nog niet bestaat, hij zet een bestaand wachtwoord
nooit terug. Prima om permanent ingesteld te laten staan.

**Wachtwoord vergeten?** Er staat een "Wachtwoord vergeten?"-link op
`login.html`. Die stuurt naar `forgot-password.html`, waar je een e-mailadres
invult; de backend mailt (via de gedeelde Gmail-mailbox) een eenmalige,
1 uur geldige resetlink naar `reset-password.html?token=...` als dat adres
bij een account hoort. De reactie is altijd dezelfde generieke tekst,
ongeacht of het adres bestaat — zo kan dit endpoint niet gebruikt worden om
te achterhalen welke e-mailadressen geregistreerd zijn.

Werkt de mail niet (bijv. Gmail-koppeling kapot, of de mailbox van de klant
zelf onbereikbaar)? Dan kun jij als beheerder het wachtwoord alsnog direct
resetten, met hetzelfde soort curl-commando als bij het aanmaken van een
account:

```bash
curl -X POST http://localhost:8000/api/admin/accounts/reset-password \
  -H "X-Admin-Secret: <jouw ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"login_email":"sales@twikeycampaigns.nl","new_password":"<nieuw wachtwoord>"}'
```

Beide routes loggen meteen ook alle bestaande sessies van dat account uit.

## Teamleden toevoegen (meerdere logins, één account)

Eén "account" hierboven is één bedrijf/tenant — maar binnen dat account
kunnen meerdere mensen een eigen login hebben, die allemaal dezelfde
contacten, campagnes en LinkedIn-log zien en gebruiken (er is geen aparte
data per teamlid, alleen per account). Dit is iets anders dan een nieuw
account aanmaken via `/api/admin/accounts` hierboven — dat maakt een nieuwe,
losstaande klant/tenant aan met zijn eigen, gescheiden data; een teamlid
toevoegen voegt alleen een extra inlog toe aan een bestaand account.

Dit gaat, in tegenstelling tot een nieuw account, niet via `ADMIN_SECRET` —
elke al ingelogde gebruiker van een account kan zelf teamleden uitnodigen en
verwijderen, via de nieuwe **Team**-tab in `dashboard.html`, of rechtstreeks:

```bash
curl -X POST http://localhost:8000/api/team/invite \
  -H "Authorization: Bearer <jouw sessietoken>" \
  -H "Content-Type: application/json" \
  -d '{"email":"collega@bedrijf.nl"}'
```

De uitgenodigde persoon krijgt een e-mail (via de gedeelde Gmail-mailbox) met
een eenmalige, 1 uur geldige link om zelf een wachtwoord in te stellen —
dezelfde `reset-password.html`-pagina als bij "wachtwoord vergeten". Niemand,
ook degene die uitnodigt niet, komt ooit een wachtwoord van een ander te
weten. `GET /api/team/users` toont alle teamleden van je eigen account;
`DELETE /api/team/users/{id}` verwijdert er één (jezelf verwijderen via dit
endpoint kan niet, en een account met precies één teamlid overhouden ook
niet — dan zou het account ontoegankelijk worden).

**Wat nog niet per account is afgeschermd**: alle accounts versturen mail op
dit moment via dezelfde gedeelde mailbox (`SEND_AS_EMAIL`,
sales@twikeycampaigns.nl). Contacten/campagnes/LinkedIn-data zijn wel volledig
per account gescheiden, maar een eigen verzendadres per klant zou betekenen
dat elke klant zijn eigen Google Workspace-domein + service-account instelt
(de hele stappen 1–2 hierboven, per klant) — dat is een bewuste vervolgstap,
nog niet gebouwd.

## Alle endpoints

Endpoints hieronder gemarkeerd met 🔒 vereisen een `Authorization: Bearer
<token>`-header (het token dat je van `/api/auth/login` terugkrijgt). Zonder
geldig token geeft elk 🔒-endpoint een 401 terug.

| Endpoint | Beschrijving |
|---|---|
| `GET /api/health` | Check of de backend draait en welke mailbox actief is. Publiek. |
| `POST /api/auth/login` | Inloggen (`email`, `password`) → `{token, account}`. Publiek. |
| `POST /api/auth/forgot-password` | Vraag een resetlink aan (`email`). Altijd dezelfde generieke reactie. Publiek. |
| `POST /api/auth/reset-password` | Wissel een geldig resettoken (`token`, `new_password`) om voor een nieuw wachtwoord. Publiek (het token zelf is de autorisatie). |
| `POST /api/auth/logout` 🔒 | Huidige sessie ongeldig maken. |
| `GET /api/auth/me` 🔒 | Gegevens van het ingelogde account. |
| `POST /api/admin/accounts` | Nieuw account aanmaken. Vereist `X-Admin-Secret`-header (niet hetzelfde als een sessietoken) — zie "Inloggen en accounts" hierboven. |
| `POST /api/admin/accounts/reset-password` | Wachtwoord van een bestaand account resetten (`login_email`, `new_password`). Vereist ook `X-Admin-Secret`. Fallback voor als de self-service "wachtwoord vergeten"-mail niet aankomt (zie "Inloggen en accounts" hierboven). |
| `POST /api/team/invite` 🔒 | Teamlid toevoegen aan je eigen account (`email`). Mailt een eenmalige link om zelf een wachtwoord te kiezen. Zie "Teamleden toevoegen". |
| `GET /api/team/users` 🔒 | Lijst van teamleden op je eigen account. |
| `DELETE /api/team/users/{id}` 🔒 | Een teamlid verwijderen van je eigen account. Niet mogelijk voor jezelf, en niet als het account daarmee 0 gebruikers zou overhouden. |
| `POST /api/send` 🔒 | Verstuur een losse mail (`to`, `subject`, `message`). |
| `GET /api/inbox` 🔒 | Laatste inbox-berichten + ongelezen/vandaag-tellingen. |
| `GET /api/contacts` 🔒 | Lijst van alle contacten (van je eigen account). |
| `POST /api/contacts` 🔒 | Eén contact toevoegen/bijwerken. |
| `POST /api/contacts/bulk` 🔒 | Meerdere contacten in één keer toevoegen. |
| `POST /api/validate-message` 🔒 | Valideer een bericht (`text`, `platform`). |
| `GET /api/campaigns` 🔒 | Lijst van campagnes (van je eigen account). |
| `POST /api/campaigns` 🔒 | Nieuwe A/B-campagne aanmaken (verdeelt contacten round-robin over de varianten). |
| `POST /api/campaigns/{id}/launch` 🔒 | Verstuurt de campagne-mails echt via Gmail, met tracking. |
| `GET /api/campaigns/{id}/results` 🔒 | Verzonden/opens/clicks/form-fills per groep. |
| `GET /track/open/{token}.png` | Open-tracking pixel (wordt automatisch in mails ingesloten). |
| `GET /track/click/{token}` | Click-tracking redirect naar de lead-magnet-pagina. |
| `POST /track/formfill/{token}` | Registreert een formulier-invulling op de lead-magnet-pagina. |
| `GET /api/linkedin/stats` 🔒 | Geaggregeerde LinkedIn-outreach-cijfers. |
| `GET /api/linkedin/templates` 🔒 | De 4 LinkedIn-berichtsjablonen. |
| `PUT /api/linkedin/templates/{id}` 🔒 | Pas een sjabloon aan. |
| `POST /api/linkedin/log` 🔒 | Log een handmatige LinkedIn-actie. |
| `GET /api/linkedin/log` 🔒 | Recente gelogde LinkedIn-activiteit. |

## Vereisten

- Python 3.10 of hoger
- Een Google Workspace-account met beheerdersrechten voor twikeycampaigns.nl
  (nodig om de domeinbrede delegatie te autoriseren)
- Het domein moet geverifieerd zijn in Google Workspace (zonder verificatie
  kun je domeinbrede delegatie niet instellen)
- Een gratis [Supabase](https://supabase.com)-project (de database — zie
  "Database (Supabase)" hieronder)

## Database (Supabase)

De backend slaat alles op in Postgres via Supabase — geen lokaal bestand
meer, dus er verdwijnt ook niets meer bij een herstart of nieuwe deploy.

1. Maak (of gebruik een bestaand) project op
   [supabase.com](https://supabase.com/dashboard) — de gratis laag is
   voldoende.
2. Ga naar **Project Settings → Database → Connection string**, kies **URI**,
   en kopieer die. Vul het wachtwoord van je database in op de plek van
   `[YOUR-PASSWORD]` (dat heb je zelf gekozen bij het aanmaken van het
   project, of kun je op diezelfde pagina resetten).
3. Zet die volledige URI als `DATABASE_URL` in `backend/.env` (lokaal, zie
   Stap 3 hieronder) en/of in Render (zie `DEPLOY.md`).

Je hoeft zelf geen tabellen aan te maken — `backend/database.py` doet dat
automatisch (`CREATE TABLE IF NOT EXISTS ...`) zodra de backend voor het
eerst opstart met een geldige `DATABASE_URL`.

## Stap 1 — Google Cloud: service-account aanmaken

1. Ga naar [console.cloud.google.com](https://console.cloud.google.com) en
   maak een project aan (of gebruik een bestaand project).
2. Ga naar **API's en services → Bibliotheek**, zoek **Gmail API** en klik op
   **Inschakelen**.
3. Ga naar **API's en services → Inloggegevens → Inloggegevens maken →
   Service-account**. Geef het een naam (bijv. `twikey-platform-mailer`) en
   rond het aanmaken af.
4. Open het zojuist aangemaakte service-account, ga naar het tabblad
   **Sleutels → Sleutel toevoegen → JSON**. Er wordt een `.json`-bestand
   gedownload — dit is de sleutel die de backend gebruikt om te
   authenticeren. **Bewaar dit bestand veilig en deel het nooit publiekelijk**
   (bijv. niet in een git-repository committen).
5. Noteer het **Client ID** van het service-account (een lang numeriek getal,
   te vinden op de service-account-detailpagina onder "Unieke ID").

## Stap 2 — Google Workspace Admin: domeinbrede delegatie autoriseren

1. Ga naar [admin.google.com](https://admin.google.com) → **Beveiliging →
   Toegang en gegevensbeheer → API-besturingselementen → Domeinbrede
   delegatie beheren**.
2. Klik op **Nieuwe toevoegen**.
3. Vul bij "Client-ID" het numerieke Client ID van het service-account in
   (uit stap 1.5).
4. Vul bij "OAuth-scopes" precies deze twee scopes in (kommagescheiden):
   ```
   https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/gmail.readonly
   ```
5. Klik op **Autoriseren**.

## Stap 3 — Backend configureren en starten

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# open .env en vul in:
# - DATABASE_URL: de Postgres-connection-string van je Supabase-project
#   (zie "Database (Supabase)" hierboven)
# - GOOGLE_SERVICE_ACCOUNT_FILE: het pad naar het gedownloade
#   JSON-sleutelbestand uit stap 1.4, bijv.:
#   GOOGLE_SERVICE_ACCOUNT_FILE=/pad/naar/service-account.json

uvicorn app:app --reload --port 8000
```

Test daarna in de browser of de terminal:

```bash
curl http://localhost:8000/api/health
# {"status":"ok","send_as":"sales@twikeycampaigns.nl"}

curl http://localhost:8000/api/inbox
# lijst met de laatste inbox-berichten
```

Als `/api/inbox` een foutmelding geeft: controleer of het domein geverifieerd
is, of de scopes in stap 2 exact overeenkomen, en of het pad naar het
JSON-sleutelbestand in `.env` klopt.

## Stap 4 — Front-end openen

Zorg eerst dat er een account bestaat om mee in te loggen (zie "Inloggen en
accounts" hierboven) — zonder account kun je niet verder dan het inlogscherm.

Open daarna `frontend/login.html` in de browser (dubbelklikken volstaat, geen
webserver nodig voor het bestand zelf) en log in. Je komt op
`frontend/dashboard.html` terecht. Ga naar het tabblad **Email Sync** — de
pagina verbindt automatisch met `http://localhost:8000` en toont de echte
inbox en verstuurstatus. `frontend/index.html` is de publieke marketingpagina
(geen login nodig) met een link naar `login.html`.

Als de backend op een ander adres/poort draait, pas dan de regel
`const API_BASE = 'http://localhost:8000';` bovenaan het `<script>`-blok aan
— deze regel staat, met dezelfde naam, in zowel `login.html` als
`dashboard.html`.

## Stap 5 — De andere tabs proberen

- **A/B Test**: voeg eerst een paar contacten toe (los of in bulk-tekstvak),
  vul een campagnenaam in en klik op "Launch A/B Test" — dit verstuurt echte
  mails via Gmail aan je contacten, verdeeld over de 4 aanbiedingen.
- **Analytics**: kies de zojuist gelanceerde campagne in de dropdown om de
  live sent/opens/clicks/form-fills per groep te zien. Klik op mails in je
  eigen postvak om "opens" te laten meetellen, en klik op de link in de mail
  om "clicks" en de lead-magnet-pagina te zien.
- **Validation**: typ of plak een bericht en klik op "Check Message".
- **LinkedIn**: log een outreach-actie die je zelf op LinkedIn hebt gedaan
  (dit stuurt niets naar LinkedIn — zie de uitleg hierboven).

Let op: lokaal draaiend (met `FRONTEND_PUBLIC_URL`/`BACKEND_PUBLIC_URL` op
hun Render-standaardwaarden) werken de tracking-links in verstuurde mails
alleen goed zodra je ze ook op Render hebt ingesteld — zie `DEPLOY.md`.

## Volgende stappen (niet in deze levering)

- Automatisch gegenereerde rapporten op de lead-magnet-pagina (nu toont die
  pagina alleen een formulier; het rapport zelf — DSO-score, cashflow-
  berekening, etc. — wordt nog niet automatisch gegenereerd).
- LinkedIn-automatisering: nog steeds afgeraden, om dezelfde reden als eerder
  (schendt LinkedIn's gebruiksvoorwaarden, risico op accountblokkering). De
  LinkedIn-tab blijft daarom een handmatige tracker.
