# Twikey Sales Platform — backend

Dit geeft alle tabs van het dashboard echte, werkende functionaliteit in
plaats van hardcoded voorbeeldcijfers. De front-end praat met een
FastAPI-backend die:

- écht mail verstuurt/leest via de Gmail API namens `sales@twikeycampaigns.nl`
  (Email Sync-tab) — een account kan dit overschrijven met zijn eigen
  mailaccount/domein via het tabblad Mail-instellingen, zie verderop;
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
- `frontend/admin-login.html` / `frontend/admin.html` — het aparte support-
  inlogscherm en -dashboard voor Twikey-medewerkers (niet voor klanten).
  Zie "Support / beheerpagina" hieronder.
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

**Verzenden**: standaard versturen alle accounts mail via dezelfde gedeelde
mailbox (`SEND_AS_EMAIL`, sales@twikeycampaigns.nl). Een account kan dit per
klant overschrijven door zijn eigen SMTP-gegevens in te vullen op het
tabblad **Mail-instellingen** — zie "Eigen mailaccount / domein instellen"
verderop. Contacten/campagnes/LinkedIn-data zijn hoe dan ook altijd volledig
per account gescheiden.

**Lezen** (de Email Sync-tab, ongelezen-teller): dat blijft altijd de
gedeelde Gmail-mailbox tonen, ook voor accounts met een eigen verzendadres —
hun eigen inbox uitlezen zou IMAP-credentials en aparte toestemming vergen
naast wat er al voor SMTP-verzending nodig is. Hun verzonden mail gaat dus
wél gewoon via hun eigen domein.

## Eigen mailaccount / domein instellen (per account SMTP)

Op het tabblad **Mail-instellingen** kan een account zijn eigen mailaccount
koppelen, zodat campagne-mails en losse verstuurde mails (`POST /api/send`,
`POST /api/campaigns/{id}/launch`) voortaan via hún domein gaan in plaats
van via het gedeelde Twikey-adres. Ontvangers zien dan het eigen adres van
de klant, en antwoorden komen in hún eigen mailbox terecht.

Benodigde gegevens (van de eigen mailprovider van de klant):

- **Host + poort**: bijv. `smtp.gmail.com` / `587` voor Google Workspace,
  `smtp.office365.com` / `587` voor Microsoft 365, of de SMTP-gegevens van
  elke andere provider (cPanel-hosting, Zoho, etc.).
- **Gebruikersnaam + wachtwoord**: bij Gmail/Google Workspace en Microsoft
  365 is dit meestal een *app-wachtwoord* (vereist 2-staps-verificatie op
  dat account), niet het normale inlogwachtwoord.
- **Afzender e-mailadres** (en optioneel een afzendernaam).

Werkwijze in de UI: eerst op **"Testmail versturen"** klikken (stuurt een
echte testmail naar het eigen inlogadres van de gebruiker) om de gegevens te
verifiëren, en pas daarna op **Opslaan**. Instellingen kunnen op elk moment
weer verwijderd worden ("Terugzetten naar gedeeld Twikey-adres"), waarna het
account meteen weer via de gedeelde mailbox verstuurt.

Technisch: het wachtwoord wordt versleuteld opgeslagen (`ENCRYPTION_KEY`,
zie `backend/crypto.py`) en nooit teruggegeven door de API. Zie
`backend/smtp_client.py` voor de daadwerkelijke SMTP-verzendlogica.

Op hetzelfde tabblad (nu **Integraties**) kunnen ook IMAP-gegevens worden
ingevuld voor het uitlezen van replies (versleuteld opgeslagen, zelfde
patroon als SMTP). Het daadwerkelijk uitlezen gebeurt op het tabblad
**Replies** — zie "Reply-tracking, AI-conceptantwoorden en opvolgsequenties"
hieronder.

## Mini-CRM: tags, toewijzing, uitsluiting, audit trail

Het tabblad **Contacten** breidt de simpele contactenlijst uit tot een
mini-CRM:

- **Functie, sector, tags**: vrije velden per contact; tags zijn een gedeeld
  vocabulaire per account (`tags`/`contact_tags`-tabellen) zodat je op tag
  kunt filteren.
- **Toewijzen aan een teamlid**: elk contact kan aan één bestaande teamgenoot
  (accountmanager/SDR, zie "Teamleden toevoegen") worden gekoppeld.
