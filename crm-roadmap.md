# Twikey Sales Platform — Mini-CRM & Groei-roadmap

Verzameling van alle features die zijn opgehaald in het gesprek van 6-8 sept. 2026,
met de architectuurkeuzes die daarbij zijn gemaakt. Dit document is het naslagwerk
zodat een volgende sessie (of teamlid) precies weet wat er gebouwd is/nog moet worden
en waarom bepaalde keuzes zijn gemaakt.

## Bevestigde scope (door Benjamin aangegeven)

1. **Mini-CRM**: contactpersonen met functie, sector, LinkedIn, tags, toewijzing aan
   een teamlid (accountmanager/SDR). Import via CSV **en** via Vibe Prospecting
   (Explorium). Volledige export.
2. **Audit trail**: alles wat er met een lead gebeurt moet zichtbaar zijn - inclusief
   uitgaande communicatie (mails, LinkedIn-acties).
3. **Lookalikes vinden** via Explorium.
4. **Opvolgmail-flows**: instelbare sequenties (stap 2, 3, ... na X dagen).
5. **Reply-tracking**: bijhouden wat geopend/geklikt is, en welke replies binnenkomen.
6. **AI-conceptantwoorden via Claude**: bij een inkomende reply wordt automatisch een
   bezwaar-categorie herkend + een conceptantwoord klaargezet (standaard
   bezwaren-overzicht + hoe ermee om te gaan). Nooit automatisch verzonden by
   default - moet altijd door de gebruiker worden goedgekeurd. Er komt wel een
   instelling om volledig automatisch te laten versturen, **standaard uit**,
   met het advies die pas later aan te zetten.
7. **Uitsluiting van bestaande klanten / lopende offertetrajecten** zodat prospecting
   hen niet opnieuw benadert. Antwoord op "waar moet dit vandaan komen": **meerdere
   opties tegelijk beschikbaar maken** (niet kiezen voor één bron).
8. **Integraties-sectie** in het dashboard: koppelingen voor het importeren van leads
   en voor mail (verzenden + lezen) en CRM.
9. **Supportpagina**: FAQ, een supportvraag kunnen stellen, en een kennisbank zodat
   klanten zichzelf kunnen helpen.

## Architectuurkeuzes (al akkoord)

- **Explorium/Vibe Prospecting API-key**: per account (net als de bestaande
  SMTP-instellingen: versleuteld opgeslagen, "bring your own"), niet één gedeelde
  platform-key. Reden: het is een credits-gemeten betaalde dienst per zoekopdracht -
  bij een gedeelde key betaalt Benjamin voor het zoekgedrag van alle klanten samen.
  Klant hoeft de key nu nog niet te hebben: het scherm/de knop wordt vast gebouwd,
  staat "niet geconfigureerd" totdat de key is ingevuld.
- **Reply-inbox**: IMAP-instellingen worden toegevoegd aan de bestaande
  "Mail-instellingen"-tab (naast SMTP), zodat ook klanten met een eigen mailserver
  (niet alleen de gedeelde Gmail-inbox van Twikey Campaigns) replies kunnen laten
  uitlezen.
- **Uitsluitlijst**: drie mechanismen naast elkaar, allemaal per account aan/uit te
  zetten in Integraties:
  1. Handmatig per contact markeren ("is klant" / "offerte loopt") in de CRM-tab.
  2. CSV-upload van een lijst met te vermijden bedrijven/domeinen.
  3. Live koppeling met HubSpot (company/deal-zoekopdracht op domein) - voor Twikey
     zelf relevant omdat hun eigen CRM HubSpot is. Vereist een eigen HubSpot private
     app-token per account (Integraties-tab), los van de Vibe Prospecting-koppeling
     van deze Claude-sessie zelf (die laatste werkt alleen hier in de chat, niet in
     het live dashboard - het dashboard heeft zijn eigen server-naar-server
     HubSpot-koppeling nodig).
  Een contact dat door een van de drie wordt gematcht krijgt `excluded_reason`
  gezet en wordt automatisch overgeslagen bij nieuwe campagnes/imports (met
  mogelijkheid tot handmatige override).

