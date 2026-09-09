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

## Fase 3a (gebouwd, getest, geleverd — 9 sept. 2026): buyer persona's & rijkere audit trail

Twee losstaande, kleinere uitbreidingen bovenop Fase 2, niet te verwarren met
de grotere "Fase 3 (nog niet gescoped)"-visie hieronder (AI-intakeformulier
per klant) — dat blijft voorlopig ongebouwd.

**1. Buyer persona's + persona-specifieke mail flows.**
- `buyer_personas`: een beheerde lijst per account (zelfde idee als tags,
  maar een contact heeft **hoogstens één** persona tegelijk —
  `contacts.persona_id` — zodat "welke flow hoort bij dit contact"
  ondubbelzinnig is). CRUD via `GET/POST /api/buyer-personas`,
  `DELETE /api/buyer-personas/{id}` (verwijderen maakt de koppeling bij elk
  contact/sequence/variant die 'm nog gebruikte weer leeg, i.p.v. te
  blokkeren). Contact koppelen/loskoppelen: `PUT /api/contacts/{id}/persona`.
  In het dashboard: kolom + filter "Buyer persona" op het Contacten-tabblad
  (inline dropdown per rij, met "+ Nieuwe persona..." om er meteen een aan te
  maken).
- **Opvolgsequenties**: een sequence kan optioneel aan één persona gekoppeld
  worden (`sequences.persona_id`, veld in de "Nieuwe opvolgmail-sequence"-
  kaart). Het inschrijfpaneel filtert dan automatisch tot contacten met die
  persona. Nieuwe knop "Automatisch inschrijven o.b.v. persona"
  (`POST /api/sequences/auto-enroll-by-persona`) schrijft in één keer elk
  contact met een persona (dat nergens actief loopt) in op de actieve
  sequence die bij die persona hoort — contacten zonder persona, of met een
  persona zonder bijpassende actieve sequence, worden overgeslagen (bewust
  geen generieke fallback hier; dat blijft de bestaande handmatige
  inschrijf-flow). Idempotent: nogmaals draaien schrijft niemand dubbel in.
- **Campagnes**: twee onafhankelijke mechanismen, allebei opgebouwd bovenop
  de bestaande `campaign_variants`:
  1. Een variant kan aan een persona gekoppeld worden
     (`campaign_variants.persona_id`, `VariantIn.persona_id` in de API). Bij
     het aanmaken van een campagne krijgt een contact met een matchende
     persona altijd die variant (round-robin *binnen* de persona-variant(en)
     als er meerdere zijn — A/B-testen blijft dus mogelijk per persona);
     iedereen anders round-robint zoals voorheen over de persona-loze
     ("generieke") varianten. Heeft een campagne uitsluitend persona-
     varianten, dan valt een niet-matchend contact terug op een gewone
     round-robin over alle varianten, zodat niemand stilzwijgend wordt
     overgeslagen.
  2. Het A/B Test-tabblad heeft geen eigen variant-editor (launcht altijd de
     4 vaste standaard-aanbiedingen) — daarom is er daar een eenvoudiger,
     campagnebreed alternatief: een "Buyer persona"-keuzelijst bij het
     starten van een test die de hele ronde filtert tot alleen contacten met
     die persona (`CampaignIn.persona_id` → `create_campaign(...,
     only_persona_id=...)`). Losstaand van mechanisme 1 hierboven, dat wél
     per-variant-inhoud ondersteunt voor toekomstig gebruik (bv. via de API
     of een latere variant-editor).

**2. Rijkere audit trail: échte mailinhoud, niet alleen het sjabloon.**
- `campaign_recipients` en `sequence_sends` hebben nu `rendered_subject`/
  `rendered_body`: de exacte, gepersonaliseerde tekst die (bij een poging
  tot) verzenden daadwerkelijk gebruikt is — gevuld op het moment van
  verzenden (`record_send_result()`, `record_sequence_send()`), ongeacht of
  het lukte. Bewust gekozen (i.p.v. achteraf het sjabloon opnieuw invullen):
  zo blijft de audit trail correct ook als een sjabloon later wordt
  aangepast of verwijderd. Kanttekening: bestaat alleen voor verzendingen
  vanaf nu — oudere rijen (van vóór deze update) tonen in de tijdlijn geen
  inhoud.