- **Zoeken**: op naam, e-mailadres of bedrijfsnaam.
- **"Niet meer benaderen"**: een expliciete, blijvende stop per contact —
  wordt overal gerespecteerd (nieuwe campagnes slaan dit contact altijd
  over, ongeacht de uitsluitlijst hieronder).
- **Uitsluitlijst (bestaande klanten / lopende offertes)**: drie manieren om
  een bedrijf uit te sluiten van nieuwe campagnes — handmatig per contact
  ("Klant"/"Offerte"-vinkjes), een CSV-upload van te vermijden
  domeinen/bedrijven, of een live HubSpot-koppeling in Integraties (zie
  hieronder). Een campagne slaat uitgesloten contacten automatisch over
  (`POST /api/campaigns` met `include_excluded: true` negeert dit bewust).
- **Herinneringen (agenderen)**: zet een datum + notitie op een contact (bv.
  "klant vroeg over 3 maanden terug te bellen") — zichtbaar als open lijst
  totdat je 'm afhandelt.
- **Audit trail / tijdlijn**: `GET /api/contacts/{id}/timeline` combineert
  CRM-events (aangemaakt, tag/toewijzing/uitsluiting gewijzigd) met alles wat
  al bestond (campagne-mails verzonden/geopend/geklikt/formulier,
  LinkedIn-outreach) tot één tijdlijn per contact, nieuwste eerst.
- **CSV import/export**: `POST /api/contacts/import-csv` herkent NL/EN
  kolomnamen automatisch (voornaam/first_name, functie/job_title, sector,
  tags, etc.); `GET /api/contacts/export-csv` levert alle velden inclusief
  tags/toewijzing/uitsluitingsstatus.
- **Vibe Prospecting / Explorium en HubSpot**: op het tabblad Integraties kan
  per account een Explorium API-key en een HubSpot access token worden
  opgeslagen (versleuteld, zelfde patroon als SMTP). Beide koppelingen zijn
  live: vanuit de Integraties-tab kun je bedrijven zoeken, lookalikes vinden
  en gevonden prospects direct als contact importeren via Explorium
  (`POST /api/prospecting/...`), en een domein handmatig laten controleren
  tegen HubSpot (`POST /api/hubspot/check`). Elk nieuw contact (handmatig,
  bulk, CSV-import of Vibe Prospecting-import) wordt bovendien automatisch
  tegen HubSpot gecontroleerd als er een HubSpot-token is ingesteld en het
  contact een bedrijfsdomein heeft — een bestaande klant of een lopende
  offerte in HubSpot zet dan automatisch `excluded_reason`. Deze
  HubSpot-check is best-effort: als HubSpot niet bereikbaar is of het token
  ongeldig is, gaat het aanmaken van het contact gewoon door (alleen de
  automatische uitsluiting wordt dan overgeslagen). Zie `crm-roadmap.md` voor
  de volledige roadmap en de vereiste HubSpot-scopes.

## Reply-tracking, AI-conceptantwoorden en opvolgsequenties

Het tabblad **Replies** en het tabblad **Sequenties** zijn samen Fase 2 van
`crm-roadmap.md`.

**Replies ophalen**: op het tabblad Replies staat een knop "Nieuwe replies
ophalen" (`POST /api/replies/fetch`), die de eigen IMAP-inbox van het account
uitleest (instellingen op het tabblad Integraties — zie hierboven) vanaf het
laatst geziene bericht. Elk nieuw bericht wordt gematcht aan een contact op
e-mailadres en aan de meest recente campagne van dat contact, en opgeslagen
als `incoming_reply`. Er is geen automatische achtergrond-polling ingebouwd —
de gebruiker klikt zelf op de knop, of dit kan periodiek getriggerd worden
met een externe scheduler (zelfde principe als de opvolgsequenties hieronder,
zie ook `DEPLOY.md`).

**Bezwaren-bibliotheek en AI-conceptantwoorden**: elke inkomende reply wordt
op trefwoorden gecategoriseerd tegen de bezwaren-bibliotheek van het account
(`GET/POST/PUT/DELETE /api/objections` — met standaardcategorieën als
"Geen tijd / geen prioriteit", "Te duur", etc., die bij het aanmaken van een
account automatisch worden meegegeven en per account aan te passen zijn).
Vervolgens wordt via de Anthropic API (zie `ANTHROPIC_API_KEY` hieronder) een
concept-antwoord gegenereerd, met de herkende bezwaarcategorie en
standaardsuggestie als context. Lukt de AI-aanroep niet (geen API-key
ingesteld, of een fout bij Anthropic), dan valt het systeem terug op de
standaardsuggestie van de bezwaarcategorie, of een generieke concepttekst.