## Bouwvolgorde (fasering - te groot voor één keer te bouwen en goed te testen)

**Fase 1 (nu gebouwd)**:
- Contacten uitgebreid: functie, sector, toegewezen teamlid, bron (csv/manual/
  vibe_prospecting/hubspot), is_customer/has_open_quote/excluded_reason.
- Tags (aanmaken, toewijzen, verwijderen, filteren).
- Toewijzing aan teamlid.
- CSV import (flexibele kolomherkenning NL/EN) + CSV export (alle velden + tags).
- Handmatige + CSV-uitsluitlijst (twee van de drie mechanismen).
- Audit trail / timeline per contact: combineert bestaande data (campagne-mails
  verzonden/geopend/geklikt/formulier, LinkedIn-outreach) met nieuwe CRM-events
  (aangemaakt, tag gewijzigd, toegewezen, uitgesloten).
- Integraties-tab in het dashboard: overzichtscherm met kaarten voor Mail
  (SMTP + IMAP), Vibe Prospecting/Explorium (key invoeren), HubSpot (token
  invoeren + uitsluiting aan/uit), CSV-import.
- Supportpagina: FAQ/kennisbank (doorzoekbaar) + supportvraag indienen
  (komt binnen bij Twikey support, zichtbaar in het support-dashboard/admin.html).

**Fase 2 (gebouwd, getest, geleverd — 9 sept. 2026)**:
- Vibe Prospecting/Explorium: echte zoek- en lookalike-endpoints (business/prospect
  match + contact-enrichment), tegen de per-account key.
  (`POST /api/prospecting/businesses/match`, `/businesses/lookalikes`,
  `/prospects/match`, `/prospects/enrich`, `/import`.)
- HubSpot live-uitsluitingscheck (companies/deals search by domain) tegen het
  per-account token. Automatisch gehaakt in elk nieuw contact (handmatig, bulk,
  CSV, Vibe Prospecting) en los aan te roepen via `POST /api/hubspot/check`.
- IMAP-uitlezen van replies + matching aan contact/campagne
  (`POST /api/replies/fetch`, `GET /api/replies`).
- Bezwaren-bibliotheek (standaard categorieën + antwoord-suggesties,
  `/api/objections`) + AI-concept via Claude (server-side Anthropic-koppeling,
  `backend/ai_client.py`) + goedkeuringsscherm (`/api/replies/drafts/*`) + de
  "automatisch versturen"-instelling (`/api/settings/auto-reply`,
  **standaard uit**, zoals afgesproken).
- Opvolgmail-flows: sequenties met wachttijd per stap, stopt automatisch bij
  reply, gebruikt dezelfde verzendweg (gedeelde Gmail of eigen SMTP) als de
  rest (`/api/sequences/*`, `POST /api/cron/process-sequences`).

Getest tegen echte lokale services (geen mocks): een handgeschreven minimale
IMAP-server, en handgeschreven HTTP-servers die de HubSpot- en Explorium-API's
nabootsen (geen live vendor-credentials beschikbaar in de bouwomgeving) — zie
"Openstaande vragen" hieronder voor wat dat betekent voor de eerste echte
klant-key. Volledige backend-testsuite (~60 checks, inclusief cross-tenant
isolatie voor elk nieuw resourcetype) en een end-to-end browsertest van beide
nieuwe tabbladen (Replies, Sequenties) plus de uitgebreide Vibe
Prospecting/HubSpot-kaarten in Integraties zijn gedraaid en groen. Zie
`README.md` voor de volledige eindgebruikers-documentatie van alle Fase
2-functionaliteit en endpoints.

## Aanvullingen (na eerste versie van dit document)

Toegevoegd aan **Fase 1** (simpel, geen externe koppeling nodig):
- Zoekfunctie op contacten: naam, e-mailadres, bedrijfsnaam (en desgewenst tag/sector).
- "Niet meer benaderen" (`do_not_contact`) per contact - los van de
  klant/offerte-uitsluiting hierboven, dit is een expliciete "stop"-vlag die overal
  (campagnes, opvolgmails, imports) wordt gerespecteerd.