- `sequence_sends` had voorheen geen tijdstip bij een mislukte verzending
  (`sent_at` werd alleen bij succes gezet) — toegevoegd: `attempted_at`,
  altijd gezet, zodat een mislukte opvolgmail nu ook in de tijdlijn
  verschijnt in plaats van stilzwijgend te verdwijnen.
- `contact_timeline()`/`GET /api/contacts/{id}/timeline` nam voorheen geen
  opvolgsequentie-mails mee (alleen campagne-mails + CRM-events +
  LinkedIn) — toegevoegd, met dezelfde subject/body-velden.
- Tijdlijn-weergave in het dashboard (contactmodal) herbouwd van een platte
  lijst naar een Datum/Gebeurtenis-tabel met kleur per event-type
  (verzonden/mislukt/geopend/geklikt/LinkedIn), geïnspireerd op de
  Events-tab van het Twikey-product zelf. Een verstuurde/mislukte mail met
  bewaarde inhoud krijgt een "e-mail bekijken"-link die de opgeslagen
  subject + brontekst (HTML voor campagnes, platte tekst voor sequenties;
  inline getoond, niet als gerenderde e-mail) uitklapt.

Getest: nieuwe suite `test_fase3.py` (persona-CRUD, contact-koppeling +
filter, sequence/campagne-persona-routing, auto-enroll idempotentie,
rendered-content in de tijdlijn, persona-verwijdering) plus de volledige
bestaande `test_crm.py`/`test_fase2.py`-suites opnieuw gedraaid — alles
groen, geen regressies.

## Fase 3b (gebouwd, getest, geleverd — 9 sept. 2026): sidebar-menu, intake + AI-mailsuggesties, ICP-scoring

Drie uitbreidingen, gebouwd in één vervolgsessie op Fase 3a hierboven.

**1. Sidebar-menu (echt doorgevoerd, niet meer alleen een mockup).**
De horizontale tabbalk in `dashboard.html` is vervangen door een verticale
sidebar, gegroepeerd per belangrijk onderdeel: Dashboard + Contacten los
bovenaan, dan de groepen "Outreach" (Email Sync, LinkedIn, A/B Test,
Sequenties), "Gesprekken" (Replies, Validation) en "Inzicht" (Analytics),
met accountbrede instellingen (Profiel, Team, Integraties, Support) plus
Uitloggen onderaan vastgepind. Dit was eerder alleen een Design-canvas
mockup (zie Fase 3a) - nu is het de daadwerkelijke navigatie van de app.
Bestaande tab-content/JS is ongewijzigd; alleen de navigatie eromheen is
herbouwd, geverifieerd met een browsertest (Playwright) van meerdere
tabbladen.

**2. Bedrijfsprofiel/intake + AI-verdiepingsronde + AI-mailsuggesties**
(dit is het grootste deel van de hieronder beschreven "Fase 3"-visie, nu
gebouwd als kernversie - zie "nog open" onderaan voor wat bewust is
uitgesteld):
- Nieuw tabblad "Profiel": waardepropositie + USP's (vrije tekst, één USP
  per regel), plus per buyer persona een optionele omschrijving
  (pijnpunt/context) - `account_profiles`- en `buyer_personas.description`
  in de database, `GET/PUT /api/account-profile`,
  `PUT /api/buyer-personas/{id}`.
- **AI-verdiepingsronde** (bewust één ronde, geen doorlopend chatgesprek -
  expliciete scope-keuze): een knop genereert 2-4 gerichte vervolgvragen via
  Claude om vage antwoorden aan te scherpen (`POST
  /api/account-profile/generate-questions`,
  `POST /api/account-profile/answer-questions`). Zonder ANTHROPIC_API_KEY
  (of bij een mislukte aanroep) valt dit terug op een vaste, altijd nuttige
  set vragen - zelfde "werkt ook zonder AI-koppeling"-garantie als de rest
  van dit project.
- **AI-mailsuggesties**: het A/B Test-tabblad heeft nu een echte
  variant-editor (rijen toevoegen/verwijderen, per rij een optionele
  buyer persona) in plaats van alleen de 4 vaste standaardvarianten te
  kunnen launchen. Een knop "AI-suggesties genereren"
  (`POST /api/campaigns/suggest-variants`) laat Claude 1-2 subject/body-
  varianten voorstellen op basis van het bedrijfsprofiel (en, indien
  gekozen, één buyer persona) - zonder AI-koppeling valt dit terug op een
  eenvoudige sjabloon-invulling met de eigen waardepropositie/USP's, zodat
  de knop altijd iets bruikbaars teruggeeft. Niets wordt automatisch
  verzonden of opgeslagen; de klant bewerkt/keurt eerst goed voordat een
  campagne daadwerkelijk wordt aangemaakt (`POST /api/campaigns` accepteert
  al langer een vrije `variants`-lijst, zie Fase 3a - dit hergebruikt dat
  pad, de UI faciliteert het nu pas echt).