**Goedkeuringsscherm**: elk concept-antwoord komt als `reply_draft` met status
`pending` op het tabblad Replies te staan. Een teamlid kan de tekst aanpassen
en goedkeuren (`POST /api/replies/drafts/{id}/approve`) — pas dan wordt de
mail echt verstuurd, via dezelfde verzendweg als de rest (eigen SMTP-adres
als dat is ingesteld, anders het gedeelde Twikey-adres) — of afwijzen
(`POST /api/replies/drafts/{id}/dismiss`).

**"Automatisch versturen"-instelling**: per account is er een schakelaar
(`GET/POST /api/settings/auto-reply`) om conceptantwoorden meteen automatisch
te laten versturen in plaats van eerst goed te keuren. **Deze staat standaard
uit** voor elk account, precies zoals afgesproken in `crm-roadmap.md` — pas
hem pas later aan zodra er vertrouwen is in de kwaliteit van de
AI-conceptantwoorden.

**Opvolgsequenties**: op het tabblad Sequenties kun je een meerstaps
opvolgmail-flow aanmaken (`POST /api/sequences`), met per stap een
onderwerp/body-sjabloon (zelfde `{{firstName}}`-stijl variabelen als de
bestaande campagnes) en een wachttijd in dagen tot de volgende stap. Contacten
worden ingeschreven via `POST /api/sequences/{id}/enroll`; de eerste stap gaat
direct uit, elke volgende stap pas na de ingestelde wachttijd na de vorige
stap. Zodra een ingeschreven contact een reply stuurt, stopt de sequence voor
dat contact automatisch (status `stopped_reply`) — er gaat dan geen opvolgmail
meer uit, en vanuit die reply kan in plaats daarvan direct een herinnering
worden gezet (tabblad Contacten). Sequenties gebruiken dezelfde verzendweg
(eigen SMTP of gedeeld Twikey-adres) als de rest van het platform.

Het daadwerkelijk versturen van due opvolgmails gebeurt niet vanzelf op de
achtergrond — dat vereist een periodieke aanroep van
`POST /api/cron/process-sequences` (beveiligd met `X-Admin-Secret`, net als de
andere admin-endpoints) door een externe scheduler. Zie "Cron: opvolgsequenties
laten versturen" in `DEPLOY.md` voor hoe je dat op Render inricht.

## Support: kennisbank en supportvragen

Het tabblad **Support** heeft een doorzoekbare kennisbank (`kb_articles`,
voorzien van standaardartikelen bij eerste opstart) en een formulier om een
supportvraag in te dienen. Vragen komen terecht in het support-dashboard
(`admin.html`), waar een Twikey-medewerker kan reageren — het antwoord is
voor de klant zichtbaar op hetzelfde tabblad.

## Support / beheerpagina (superadmin)

Naast klantaccounts (`accounts`/`users` hierboven) bestaat er een volledig
gescheiden inlog voor Twikey-medewerkers: `frontend/admin-login.html` →
`frontend/admin.html`. Dit is geen "inloggen als een klant" (geen
impersonatie) — het is een support-overzicht:

- **Alle accounts in één lijst**, met aantal gebruikers/contacten/campagnes
  per account.
- **Nieuw klantaccount aanmaken** vanuit de pagina zelf, in plaats van met
  een curl-commando.
- **Meekijken in één account**: teamleden, campagnes, en een (tot 200
  getoonde) lijst contacten — voor als je een klant helpt debuggen. Bevat
  geen Gmail-postvakinhoud, alleen wat in de database staat.
- **Wachtwoord resetten voor een specifiek teamlid** van een account.

Beheerders (support-medewerkers) zijn een aparte tabel (`admins`) met hun
eigen sessies (`admin_sessions`), losstaand van de klant-`users`/`sessions`.
Een klant-sessietoken werkt dus nooit op een `/api/superadmin/*`-endpoint,
en andersom een beheerderstoken nooit op een klant-endpoint zoals
`/api/auth/me` — dat is met een geautomatiseerde test geverifieerd.