- Agenderen/reminder-functie per contact: "neem over 3 maanden weer contact op" -
  zet een reminder met datum + notitie, met een overzicht van openstaande/vervallen
  reminders (wie moet wie wanneer weer benaderen).

Toegevoegd aan **Fase 2** (hoort bij de opvolgmail-flow-engine die daar al gepland
stond):
- Een sequence stopt automatisch zodra de contactpersoon reply't (niet nog een
  opvolgmail sturen na een antwoord).
- Vanuit een reply kan direct een reminder/agenderen worden ingesteld (bv. klant
  zegt "bel me over 3 maanden") in plaats van de sequence te hervatten.

## Fase 3 (nog niet gescoped) — AI-gedreven intake & optimalisatie

Later toegevoegd, nog niet uitgewerkt:

- **Intakeformulier per nieuwe klant**: waardepropositie, USP's, buyer
  personas - bij aansluiting in te vullen.
- De tool stelt zelf verdiepende vragen (via Claude) om dit scherp te
  krijgen, in plaats van alleen een statisch formulier.
- Uitgaande mails worden op basis van dit profiel voorgesteld (subject/body-
  suggesties per persona/segment), i.p.v. de huidige vaste 4 vaste
  A/B-varianten.
- Analyse welke combinaties (variant × sector × persona × functie) het beste
  presteren, gebaseerd op de bestaande open/click/reply-data.
- Periodieke, automatisch gegenereerde verbetervoorstellen om de
  succesratio te verhogen.

Dit hangt samen met Fase 2 (heeft dezelfde Claude/Anthropic-koppeling nodig
als de AI-conceptantwoorden, en dezelfde reply-data als reply-tracking) -
pas te plannen zodra Fase 2 staat.

**Update 9 sept. 2026 — klaar om te scopen**: de infrastructuur waar Fase 3
op leunt bestaat nu. De Anthropic/Claude-koppeling (`backend/ai_client.py`,
platform-brede `ANTHROPIC_API_KEY`) en reply-/objectie-data
(`incoming_replies`, `reply_drafts`, `objection_templates`) uit Fase 2 zijn
gebouwd en getest. Fase 3 kan dus vanaf nu concreet uitgewerkt worden zodra
Benjamin daar behoefte aan heeft.

## Openstaande vragen voor Fase 2 (opgelost bij oplevering, 9 sept. 2026)

- **HubSpot-token/scopes**: opgelost — een private app access token met
  (minimaal) de scopes `crm.objects.companies.read` en
  `crm.objects.deals.read` volstaat voor de live-uitsluitingscheck
  (`backend/hubspot_client.py` doet alleen company/deal-lookups, geen writes).
  Benjamin laat elke klant zelf zo'n token aanmaken in hún eigen HubSpot
  (Instellingen → Integraties → Private apps) en vult 'm in op het
  Integraties-tabblad — geen platform-brede HubSpot-app nodig.
- **Explorium v2-eindpunten**: geïmplementeerd conform de gedocumenteerde
  developers.explorium.ai-API (`businesses/match`, `businesses/lookalikes`,
  `prospects/match`, `prospects/enrich`), en end-to-end getest tegen een
  zelfgebouwde nagebootste server omdat er geen live Explorium-key beschikbaar
  was in de bouwomgeving. **Nog niet bevestigd tegen een echte key/response**
  — dit is een expliciete restpunt: bij de eerste klant die een echte
  Explorium-key invult, één keer een test-zoekopdracht laten draaien en de
  respons vergelijken met wat `prospecting_client.py` verwacht (met name
  veldnamen in de JSON-respons), voor het geval de officiële API sinds het
  bouwen is gewijzigd.
- **Anthropic API-key plaatsing**: opgelost, zoals voorgesteld — platform-
  niveau (`ANTHROPIC_API_KEY` in Render/`.env`, niet per account). Bevestigd
  bij implementatie: het gaat om een klein aantal generaties per
  binnenkomende reply, geen zware kosten zoals bij Explorium-zoekopdrachten.