- **Bewust uitgesteld** (kernversie-scope-keuze): een volledig doorlopend
  AI-chatgesprek tijdens de intake (i.p.v. één verdiepingsronde), en
  periodieke, automatisch gegenereerde verbetervoorstellen (heeft meer
  verzenddata nodig om zinvol te zijn) - zie de "Fase 3 (nog niet
  gescoped)"-tekst hieronder, nu bijgewerkt met wat al wel gebouwd is.

**3. ICP-scoring** (het "analyse welke combinaties het beste presteren"-punt
uit Fase 3, naar voren gehaald op verzoek):
- Nieuw veld `contacts.revenue_range` (omzetcategorie, vrije tekst zoals
  sector - bv. "1-10M"), instelbaar via het contactformulier, CSV-import
  (kolommen "omzet"/"revenue"/"jaaromzet" e.d. worden herkend) en CSV-export.
- `database.icp_scores()` / `GET /api/analytics/icp-scores`: scoort sector,
  buyer persona en omzetcategorie - los én als combinatie - op basis van de
  al bestaande open/klik/reply-data (geen nieuwe tracking nodig). Score =
  50% reply-rate + 30% click-rate + 20% open-rate (reply weegt het zwaarst:
  het enige signaal dat de ontvanger daadwerkelijk heeft gereageerd).
  Combinaties/dimensies met minder dan 3 verzonden mails krijgen
  `sufficient_data: false` en tellen niet mee voor de aanbevolen ICP, om te
  voorkomen dat één toevallige open op 1 mail als "100% score" bovenaan
  komt te staan.
- Nieuwe "ICP-analyse"-kaart op het Analytics-tabblad: een aanbevolen-ICP-
  banner (sector × persona × omzet met de beste score, of - als nog geen
  enkele combinatie genoeg data heeft - de sterkste losse dimensies met een
  duidelijke toelichting waarom), een tabel met de sterkste combinaties, en
  een score-tabel per sector/persona/omzetcategorie apart.

Getest: `test_fase3b.py` (bedrijfsprofiel opslaan, AI-verdiepingsronde met
en zonder AI-key, persona-omschrijving bewerken, variant-suggesties met
fallback, end-to-end een AI-suggestie gebruiken om een campagne te
launchen) en `test_icp.py` (een duidelijk "hot" vs. "cold" segment
opgebouwd met echte campagne-tracking en een reply, en geverifieerd dat de
scoring, ranking en aanbevolen-ICP-logica de juiste winnaar aanwijst) - plus
de volledige bestaande suites (`test_crm.py`, `test_fase2.py`,
`test_fase3.py`) opnieuw gedraaid, alles groen, geen regressies.

## Fase 3 (nog niet gescoped) — AI-gedreven intake & optimalisatie

**Update 9 sept. 2026**: de kern hiervan is gebouwd, zie "Fase 3b"
hierboven. Wat bewust nog open staat:

- Een volledig doorlopend AI-chatgesprek tijdens de intake (i.p.v. de
  gebouwde ene verdiepingsronde) - expliciete scope-keuze, niet gebouwd.
- Periodieke, automatisch gegenereerde verbetervoorstellen om de
  succesratio te verhogen (bovenop de nu gebouwde ICP-scoring/analyse) -
  heeft meer verzameld verzend-/respons-data nodig om zinvol te zijn dan er
  in de bouwomgeving beschikbaar was.

Oorspronkelijke, nog niet uitgewerkte visie (voor de context):

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

**Update 9 sept. 2026 (later dezelfde dag) — grotendeels gebouwd**: zie
"Fase 3b" hierboven - intakeformulier, AI-verdiepingsronde,
AI-mailsuggesties en de combinatie-analyse (nu "ICP-scoring") zijn gebouwd,
getest en geleverd. Alleen het doorlopende AI-chatgesprek tijdens intake en
de periodieke auto-verbetervoorstellen staan nog open, zoals hierboven
toegelicht.

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