**De eerste beheerder aanmaken** gaat, net als het allereerste klantaccount,
via `ADMIN_SECRET` (er is nog geen andere beheerder om er een uit te
nodigen):

```bash
curl -X POST http://localhost:8000/api/superadmin/admins \
  -H "X-Admin-Secret: <jouw ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"email":"jouw-email@twikey.com","password":"<kies een echt wachtwoord>"}'
```

Log daarna in via `admin-login.html` met dat e-mailadres/wachtwoord. Wil je
een collega ook toegang geven, draai dan dit commando nogmaals met hun
gegevens — er is (bewust, vooralsnog) geen "beheerder uitnodigen"-knop in de
pagina zelf, in tegenstelling tot de Team-tab voor klanten.

## Alle endpoints

Endpoints hieronder gemarkeerd met 🔒 vereisen een `Authorization: Bearer
<token>`-header (het token dat je van `/api/auth/login` terugkrijgt). Zonder
geldig token geeft elk 🔒-endpoint een 401 terug. Endpoints gemarkeerd met
🔒\* vereisen in plaats daarvan een *beheerderstoken* (van
`/api/superadmin/login`) — een gewoon klanttoken werkt daar niet op, en
omgekeerd werkt een beheerderstoken niet op de gewone 🔒-endpoints.

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
| `POST /api/superadmin/admins` | Eerste/extra beheerderslogin aanmaken. Vereist `X-Admin-Secret`. Zie "Support / beheerpagina". |
| `POST /api/superadmin/login` | Inloggen als beheerder (`email`, `password`) → `{token, admin}`. Publiek, los van klant-login. |
| `POST /api/superadmin/logout` 🔒\* | Huidige beheerderssessie ongeldig maken. |
| `GET /api/superadmin/me` 🔒\* | Gegevens van de ingelogde beheerder. |
| `GET /api/superadmin/accounts` 🔒\* | Alle accounts met aantal gebruikers/contacten/campagnes. |
| `POST /api/superadmin/accounts` 🔒\* | Nieuw klantaccount aanmaken (zelfde als `/api/admin/accounts`, maar via beheerderslogin i.p.v. `X-Admin-Secret`). |
| `GET /api/superadmin/accounts/{id}` 🔒\* | Detail van één account: teamleden, campagnes, contacten (max. 200), LinkedIn-cijfers. |
| `POST /api/superadmin/accounts/{id}/users/{user_id}/reset-password` 🔒\* | Wachtwoord van één teamlid resetten. |
| `POST /api/send` 🔒 | Verstuur een losse mail (`to`, `subject`, `message`) — via het eigen SMTP-adres van het account als dat is ingesteld, anders via het gedeelde Twikey-adres. |
| `GET /api/inbox` 🔒 | Laatste inbox-berichten + ongelezen/vandaag-tellingen. Altijd de gedeelde Gmail-mailbox, ook met een eigen SMTP-afzender ingesteld. |
| `GET /api/email-settings` 🔒 | Eigen SMTP-instellingen van dit account (zonder wachtwoord), of `{"configured": false}`. |
| `POST /api/email-settings` 🔒 | Eigen SMTP-instellingen opslaan/bijwerken (`host`, `port`, `username`, `password`, `from_email`, `from_name`, `use_tls`). Leeg wachtwoord behoudt het huidige. |
| `DELETE /api/email-settings` 🔒 | Eigen SMTP-instellingen verwijderen — het account gaat weer via het gedeelde Twikey-adres. |
| `POST /api/email-settings/test` 🔒 | Stuurt een echte testmail met de opgegeven (nog niet per se opgeslagen) instellingen, naar `test_to` of anders het eigen inlogadres. |
| `GET /api/contacts` 🔒 | Lijst van contacten. Query-params: `q` (zoeken), `tag`, `assigned_to` (user-id of `none`), `exclude_excluded`, `exclude_dnc`. |
| `GET /api/contacts/{id}` 🔒 | Eén contact incl. tags/toewijzing. |
| `GET /api/contacts/{id}/timeline` 🔒 | Audit trail: CRM-events + campagne-mails + opvolgsequentie-mails + LinkedIn-outreach, nieuwste eerst. Verzonden/mislukte mails bevatten (vanaf Fase 3) de daadwerkelijk verstuurde `subject`/`body`, niet alleen een sjabloonverwijzing. |
| `POST /api/contacts` 🔒 | Eén contact toevoegen/bijwerken. |
| `PATCH /api/contacts/{id}` 🔒 | CRM-velden bijwerken (`job_title`, `sector`, `company`, `linkedin_url`, `is_customer`, `has_open_quote`, `do_not_contact`). |
| `POST /api/contacts/bulk` 🔒 | Meerdere contacten in één keer toevoegen. |
| `POST /api/contacts/import-csv` 🔒 | CSV-bestand importeren (multipart `file`), flexibele NL/EN-kolomherkenning. |
| `GET /api/contacts/export-csv` 🔒 | Alle contacten als CSV downloaden. |
| `GET /api/tags` 🔒 / `POST /api/tags` 🔒 / `DELETE /api/tags/{id}` 🔒 | Tags beheren voor je account. |
| `POST /api/contacts/{id}/tags` 🔒 / `DELETE /api/contacts/{id}/tags/{tag_id}` 🔒 | Tag aan een contact koppelen/loskoppelen. |
| `POST /api/contacts/{id}/assign` 🔒 | Contact toewijzen aan een teamlid (`user_id`, of `null` om los te koppelen). |
| `GET /api/buyer-personas` 🔒 / `POST /api/buyer-personas` 🔒 / `DELETE /api/buyer-personas/{id}` 🔒 | Buyer persona's beheren voor je account (Fase 3). Verwijderen maakt de koppeling bij contacten/sequenties/campagne-varianten leeg i.p.v. te blokkeren. |
| `PUT /api/contacts/{id}/persona` 🔒 | De buyer persona van één contact instellen (`persona_id`) of loskoppelen (`persona_id: null`) — een contact heeft er hoogstens één tegelijk. |
| `GET /api/reminders` 🔒 / `POST /api/reminders` 🔒 / `POST /api/reminders/{id}/complete` 🔒 | Herinneringen (agenderen) per contact. |
| `GET /api/exclusions` 🔒 / `POST /api/exclusions` 🔒 / `DELETE /api/exclusions/{id}` 🔒 | Uitsluitlijst (domein/bedrijf) beheren. |
| `POST /api/exclusions/import-csv` 🔒 | CSV met te vermijden domeinen/bedrijven importeren. |
| `GET/POST/DELETE /api/integrations/prospecting` 🔒 | Explorium/Vibe Prospecting API-key opslaan (credential-only, zie `crm-roadmap.md`). |
| `GET/POST/DELETE /api/integrations/hubspot` 🔒 | HubSpot access token + uitsluitingsvoorkeuren opslaan (credential-only). |
| `GET /api/support/kb` | Kennisbank doorzoeken (`q`). Publiek. |
| `POST /api/support/tickets` 🔒 / `GET /api/support/tickets` 🔒 | Supportvraag indienen / eigen supportvragen bekijken. |
| `GET /api/superadmin/support/tickets` 🔒\* / `POST /api/superadmin/support/tickets/{id}/reply` 🔒\* | Supportvragen van alle accounts bekijken/beantwoorden. |
| `POST /api/validate-message` 🔒 | Valideer een bericht (`text`, `platform`). |
| `GET /api/campaigns` 🔒 | Lijst van campagnes (van je eigen account). |
| `POST /api/campaigns` 🔒 | Nieuwe A/B-campagne aanmaken (verdeelt contacten round-robin over de varianten). Elke variant mag een `persona_id` hebben (Fase 3) — een contact met een matchende persona krijgt altijd die variant; optioneel `persona_id` op de campagne zelf beperkt de hele ronde tot contacten met die persona. |
| `POST /api/campaigns/{id}/launch` 🔒 | Verstuurt de campagne-mails echt (via het eigen SMTP-adres als dat is ingesteld, anders via Gmail), met tracking. |
| `GET /api/campaigns/{id}/results` 🔒 | Verzonden/opens/clicks/form-fills per groep. |
| `GET /track/open/{token}.png` | Open-tracking pixel (wordt automatisch in mails ingesloten). |
| `GET /track/click/{token}` | Click-tracking redirect naar de lead-magnet-pagina. |
| `POST /track/formfill/{token}` | Registreert een formulier-invulling op de lead-magnet-pagina. |
| `GET /api/linkedin/stats` 🔒 | Geaggregeerde LinkedIn-outreach-cijfers. |
| `GET /api/linkedin/templates` 🔒 | De 4 LinkedIn-berichtsjablonen. |
| `PUT /api/linkedin/templates/{id}` 🔒 | Pas een sjabloon aan. |
| `POST /api/linkedin/log` 🔒 | Log een handmatige LinkedIn-actie. |
| `GET /api/linkedin/log` 🔒 | Recente gelogde LinkedIn-activiteit. |
| `POST /api/prospecting/businesses/match` 🔒 | Explorium: zoek bedrijven op naam/domein (echte live-aanroep, per-account API-key). |
| `POST /api/prospecting/businesses/lookalikes` 🔒 | Explorium: vind vergelijkbare bedrijven ("lookalikes") bij een bedrijf. |
| `POST /api/prospecting/prospects/match` 🔒 | Explorium: zoek contactpersonen binnen gevonden bedrijven. |
| `POST /api/prospecting/prospects/enrich` 🔒 | Explorium: verrijk gevonden prospects met contactgegevens (e-mailadres e.d.). |
| `POST /api/prospecting/import` 🔒 | Importeer een verrijkte Explorium-prospect als contact (`source="vibe_prospecting"`), inclusief automatische HubSpot-uitsluitingscheck. |
| `POST /api/hubspot/check` 🔒 | Controleer handmatig één domein tegen HubSpot (bestaande klant / open deal), zonder een contact aan te maken. |
| `GET /api/objections` 🔒 / `POST /api/objections` 🔒 | Bezwaren-bibliotheek bekijken / een categorie toevoegen. |
| `PUT /api/objections/{id}` 🔒 / `DELETE /api/objections/{id}` 🔒 | Een bezwaarcategorie aanpassen / verwijderen. |
| `POST /api/replies/fetch` 🔒 | Nieuwe replies ophalen via IMAP, categoriseren en een AI-conceptantwoord genereren (of automatisch versturen, zie de auto-reply-instelling). |
| `GET /api/replies` 🔒 | Alle binnengekomen replies van dit account. |
| `GET /api/replies/drafts` 🔒 | Conceptantwoorden, optioneel gefilterd op `status` (`pending`/`sent`/`dismissed`). |
| `POST /api/replies/drafts/{id}/approve` 🔒 | Een conceptantwoord (evt. aangepast via `draft_body`) goedkeuren en echt versturen. |
| `POST /api/replies/drafts/{id}/dismiss` 🔒 | Een conceptantwoord afwijzen zonder te versturen. |
| `GET /api/settings/auto-reply` 🔒 / `POST /api/settings/auto-reply` 🔒 | "Automatisch versturen"-instelling bekijken/wijzigen (standaard uit). |
| `GET /api/sequences` 🔒 / `POST /api/sequences` 🔒 | Opvolgsequenties bekijken (incl. inschrijvingstellingen) / een nieuwe sequence met stappen aanmaken. Optioneel `persona_id` (Fase 3) koppelt de sequence aan één buyer persona. |
| `GET /api/sequences/{id}` 🔒 | Detail van één sequence incl. stappen. |
| `POST /api/sequences/{id}/status` 🔒 | Sequence op `active`/`paused` zetten. |
| `POST /api/sequences/{id}/enroll` 🔒 | Eén of meer contacten inschrijven op een sequence. |
| `POST /api/sequences/auto-enroll-by-persona` 🔒 | Fase 3: schrijft elk contact met een buyer persona (dat nergens actief loopt) automatisch in op de actieve sequence die aan diezelfde persona gekoppeld is. Idempotent. |
| `GET /api/sequences/{id}/enrollments` 🔒 | Ingeschreven contacten van een sequence met hun status/voortgang. |
| `POST /api/cron/process-sequences` | Verstuurt alle due opvolgmails over alle accounts heen. Vereist `X-Admin-Secret`, bedoeld voor een externe scheduler — zie `DEPLOY.md`. |

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
# - ANTHROPIC_API_KEY: optioneel, alleen nodig voor AI-conceptantwoorden op
#   binnenkomende replies (tabblad Replies). Zonder deze key vallen
#   conceptantwoorden automatisch terug op de standaard bezwaar-suggestie.
#   Dit is één gedeelde, platform-brede key (niet per klantaccount) - zie
#   "Reply-tracking, AI-conceptantwoorden en opvolgsequenties" hierboven.

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
